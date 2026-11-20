"""Surgical 450-DPI isolated crop verification and `gemini-decision-1` / VLM boundary arbitration.

Operates non-mutatingly on an extracted run (`workspace/runs/` or `workspace/experiments/`),
emitting a validated `correction-v2` proposal ready for `lanterns intake`.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lanterns.core.atomic import atomic_write_json
from lanterns.core.config import BookConfig, load_prompt, validate_data
from lanterns.core.cost import CostCeilingExceeded, CostLedger
from lanterns.core.models import FURNITURE_BLOCK_TYPES, BlockIR, PageIR
from lanterns.core.paths import RepoPaths
from lanterns.core.pricing import conservative_call_reservation_usd, ensure_priced_routes, parse_model_route
from lanterns.core.tags import extract_inline_markers, parse_tagged_text
from lanterns.extraction.normalize import split_note_label_markers
from lanterns.extraction.pdf_ingest import crop_normalized_bbox
from lanterns.extraction.pipeline import load_run_pages
from lanterns.providers.base import DecisionBatchResult, ProviderRequest, ProviderSuccess
from lanterns.providers.gemini import GeminiProvider
from lanterns.providers.router import build_provider

_MULTI_DIGIT_RE = re.compile(r"\b\d[\d,]{2,}\b")
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z]{5,}\b")


def _load_witness_map(run_dir: Path) -> dict[int, str]:
    wit_dir = run_dir / "witnesses"
    if not wit_dir.is_dir():
        return {}
    out: dict[int, str] = {}
    for p in sorted(wit_dir.glob("page_*.txt")):
        digits = "".join(ch for ch in p.stem if ch.isdigit())
        if digits:
            out[int(digits)] = p.read_text(encoding="utf-8", errors="replace")
    return out


def collect_epistolary_hotspots(
    pages: list[PageIR],
    witness_by_page: dict[int, str],
) -> dict[str, list[dict[str, Any]]]:
    """Identify $0 block-level text hotspots and ambiguous epistolary boundary candidates."""
    proper_counts: Counter[str] = Counter()
    for page in pages:
        for blk in page.blocks:
            if blk.type not in FURNITURE_BLOCK_TYPES:
                proper_counts.update(_PROPER_NOUN_RE.findall(blk.plain_text))

    crop_targets: list[dict[str, Any]] = []
    boundary_targets: list[dict[str, Any]] = []
    for page in pages:
        if not page.failed_closed:
            _collect_page_hotspots(
                page,
                witness_by_page.get(page.page_number, ""),
                proper_counts,
                crop_targets,
                boundary_targets,
            )
    return {"crop_targets": crop_targets, "boundary_targets": boundary_targets}


def _collect_page_hotspots(
    page: PageIR,
    witness_txt: str,
    proper_counts: Counter[str],
    crop_targets: list[dict[str, Any]],
    boundary_targets: list[dict[str, Any]],
) -> None:
    wit_nums = {m.replace(",", "") for m in _MULTI_DIGIT_RE.findall(witness_txt)}
    wit_caps = set(_PROPER_NOUN_RE.findall(re.sub(r"-\s*\n\s*", "", witness_txt)))
    callouts: list[str] = []
    note_labels: set[str] = set()
    seen_sig = False

    for idx, blk in enumerate(page.blocks):
        if blk.type in FURNITURE_BLOCK_TYPES:
            continue
        reasons = _inspect_block_reasons(blk, wit_nums, wit_caps, proper_counts)
        if reasons and blk.bbox != [0, 0, 1000, 1000]:
            crop_targets.append({"page_number": page.page_number, "block": blk, "reasons": reasons})

        if blk.type == "footnote":
            note_labels.update(split_note_label_markers(blk.note_label))
        else:
            callouts.extend(extract_inline_markers(blk.tagged_text))

        seen_sig = _track_boundary_candidate(page, idx, blk, seen_sig, boundary_targets)

    unmatched = [c for c in dict.fromkeys(callouts) if c not in note_labels]
    fn_blocks = [b for b in page.blocks if b.type == "footnote" and b.bbox != [0, 0, 1000, 1000]]
    if unmatched and fn_blocks:
        crop_targets.append(
            {
                "page_number": page.page_number,
                "block": fn_blocks[0],
                "reasons": [f"unmatched_callout:{','.join(unmatched[:4])}"],
            }
        )


def _track_boundary_candidate(
    page: PageIR,
    idx: int,
    blk: BlockIR,
    seen_sig: bool,
    boundary_targets: list[dict[str, Any]],
) -> bool:
    if blk.epistolary_transition == "starts_new_letter":
        seen_sig = False
    elif blk.type in {"salutation", "letter_header", "dateline"} and seen_sig and idx > 0:
        boundary_targets.append(
            {
                "page_number": page.page_number,
                "prev_block": page.blocks[idx - 1],
                "candidate_block": blk,
                "reason": "opener_after_signature_without_transition",
            }
        )
        seen_sig = False
    if blk.type == "signature":
        return True
    return seen_sig


def _has_one_char_match(target: str, candidates: set[str], *, same_initial: bool = False) -> bool:
    for cand in candidates:
        if len(cand) != len(target):
            continue
        if same_initial and cand[0] != target[0]:
            continue
        if sum(a != b for a, b in zip(cand, target)) == 1:
            return True
    return False


def _inspect_block_reasons(
    blk: BlockIR,
    wit_nums: set[str],
    wit_caps: set[str],
    proper_counts: Counter[str],
) -> list[str]:
    reasons: list[str] = []
    if "<unclear>" in blk.tagged_text or any(sp.tag == "unclear" for sp in blk.spans):
        reasons.append("unclear_span")
    for raw_num in _MULTI_DIGIT_RE.findall(blk.plain_text):
        norm = raw_num.replace(",", "")
        if len(norm) >= 3 and norm not in wit_nums and _has_one_char_match(norm, wit_nums):
            reasons.append(f"witness_numeric_diff:{raw_num}")
            break
    for word in _PROPER_NOUN_RE.findall(blk.plain_text):
        if proper_counts[word] == 1 and word not in wit_caps and _has_one_char_match(word, wit_caps, same_initial=True):
            reasons.append(f"witness_proper_noun_diff:{word}")
            break
    return reasons


def verify_hotspots(
    book_id: str,
    *,
    run_id: str | None = None,
    verify_id: str | None = None,
    cost_ceiling_usd: float = 0.10,
    model: str | None = None,
    source_pdf: str | Path | None = None,
    crop_provider_override: Any = None,
    decision_provider_override: Any = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Run 450-DPI isolated crop verification + `gemini-decision-1` / VLM boundary arbitration."""
    repo = RepoPaths.from_root(repo_root)
    book = BookConfig.load(book_id, paths=repo)
    target_run_id = book.resolve_run_id(run_id)
    base_run_dir = repo.find_run_dir(book_id, target_run_id)
    pages = load_run_pages(base_run_dir)
    witness_map = _load_witness_map(base_run_dir)

    pdf_path = book.resolve_source_pdf_path(source_pdf, paths=repo)
    settings = book.resolved_extraction_settings(paths=repo)
    crop_route = model or str(settings.get("hotspot_model") or "gemini/gemini-4-hydrogen")
    decision_route = str(settings.get("decision_model") or "gemini/gemini-decision-1")
    boundary_route = str(settings.get("boundary_model") or "gemini/gemini-4-hydrogen")

    crop_prov_name, crop_mod_name = parse_model_route(crop_route)
    dec_prov_name, dec_mod_name = parse_model_route(decision_route)
    bnd_prov_name, bnd_mod_name = parse_model_route(boundary_route)

    if crop_provider_override is None and crop_prov_name != "mock":
        ensure_priced_routes(
            [(crop_prov_name, crop_mod_name), (dec_prov_name, dec_mod_name), (bnd_prov_name, bnd_mod_name)],
            root=str(repo.root),
        )

    crop_provider = crop_provider_override or build_provider(crop_prov_name, crop_mod_name)
    decision_provider = decision_provider_override or (
        GeminiProvider(model=dec_mod_name) if dec_prov_name == "gemini" and crop_prov_name != "mock" else crop_provider
    )

    eff_verify_id = verify_id or f"verify-{target_run_id}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    verify_dir = repo.run_dir(book_id, eff_verify_id, kind="experiment")
    crops_dir = verify_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    reserve_est = (
        0.0
        if getattr(crop_provider, "name", "") == "mock"
        else conservative_call_reservation_usd(crop_prov_name, crop_mod_name, is_crop=True, root=str(repo.root))
    )
    ledger = CostLedger(verify_dir / "cost_ledger.jsonl", ceiling_usd=cost_ceiling_usd, default_reservation_usd=reserve_est)

    hotspots = collect_epistolary_hotspots(pages, witness_map)
    crop_prompt = load_prompt("hotspot_crop_verify_v1", root=str(repo.root))
    arbiter_prompt = load_prompt("boundary_arbiter_v1", root=str(repo.root))
    now_iso = datetime.now(timezone.utc).isoformat()
    reviewer_id = f"verify-hotspots:{getattr(crop_provider, 'model', crop_mod_name)}"

    corrections: list[dict[str, Any]] = []
    verifications: list[dict[str, Any]] = []

    _verify_crop_targets(
        targets=hotspots["crop_targets"],
        pdf_path=pdf_path,
        base_run_dir=base_run_dir,
        crops_dir=crops_dir,
        crop_prompt=crop_prompt,
        crop_provider=crop_provider,
        ledger=ledger,
        verify_id=eff_verify_id,
        reviewer_id=reviewer_id,
        now_iso=now_iso,
        corrections=corrections,
        verifications=verifications,
    )
    _arbitrate_boundary_targets(
        targets=hotspots["boundary_targets"],
        decision_provider=decision_provider,
        crop_provider=crop_provider,
        arbiter_prompt=arbiter_prompt,
        ledger=ledger,
        verify_id=eff_verify_id,
        reviewer_id=reviewer_id,
        now_iso=now_iso,
        corrections=corrections,
        verifications=verifications,
    )
    ledger.flush()

    proposal_doc = {
        "version": "correction-v2",
        "book_id": book_id,
        "run_id": target_run_id,
        "reviewer": reviewer_id,
        "generated_by": "verify-hotspots",
        "created_at": now_iso,
        "corrections": corrections,
    }
    validate_data("correction_v2", proposal_doc, root=str(repo.root))
    proposal_path = verify_dir / "hotspot_proposal.json"
    report_path = verify_dir / "hotspot_report.json"
    atomic_write_json(proposal_path, proposal_doc)

    report = {
        "book_id": book_id,
        "run_id": target_run_id,
        "verify_id": eff_verify_id,
        "verify_dir": str(verify_dir),
        "proposal_path": str(proposal_path),
        "crop_hotspot_count": len(hotspots["crop_targets"]),
        "boundary_hotspot_count": len(hotspots["boundary_targets"]),
        "proposed_corrections_count": len(corrections),
        "total_spend_usd": round(ledger.total_spent_usd, 6),
        "verifications": verifications,
    }
    atomic_write_json(report_path, report)
    return report


def _record_crop_spend(ledger: CostLedger, crop_provider: Any, reserved: float, prov: dict[str, Any], pnum: int) -> None:
    if getattr(crop_provider, "name", "") == "mock":
        return
    ledger.release_and_record(
        reserved,
        stage="hotspot_crop_450dpi",
        provider=str(prov.get("provider") or "gemini"),
        model=str(prov.get("model") or "unknown"),
        cost_usd=float(prov.get("cost_estimate_usd") or 0.0),
        page_number=pnum,
        input_tokens=int(prov.get("input_tokens") or 0),
        output_tokens=int(prov.get("output_tokens") or 0),
        thoughts_tokens=int(prov.get("thoughts_tokens") or 0),
    )


def _apply_zoom_comparison(
    blk: BlockIR,
    pnum: int,
    reasons: list[str],
    res_data: dict[str, Any],
    *,
    verify_id: str,
    reviewer_id: str,
    now_iso: str,
    corrections: list[dict[str, Any]],
    verifications: list[dict[str, Any]],
) -> None:
    z_blocks = res_data.get("blocks") or []
    z_raw = " ".join(str(zb.get("text") or "").strip() for zb in z_blocks if str(zb.get("text") or "").strip())
    z_tagged, z_plain, _ = parse_tagged_text(z_raw)
    keep_change = bool(z_plain and z_tagged != blk.tagged_text and ("<unclear>" not in z_tagged or "<unclear>" not in blk.tagged_text))
    if keep_change:
        corrections.append(
            {
                "correction_id": f"hs-{verify_id}-{blk.block_id}",
                "page_number": pnum,
                "block_id": blk.block_id,
                "block_id_hint": blk.block_id,
                "anchor_text": blk.plain_text[:160],
                "op": "set_text",
                "replacement_tagged_text": z_tagged,
                "source": "machine",
                "reviewer": reviewer_id,
                "note": f"450-DPI isolated crop verification ({', '.join(reasons)}).",
                "created_at": now_iso,
            }
        )
        verifications.append({"page_number": pnum, "block_id": blk.block_id, "verdict": "propose_set_text", "zoom_tagged": z_tagged})
    else:
        verifications.append({"page_number": pnum, "block_id": blk.block_id, "verdict": "confirmed"})


def _verify_crop_targets(
    *,
    targets: list[dict[str, Any]],
    pdf_path: Path | None,
    base_run_dir: Path,
    crops_dir: Path,
    crop_prompt: str,
    crop_provider: Any,
    ledger: CostLedger,
    verify_id: str,
    reviewer_id: str,
    now_iso: str,
    corrections: list[dict[str, Any]],
    verifications: list[dict[str, Any]],
) -> None:
    is_mock = getattr(crop_provider, "name", "") == "mock"
    for item in targets:
        pnum = int(item["page_number"])
        blk: BlockIR = item["block"]
        src = pdf_path if (pdf_path is not None and pdf_path.is_file()) else (base_run_dir / "renders" / f"page_{pnum:04d}.png")
        if not src.is_file():
            continue
        crop_png = crops_dir / f"crop_{blk.block_id}.png"
        try:
            crop_normalized_bbox(src, blk.bbox, crop_png, page_number=pnum, dpi=450, pad_norm=10)
            reserved = 0.0 if is_mock else ledger.acquire_slot()
        except CostCeilingExceeded:
            break
        except ValueError:
            continue

        req = ProviderRequest(
            prompt=crop_prompt,
            schema_name="page_extract_wire",
            image_paths=[str(crop_png)],
            dynamic_suffix=f"Block `{blk.block_id}` (`{blk.type}`) on page {pnum}. Reasons: {', '.join(item['reasons'])}.",
            settings={"media_resolution": "MEDIA_RESOLUTION_ULTRA_HIGH", "thinking_level": "low", "max_output_tokens": 2048},
        )
        res = crop_provider.generate(req)
        if not isinstance(res, ProviderSuccess):
            if reserved > 0:
                ledger.release_without_spend(reserved)
            verifications.append({"page_number": pnum, "block_id": blk.block_id, "verdict": "provider_error"})
            continue

        _record_crop_spend(ledger, crop_provider, reserved, res.provenance or {}, pnum)
        _apply_zoom_comparison(
            blk,
            pnum,
            item["reasons"],
            res.data,
            verify_id=verify_id,
            reviewer_id=reviewer_id,
            now_iso=now_iso,
            corrections=corrections,
            verifications=verifications,
        )


def _arbitrate_boundary_targets(
    *,
    targets: list[dict[str, Any]],
    decision_provider: Any,
    crop_provider: Any,
    arbiter_prompt: str,
    ledger: CostLedger,
    verify_id: str,
    reviewer_id: str,
    now_iso: str,
    corrections: list[dict[str, Any]],
    verifications: list[dict[str, Any]],
) -> None:
    for item in targets:
        pnum = int(item["page_number"])
        prev_blk: BlockIR = item["prev_block"]
        cand_blk: BlockIR = item["candidate_block"]
        context = (
            f"Page {pnum} preceding block ({prev_blk.type}, align={prev_blk.alignment}): {prev_blk.plain_text}\n"
            f"Candidate block ({cand_blk.type}, align={cand_blk.alignment}): {cand_blk.plain_text}"
        )

        verdict = "continues_same_letter"
        if isinstance(decision_provider, GeminiProvider):
            try:
                reserved = ledger.acquire_slot(0.001)
            except CostCeilingExceeded:
                break
            dec_res = decision_provider.evaluate_decisions(
                context,
                {
                    "new_letter": {
                        "type": "noul",
                        "instructions": "Does a distinct new top-level letter begin at the candidate block (rather than an enclosure or continuation)?",
                    }
                },
            )
            if isinstance(dec_res, DecisionBatchResult) and "new_letter" in dec_res.answers:
                ans = dec_res.answers["new_letter"]
                ledger.release_and_record(
                    reserved,
                    stage="boundary_decision_api",
                    provider="gemini",
                    model=str(dec_res.provenance.get("model") or "gemini-decision-1"),
                    cost_usd=float(dec_res.provenance.get("cost_estimate_usd") or 0.0),
                    page_number=pnum,
                    input_tokens=int(dec_res.provenance.get("input_tokens") or 0),
                )
                if ans.verdict == "yes" and ans.score >= 0.65:
                    verdict = "starts_new_letter"
            else:
                ledger.release_without_spend(reserved)
                verdict = _fallback_vlm_boundary_check(context, arbiter_prompt, crop_provider)
        else:
            verdict = _fallback_vlm_boundary_check(context, arbiter_prompt, crop_provider)

        if verdict == "starts_new_letter":
            corrections.append(
                {
                    "correction_id": f"bnd-{verify_id}-{cand_blk.block_id}",
                    "page_number": pnum,
                    "block_id": cand_blk.block_id,
                    "block_id_hint": cand_blk.block_id,
                    "anchor_text": cand_blk.plain_text[:160],
                    "op": "set_transition",
                    "replacement_transition": "starts_new_letter",
                    "source": "machine",
                    "reviewer": reviewer_id,
                    "note": "Calibrated boundary arbitration confirmed start of new top-level letter.",
                    "created_at": now_iso,
                }
            )
        verifications.append({"page_number": pnum, "block_id": cand_blk.block_id, "verdict": verdict})


def _fallback_vlm_boundary_check(context: str, arbiter_prompt: str, provider: Any) -> str:
    arb_schema = {
        "type": "object",
        "required": ["verdict", "confidence", "rationale"],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["starts_new_letter", "continues_same_letter", "enclosure_inside_letter", "editorial_interlude"],
            },
            "confidence": {"type": "number"},
            "rationale": {"type": "string"},
        },
    }
    req = ProviderRequest(prompt=arbiter_prompt, schema_dict=arb_schema, dynamic_suffix=context)
    res = provider.generate(req)
    if isinstance(res, ProviderSuccess):
        return str(res.data.get("verdict") or "continues_same_letter")
    return "continues_same_letter"
