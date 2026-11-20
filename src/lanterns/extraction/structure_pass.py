"""Hierarchical Pass 2A/2B/2C structure pass for paper-faithful volume segmentation.

ROLE: Partition content BlockIR streams into sections (Pass 2A), compact structural-unit
tuples with optional one-level nested enclosures (Pass 2B), and multimodal boundary
verification via `gemini-decision-1` + `gemini-4-hydrogen` escalation (Pass 2C).
INPUTS: Post-wrap/post-join `BlockIR` sequence (or `run_dir` + `book_id`).
OUTPUTS: Schema-validated `structure_plan` (`ir/structure_auto.json`) with `BoundaryAnchor`s.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lanterns.core.atomic import atomic_write_json
from lanterns.core.config import BookConfig, load_prompt, validate_data
from lanterns.core.cost import CostCeilingExceeded, CostLedger
from lanterns.core.models import BlockIR, PageIR, make_boundary_anchor
from lanterns.core.paths import RepoPaths
from lanterns.core.pricing import (
    conservative_call_reservation_usd,
    ensure_priced_routes,
    estimate_cost_usd,
    parse_model_route,
)
from lanterns.extraction.line_wraps import apply_cross_page_prose_joins, resolve_page_ir_line_wraps
from lanterns.extraction.normalize import promote_page_symbol_callouts, propagate_page_folios
from lanterns.extraction.pdf_ingest import crop_normalized_bbox
from lanterns.extraction.pipeline import resolve_compile_pages
from lanterns.extraction.proof_overlay import apply_corrections_and_proof_overlay
from lanterns.providers.base import DecisionBatchResult, ProviderError, ProviderRequest, ProviderSuccess
from lanterns.providers.gemini import GeminiProvider
from lanterns.providers.router import build_provider

DEFAULT_PASS2A_MODEL = "gemini/gemini-3.8-flash"
DEFAULT_PASS2B_MODEL = "gemini/gemini-3.8-flash"
DEFAULT_DECISION_MODEL = "gemini/gemini-decision-1"
DEFAULT_ESCALATION_MODEL = "gemini/gemini-4-hydrogen"
DEFAULT_FALLBACK_MODEL = "anthropic/claude-sonnet-5-5"

SECTION_KINDS = ("frontmatter", "chapter", "epistolary_part", "appendix", "index", "backmatter")
UNIT_KINDS = ("letter", "letter_excerpt", "narrative", "editorial_bridge", "document", "apparatus")
CHILD_KINDS = ("enclosure", "letter_excerpt", "verse_insert")
PASS2C_VERDICTS = ("starts_new_letter", "starts_enclosure", "narrative_or_document", "continuation")
_QUOTE_OPEN_CHARS = ('"', "“", "'", "‘", "«", "„")

_PASS2A_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sections"],
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "title_plain", "start_block_id", "end_block_id"],
                "properties": {
                    "kind": {"type": "string", "enum": list(SECTION_KINDS)},
                    "title_plain": {"type": "string"},
                    "start_block_id": {"type": "string"},
                    "end_block_id": {"type": "string"},
                },
            },
        }
    },
}

_CORR_TUPLE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 2,
    "maxItems": 2,
    "prefixItems": [
        {"type": "string"},
        {"type": "string", "enum": ["person", "org"]},
    ],
}

_CHILD_TUPLE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 4,
    "maxItems": 4,
    "prefixItems": [
        {"type": "string", "enum": list(CHILD_KINDS)},
        {"type": "string"},
        {"type": "string"},
        {"type": ["string", "null"]},
    ],
}

_UNIT_TUPLE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 10,
    "maxItems": 10,
    "prefixItems": [
        {"type": "string", "enum": list(UNIT_KINDS)},
        {"type": "string"},
        {"type": "string"},
        {"type": ["string", "null"]},
        {"type": "array", "items": _CORR_TUPLE_SCHEMA},
        {"type": "array", "items": _CORR_TUPLE_SCHEMA},
        {"type": ["string", "null"]},
        {"type": ["string", "null"]},
        {"type": ["string", "null"]},
        {"type": "array", "items": _CHILD_TUPLE_SCHEMA},
    ],
}

_PASS2B_TUPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["units"],
    "properties": {
        "units": {"type": "array", "items": _UNIT_TUPLE_SCHEMA},
    },
}

_PASS2B_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["units"],
    "properties": {
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "start_block_id", "end_block_id"],
                "properties": {
                    "kind": {"type": "string", "enum": list(UNIT_KINDS)},
                    "start_block_id": {"type": "string"},
                    "end_block_id": {"type": "string"},
                    "label": {"type": ["string", "null"]},
                    "senders": {"type": "array", "items": _CORR_TUPLE_SCHEMA},
                    "recipients": {"type": "array", "items": _CORR_TUPLE_SCHEMA},
                    "place": {"type": ["string", "null"]},
                    "date_raw": {"type": ["string", "null"]},
                    "date_iso": {"type": ["string", "null"]},
                    "children": {"type": "array", "items": _CHILD_TUPLE_SCHEMA},
                },
            },
        }
    },
}

_PASS2C_ESCALATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "confidence", "rationale"],
    "properties": {
        "verdict": {"type": "string", "enum": list(PASS2C_VERDICTS)},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
}


def load_structure_auto(run_dir: str | Path | None) -> dict[str, Any] | None:
    """Load `<run_dir>/ir/structure_auto.json` if present."""
    if run_dir is None:
        return None
    cand = Path(run_dir) / "ir" / "structure_auto.json"
    if not cand.is_file():
        return None
    return json.loads(cand.read_text(encoding="utf-8"))


def prepare_structure_blocks(
    pages: list[PageIR] | None = None,
    *,
    book_id: str = "",
    active_run_id: str = "manual",
    run_dir: str | Path | None = None,
    repo_root: str | Path | None = None,
    corrections: list[dict[str, Any]] | None = None,
    proof_ledger: dict[str, Any] | None = None,
) -> tuple[list[BlockIR], Path | None]:
    """Run the deterministic pre-segmentation block pipeline matching `compile_collection_ast`."""
    raw_pages, resolved_run_dir = resolve_compile_pages(pages, run_dir, book_id, active_run_id, repo_root)
    overlaid, _ = apply_corrections_and_proof_overlay(
        raw_pages,
        book_id=book_id,
        repo_root=repo_root,
        corrections=corrections,
        proof_ledger=proof_ledger,
    )
    witness_dir = resolved_run_dir / "witnesses" if resolved_run_dir else None
    dehyphenated, _ = resolve_page_ir_line_wraps(overlaid, witness_dir=witness_dir)
    normalized = promote_page_symbol_callouts(propagate_page_folios(dehyphenated))
    return apply_cross_page_prose_joins(normalized, witness_dir=witness_dir), resolved_run_dir


def _format_skeleton_line(blk: BlockIR) -> str:
    txt = " ".join(blk.plain_text.split())
    cap = 110 if blk.type == "body" else 160
    if len(txt) > cap:
        txt = f"{txt[: cap - 3]}..."
    hl = f" h={blk.heading_level}" if blk.heading_level is not None else ""
    tr = f" trans={blk.epistolary_transition}" if blk.epistolary_transition != "none" else ""
    al = f" align={blk.alignment}" if blk.alignment != "left" else ""
    fol = blk.printed_folio or str(blk.page_number)
    return f"{blk.block_id} p={blk.page_number} fol={fol} type={blk.type}{hl}{al}{tr} | {txt}"


def build_section_skeleton(blocks: list[BlockIR]) -> str:
    """Build compact volume-level Pass 2A skeleton highlighting headings, TOC, and boundaries."""
    ep_indices = [
        i for i, b in enumerate(blocks) if b.type == "letter_header" or b.epistolary_transition == "starts_new_letter"
    ]
    first_last_ep = {ep_indices[0], ep_indices[-1]} if ep_indices else set()
    lines: list[str] = []
    for idx, blk in enumerate(blocks):
        is_edge = idx == 0 or idx == len(blocks) - 1
        is_struct = blk.type in {"toc_entry", "imprint"} or (
            blk.type == "heading" and blk.heading_level in {None, 1, 2}
        )
        is_index_edge = blk.type == "index_entry" and (
            idx == 0 or blocks[idx - 1].type != "index_entry" or idx == len(blocks) - 1 or blocks[idx + 1].type != "index_entry"
        )
        if is_edge or is_struct or is_index_edge or idx in first_last_ep:
            lines.append(_format_skeleton_line(blk))
    return "\n".join(lines)


def _is_compressible_body(blocks: list[BlockIR], idx: int) -> bool:
    if idx <= 0 or idx >= len(blocks) - 1:
        return False
    blk = blocks[idx]
    if blk.type != "body" or blk.epistolary_transition != "none":
        return False
    if blk.plain_text.lstrip().startswith(_QUOTE_OPEN_CHARS):
        return False
    if blocks[idx - 1].plain_text.rstrip().endswith(":"):
        return False
    return True


def build_unit_skeleton(sec_blocks: list[BlockIR]) -> list[str]:
    """Render Pass 2B skeleton lines, never omitting quoted or colon-preceded body blocks."""
    out: list[str] = []
    idx = 0
    while idx < len(sec_blocks):
        if _is_compressible_body(sec_blocks, idx):
            run_end = idx
            while run_end + 1 < len(sec_blocks) and _is_compressible_body(sec_blocks, run_end + 1):
                run_end += 1
            run_len = run_end - idx + 1
            if run_len >= 3:
                out.append(_format_skeleton_line(sec_blocks[idx]))
                out.append(f"[... {run_len - 2} interior body paragraphs omitted ({sec_blocks[idx + 1].block_id}..{sec_blocks[run_end - 1].block_id}) ...]")
                out.append(_format_skeleton_line(sec_blocks[run_end]))
                idx = run_end + 1
                continue
        out.append(_format_skeleton_line(sec_blocks[idx]))
        idx += 1
    return out


def _call_structured_llm(
    *,
    route_spec: str,
    fallback_spec: str | None,
    prompt_text: str,
    dynamic_suffix: str,
    schema_dict: dict[str, Any],
    fallback_schema_dict: dict[str, Any] | None,
    stage: str,
    ledger: CostLedger | None,
    provider_factory: Callable[[str], Any],
    repo_root: str | Path | None,
    image_paths: list[str] | None = None,
) -> tuple[dict[str, Any], str, int]:
    candidates: list[tuple[str, dict[str, Any]]] = [(route_spec, schema_dict)]
    if fallback_spec and parse_model_route(fallback_spec) != parse_model_route(route_spec):
        candidates.append((fallback_spec, fallback_schema_dict or schema_dict))

    last_err = "No structure provider succeeded"
    for spec, active_schema in candidates:
        prov_name, model_name = parse_model_route(spec)
        provider = provider_factory(spec)
        if prov_name == "gemini" and hasattr(provider, "max_retries"):
            provider.max_retries = 1
        tier_plans = (
            (("standard", 60_000),)
            if stage == "structure_pass2c_escalation" or prov_name != "gemini"
            else (("flex", 75_000), ("standard", 90_000))
        )
        for tier, timeout_ms in tier_plans:
            settings = {
                "service_tier": tier,
                "thinking_level": "low",
                "max_output_tokens": 65536,
                "timeout_ms": timeout_ms,
            }
            req = ProviderRequest(
                prompt=prompt_text,
                dynamic_suffix=dynamic_suffix,
                image_paths=list(image_paths or []),
                schema_dict=active_schema,
                settings=settings,
            )
            res, out_tok = _run_single_provider_call(
                provider=provider,
                prov_name=prov_name,
                model_name=model_name,
                req=req,
                stage=stage,
                ledger=ledger,
                repo_root=repo_root,
            )
            if isinstance(res, ProviderSuccess):
                return res.data, f"{prov_name}/{model_name}", out_tok
            last_err = res.message
    raise RuntimeError(f"Structure pass `{stage}` failed closed: {last_err}")


def _run_single_provider_call(
    *,
    provider: Any,
    prov_name: str,
    model_name: str,
    req: ProviderRequest,
    stage: str,
    ledger: CostLedger | None,
    repo_root: str | Path | None,
) -> tuple[ProviderSuccess | ProviderError, int]:
    reserved = 0.0
    is_mock = getattr(provider, "name", prov_name) == "mock"
    if ledger is not None and not is_mock:
        est = conservative_call_reservation_usd(prov_name, model_name, root=str(repo_root) if repo_root else None)
        slot_cap = 0.015 if (prov_name == "gemini" and "flash" in model_name) else min(est, 0.04)
        reserved = ledger.acquire_slot(min(est, slot_cap))
    res = provider.generate(req)
    prov_meta = res.provenance or {}
    call_cost = _resolve_call_cost(prov_name, model_name, prov_meta, req.settings.get("service_tier"), repo_root)
    out_tok = int(prov_meta.get("output_tokens") or 0)
    if ledger is not None and not is_mock:
        if isinstance(res, ProviderSuccess) or call_cost > 0.0:
            ledger.release_and_record(
                reserved,
                stage=stage,
                provider=str(prov_meta.get("provider") or prov_name),
                model=str(prov_meta.get("model") or model_name),
                cost_usd=call_cost,
                input_tokens=int(prov_meta.get("input_tokens") or 0),
                cached_input_tokens=int(prov_meta.get("cached_input_tokens") or 0),
                output_tokens=out_tok,
                thoughts_tokens=int(prov_meta.get("thoughts_tokens") or 0),
                status="ok" if isinstance(res, ProviderSuccess) else f"error:{res.kind.value}",
            )
        else:
            ledger.release_without_spend(reserved)
    return res, out_tok


def _resolve_call_cost(
    prov_name: str,
    model_name: str,
    prov_meta: dict[str, Any],
    service_tier: Any,
    repo_root: str | Path | None,
) -> float:
    explicit = float(prov_meta.get("cost_estimate_usd") or 0.0)
    if explicit > 0.0:
        return explicit
    in_tok = int(prov_meta.get("input_tokens") or 0)
    out_tok = int(prov_meta.get("output_tokens") or 0)
    thought_tok = int(prov_meta.get("thoughts_tokens") or 0)
    if in_tok == 0 and out_tok == 0 and thought_tok == 0:
        return 0.0
    return estimate_cost_usd(
        prov_name,
        model_name,
        in_tok,
        out_tok,
        thought_tok,
        cached_input_tokens=int(prov_meta.get("cached_input_tokens") or 0),
        service_tier=str(service_tier) if service_tier else None,
        root=str(repo_root) if repo_root else None,
    )


def _snap_contiguous_spans(
    raw_spans: list[dict[str, Any]],
    blocks: list[BlockIR],
    *,
    default_kind: str,
    default_title: str,
) -> list[dict[str, Any]]:
    """Deterministically clamp & stitch raw `[start_block_id, end_block_id]` spans to 100% block coverage."""
    if not blocks:
        return []
    id_to_idx = {b.block_id: idx for idx, b in enumerate(blocks)}
    resolved: list[tuple[int, int, dict[str, Any]]] = []
    seen_starts: set[int] = set()
    for item in raw_spans:
        s_i = id_to_idx.get(str(item.get("start_block_id") or ""))
        e_i = id_to_idx.get(str(item.get("end_block_id") or ""))
        if s_i is not None and s_i not in seen_starts:
            seen_starts.add(s_i)
            resolved.append((s_i, max(s_i, e_i if e_i is not None else s_i), dict(item)))
    resolved.sort(key=lambda t: (t[0], t[1]))

    if not resolved:
        return [{"kind": default_kind, "title_plain": default_title, "start_idx": 0, "end_idx": len(blocks) - 1}]

    stitched: list[dict[str, Any]] = []
    if resolved[0][0] > 0:
        stitched.append({"kind": default_kind, "title_plain": default_title, "start_idx": 0, "end_idx": resolved[0][0] - 1})
    for s_i, _, item in resolved:
        if stitched and s_i <= stitched[-1]["start_idx"]:
            continue
        if stitched:
            stitched[-1]["end_idx"] = s_i - 1
        stitched.append({**item, "start_idx": s_i, "end_idx": len(blocks) - 1})
    return stitched


def run_pass_2a(
    blocks: list[BlockIR],
    *,
    prompt_text: str,
    pass2a_model: str = DEFAULT_PASS2A_MODEL,
    fallback_model: str | None = DEFAULT_FALLBACK_MODEL,
    ledger: CostLedger | None = None,
    provider_factory: Callable[[str], Any] = build_provider,
    repo_root: str | Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], int]:
    """Partition `blocks` into contiguous top-level `BookSection` spans (Pass 2A)."""
    if not blocks:
        return [], {}, 0
    page_set = {b.page_number for b in blocks}
    has_struct_markers = any(b.type in {"heading", "toc_entry"} for b in blocks)
    if len(page_set) <= 2 and not has_struct_markers:
        return (
            [{"kind": "epistolary_part", "title_plain": "Correspondence", "start_idx": 0, "end_idx": len(blocks) - 1}],
            {"pass2a": "local/trivial"},
            0,
        )

    skeleton = build_section_skeleton(blocks)
    dyn = f"## Task: Pass 2A Section Partition\nPartition all blocks into contiguous sections.\n\n{skeleton}"
    data, used_model, out_tok = _call_structured_llm(
        route_spec=pass2a_model,
        fallback_spec=fallback_model,
        prompt_text=prompt_text,
        dynamic_suffix=dyn,
        schema_dict=_PASS2A_SCHEMA,
        fallback_schema_dict=_PASS2A_SCHEMA,
        stage="structure_pass2a",
        ledger=ledger,
        provider_factory=provider_factory,
        repo_root=repo_root,
    )
    snapped = _snap_contiguous_spans(
        list(data.get("sections") or []),
        blocks,
        default_kind="epistolary_part",
        default_title="Correspondence",
    )
    return snapped, {"pass2a": used_model}, out_tok


def _split_section_subbatches(
    sec_blocks: list[BlockIR],
    max_openers: int = 160,
    max_lines: int = 420,
) -> list[list[BlockIR]]:
    """Split huge sections strictly on letter_header / starts_new_letter boundaries."""
    skeleton = build_unit_skeleton(sec_blocks)
    opener_count = sum(
        1 for b in sec_blocks if b.epistolary_transition != "none" or b.type in {"letter_header", "salutation", "dateline", "heading"}
    )
    if opener_count <= max_openers and len(skeleton) <= max_lines:
        return [sec_blocks]

    batches: list[list[BlockIR]] = []
    start_i = 0
    cur_openers = 0
    for idx, blk in enumerate(sec_blocks):
        if blk.epistolary_transition != "none" or blk.type in {"letter_header", "salutation", "dateline"}:
            cur_openers += 1
        is_clean_split = blk.epistolary_transition == "starts_new_letter" or blk.type == "letter_header"
        span_len = idx - start_i
        if idx > start_i and is_clean_split and (cur_openers >= 120 or span_len >= 320):
            batches.append(sec_blocks[start_i:idx])
            start_i = idx
            cur_openers = 1
    if start_i < len(sec_blocks):
        batches.append(sec_blocks[start_i:])
    return batches


def _normalize_corrs(raw_list: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in raw_list or []:
        if isinstance(item, list) and item:
            name = str(item[0] or "").strip()
            kind = str(item[1] if len(item) > 1 else "person").strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            kind = str(item.get("kind") or "person").strip()
        else:
            continue
        if name and name.casefold() != "unknown":
            out.append({"name": name, "kind": kind if kind in {"person", "org"} else "person"})
    return out


def _opt_str(val: Any) -> str | None:
    return str(val) if val else None


def _normalize_raw_unit(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, list) and len(raw) >= 3:
        kind = str(raw[0] or "letter")
        return {
            "kind": kind if kind in UNIT_KINDS else "letter",
            "start_block_id": str(raw[1] or ""),
            "end_block_id": str(raw[2] or ""),
            "label": _opt_str(raw[3]) if len(raw) > 3 else None,
            "senders": _normalize_corrs(raw[4] if len(raw) > 4 else []),
            "recipients": _normalize_corrs(raw[5] if len(raw) > 5 else []),
            "place": _opt_str(raw[6]) if len(raw) > 6 else None,
            "date_raw": _opt_str(raw[7]) if len(raw) > 7 else None,
            "date_iso": _opt_str(raw[8]) if len(raw) > 8 else None,
            "children_raw": list(raw[9]) if len(raw) > 9 and isinstance(raw[9], list) else [],
        }
    if isinstance(raw, dict):
        kind = str(raw.get("kind") or "letter")
        return {
            "kind": kind if kind in UNIT_KINDS else "letter",
            "start_block_id": str(raw.get("start_block_id") or ""),
            "end_block_id": str(raw.get("end_block_id") or ""),
            "label": _opt_str(raw.get("label")),
            "senders": _normalize_corrs(raw.get("senders")),
            "recipients": _normalize_corrs(raw.get("recipients")),
            "place": _opt_str(raw.get("place")),
            "date_raw": _opt_str(raw.get("date_raw")),
            "date_iso": _opt_str(raw.get("date_iso")),
            "children_raw": list(raw.get("children") or []),
        }
    return None


def _snap_children_to_unit(
    raw_children: list[Any],
    unit_blocks: list[BlockIR],
    child_seq_start: int,
) -> tuple[list[dict[str, Any]], int]:
    id_to_idx = {b.block_id: i for i, b in enumerate(unit_blocks)}
    spans: list[tuple[int, int, str, str | None]] = []
    for rc in raw_children:
        if isinstance(rc, list) and len(rc) >= 3:
            ckind, s_id, e_id = str(rc[0] or "enclosure"), str(rc[1] or ""), str(rc[2] or "")
            title = str(rc[3]) if len(rc) > 3 and rc[3] else None
        elif isinstance(rc, dict):
            ckind, s_id, e_id = str(rc.get("kind") or "enclosure"), str(rc.get("start_block_id") or ""), str(rc.get("end_block_id") or "")
            title = str(rc["title"]) if rc.get("title") else None
        else:
            continue
        s_i = id_to_idx.get(s_id)
        e_i = id_to_idx.get(e_id)
        if s_i is not None:
            eff_e = max(s_i, e_i if e_i is not None else s_i)
            spans.append((s_i, min(eff_e, len(unit_blocks) - 1), ckind if ckind in CHILD_KINDS else "enclosure", title))

    spans.sort(key=lambda t: (t[0], t[1]))
    children_out: list[dict[str, Any]] = []
    last_end = -1
    seq = child_seq_start
    for s_i, e_i, ckind, title in spans:
        if s_i <= last_end:
            continue
        last_end = e_i
        seq += 1
        span_blks = unit_blocks[s_i : e_i + 1]
        children_out.append(
            {
                "child_id": f"child-{seq:04d}",
                "kind": ckind,
                "start_block_id": span_blks[0].block_id,
                "end_block_id": span_blks[-1].block_id,
                "start_anchor": make_boundary_anchor(span_blks[0]).model_dump(mode="json"),
                "end_anchor": make_boundary_anchor(span_blks[-1]).model_dump(mode="json"),
                "block_ids": [b.block_id for b in span_blks],
                "title": title,
            }
        )
    return children_out, seq


def _run_pass2b_subbatch(
    *,
    sec_title: str,
    sec_kind: str,
    batch_blocks: list[BlockIR],
    prompt_text: str,
    pass2b_model: str,
    fallback_model: str | None,
    ledger: CostLedger | None,
    provider_factory: Callable[[str], Any],
    repo_root: str | Path | None,
) -> tuple[list[dict[str, Any]], str, int]:
    skel_lines = build_unit_skeleton(batch_blocks)
    dyn = (
        f"## Task: Pass 2B Section Unit Partition\nSection: `{sec_title}` (kind=`{sec_kind}`)\n\n"
        + "\n".join(skel_lines)
    )
    data, used_model, out_tok = _call_structured_llm(
        route_spec=pass2b_model,
        fallback_spec=fallback_model,
        prompt_text=prompt_text,
        dynamic_suffix=dyn,
        schema_dict=_PASS2B_TUPLE_SCHEMA,
        fallback_schema_dict=_PASS2B_OBJECT_SCHEMA,
        stage="structure_pass2b",
        ledger=ledger,
        provider_factory=provider_factory,
        repo_root=repo_root,
    )
    normalized = [u for item in (data.get("units") or []) if (u := _normalize_raw_unit(item)) is not None]
    default_kind = "apparatus" if sec_kind in {"frontmatter", "index", "backmatter"} else "letter"
    return _snap_contiguous_spans(normalized, batch_blocks, default_kind=default_kind, default_title=sec_title), used_model, out_tok


def _materialize_units(
    snapped: list[dict[str, Any]],
    sec_blocks: list[BlockIR],
    unit_seq_start: int,
    child_seq_start: int,
) -> tuple[list[dict[str, Any]], int, int]:
    units_out: list[dict[str, Any]] = []
    u_seq, c_seq = unit_seq_start, child_seq_start
    for sp in snapped:
        u_seq += 1
        ublks = sec_blocks[int(sp["start_idx"]) : int(sp["end_idx"]) + 1]
        children, c_seq = _snap_children_to_unit(list(sp.get("children_raw") or []), ublks, c_seq)
        units_out.append(
            {
                "unit_id": f"unit-{u_seq:04d}",
                "kind": str(sp.get("kind") or "letter"),
                "start_block_id": ublks[0].block_id,
                "end_block_id": ublks[-1].block_id,
                "start_anchor": make_boundary_anchor(ublks[0]).model_dump(mode="json"),
                "end_anchor": make_boundary_anchor(ublks[-1]).model_dump(mode="json"),
                "block_ids": [b.block_id for b in ublks],
                "children": children,
                "label": sp.get("label"),
                "title": sp.get("title"),
                "senders": list(sp.get("senders") or []),
                "recipients": list(sp.get("recipients") or []),
                "place": sp.get("place"),
                "date_raw": sp.get("date_raw"),
                "date_iso": sp.get("date_iso"),
                "boundary_confidence": "high",
            }
        )
    return units_out, u_seq, c_seq


def _find_boundary_candidates(
    blocks: list[BlockIR],
    units: list[dict[str, Any]],
    *,
    max_boundaries: int,
) -> list[dict[str, Any]]:
    idx_by_id = {b.block_id: i for i, b in enumerate(blocks)}
    unit_by_start = {u["start_block_id"]: u for u in units}
    child_starts = {c["start_block_id"] for u in units for c in u.get("children") or []}
    cands: list[dict[str, Any]] = []

    for idx, blk in enumerate(blocks):
        bid = blk.block_id
        in_units_start = bid in unit_by_start
        is_enc = blk.epistolary_transition == "starts_enclosure" or bid in child_starts
        is_missed_p1 = blk.epistolary_transition == "starts_new_letter" and not in_units_start and idx > 0
        u = unit_by_start.get(bid)
        is_unanchored_split = (
            u is not None
            and idx > 0
            and u.get("kind") == "letter"
            and blk.type in {"body", "editorial"}
            and blk.epistolary_transition == "none"
        )
        if is_enc or is_missed_p1 or is_unanchored_split:
            reason = "enclosure" if is_enc else ("missed_p1" if is_missed_p1 else "unanchored_split")
            cands.append({"block_idx": idx_by_id[bid], "block": blk, "reason": reason, "priority": 0 if is_enc else 1})

    cands.sort(key=lambda c: (int(c["priority"]), int(c["block_idx"])))
    return cands[:max_boundaries]


def _resolve_page_render_or_pdf(
    page_num: int,
    run_dir: Path | None,
    source_pdf: Path | None,
) -> Path | None:
    if run_dir is not None:
        for sub in ("renders", "evidence/renders"):
            cand = run_dir / sub / f"page_{page_num:04d}.png"
            if cand.is_file():
                return cand
    return source_pdf if (source_pdf is not None and source_pdf.is_file()) else None


def _crop_boundary_strip(
    blk: BlockIR,
    *,
    run_dir: Path | None,
    source_pdf: Path | None,
    crop_dir: Path,
) -> str | None:
    src = _resolve_page_render_or_pdf(blk.page_number, run_dir, source_pdf)
    if src is None or len(blk.bbox) != 4:
        return None
    y0 = max(0, int(blk.bbox[1]) - 140)
    y1 = min(1000, int(blk.bbox[3]) + 140)
    out_p = crop_dir / f"strip_{blk.block_id}.png"
    crop_normalized_bbox(src, [40, y0, 960, max(y0 + 20, y1)], out_p, page_number=blk.page_number, dpi=200, pad_norm=0)
    return str(out_p) if out_p.is_file() else None


def _adjudicate_single_boundary(
    *,
    cand: dict[str, Any],
    blocks: list[BlockIR],
    decision_provider: Any,
    decision_model: str,
    escalation_model: str,
    fallback_model: str | None,
    run_dir: Path | None,
    source_pdf: Path | None,
    crop_dir: Path,
    ledger: CostLedger | None,
    provider_factory: Callable[[str], Any],
    repo_root: str | Path | None,
) -> tuple[str | None, bool, bool]:
    idx = int(cand["block_idx"])
    blk: BlockIR = cand["block"]
    prev_blk = blocks[idx - 1] if idx > 0 else None
    next_blks = blocks[idx + 1 : idx + 3]
    ctx_lines = ([_format_skeleton_line(prev_blk)] if prev_blk else []) + [_format_skeleton_line(blk)] + [
        _format_skeleton_line(nb) for nb in next_blks
    ]
    context = "Boundary Context:\n" + "\n".join(ctx_lines)
    strip_path = _crop_boundary_strip(blk, run_dir=run_dir, source_pdf=source_pdf, crop_dir=crop_dir)
    img_args = [strip_path] if strip_path else None

    _, dec_mod_name = parse_model_route(decision_model)
    reserved = 0.0
    if ledger is not None and getattr(decision_provider, "name", "") != "mock":
        reserved = ledger.acquire_slot(0.002)
    dec_res = decision_provider.evaluate_decisions(
        context,
        {
            "boundary": {
                "type": "choice",
                "instructions": "Inspect the scan strip and context. Classify the structural role at the target boundary block.",
                "criteria": {
                    "starts_new_letter": "Begins a distinct new top-level letter, postcard, or dispatch.",
                    "starts_enclosure": "Begins an inner letter or document enclosed inside an active parent letter.",
                    "narrative_or_document": "Begins non-letter biographical narrative, editorial bridge, or standalone report.",
                    "continuation": "Continues the current unit (including postscripts, diary sub-dates, and bottom datelines).",
                },
            }
        },
        image_paths=img_args,
        model=dec_mod_name,
    )
    if isinstance(dec_res, DecisionBatchResult) and "boundary" in dec_res.answers:
        ans = dec_res.answers["boundary"]
        if ledger is not None and getattr(decision_provider, "name", "") != "mock":
            cost_val = _resolve_call_cost("gemini", dec_mod_name, dec_res.provenance, None, repo_root)
            ledger.release_and_record(
                reserved,
                stage="structure_pass2c_decision",
                provider="gemini",
                model=dec_mod_name,
                cost_usd=cost_val,
                page_number=blk.page_number,
                input_tokens=int(dec_res.provenance.get("input_tokens") or 0),
            )
        if float(ans.confidence) >= 0.85 and ans.verdict in PASS2C_VERDICTS:
            return ans.verdict, True, False
    elif ledger is not None and getattr(decision_provider, "name", "") != "mock":
        ledger.release_without_spend(reserved)

    esc_prompt = (
        "Classify the epistolary boundary at the target block into one of: "
        "`starts_new_letter`, `starts_enclosure`, `narrative_or_document`, `continuation`."
    )
    esc_data, _, _ = _call_structured_llm(
        route_spec=escalation_model,
        fallback_spec=fallback_model,
        prompt_text=esc_prompt,
        dynamic_suffix=context,
        schema_dict=_PASS2C_ESCALATION_SCHEMA,
        fallback_schema_dict=_PASS2C_ESCALATION_SCHEMA,
        stage="structure_pass2c_escalation",
        ledger=ledger,
        provider_factory=provider_factory,
        repo_root=repo_root,
        image_paths=img_args,
    )
    verdict = str(esc_data.get("verdict") or "continuation")
    return (verdict if verdict in PASS2C_VERDICTS else None), True, True


def _apply_boundary_verdict(
    sec_units: list[dict[str, Any]],
    sec_blocks: list[BlockIR],
    target_bid: str,
    verdict: str,
) -> list[dict[str, Any]]:
    idx_by_id = {b.block_id: i for i, b in enumerate(sec_blocks)}
    t_idx = idx_by_id.get(target_bid)
    if t_idx is None:
        return sec_units

    u_pos = next((ui for ui, u in enumerate(sec_units) if target_bid in u["block_ids"]), None)
    if u_pos is None:
        return sec_units
    unit = sec_units[u_pos]

    if verdict == "continuation" and target_bid == unit["start_block_id"] and u_pos > 0:
        prev = sec_units[u_pos - 1]
        merged_ids = [*prev["block_ids"], *unit["block_ids"]]
        prev["block_ids"] = merged_ids
        prev["end_block_id"] = unit["end_block_id"]
        prev["end_anchor"] = unit["end_anchor"]
        prev["children"] = [*prev.get("children", []), *unit.get("children", [])]
        prev["boundary_confidence"] = "adjudicated"
        return [*sec_units[:u_pos], *sec_units[u_pos + 1 :]]

    if verdict == "narrative_or_document" and target_bid == unit["start_block_id"]:
        unit["kind"] = "document" if len(unit["block_ids"]) > 2 else "narrative"
        unit["boundary_confidence"] = "adjudicated"
        return sec_units

    if verdict == "starts_enclosure":
        return _apply_enclosure_verdict(sec_units, sec_blocks, u_pos, target_bid)

    if verdict == "starts_new_letter" and target_bid != unit["start_block_id"]:
        split_at = unit["block_ids"].index(target_bid)
        left_blks = [sec_blocks[idx_by_id[bid]] for bid in unit["block_ids"][:split_at]]
        right_blks = [sec_blocks[idx_by_id[bid]] for bid in unit["block_ids"][split_at:]]
        left_unit = {
            **unit,
            "end_block_id": left_blks[-1].block_id,
            "end_anchor": make_boundary_anchor(left_blks[-1]).model_dump(mode="json"),
            "block_ids": [b.block_id for b in left_blks],
            "children": [c for c in unit.get("children", []) if set(c["block_ids"]) <= {b.block_id for b in left_blks}],
            "boundary_confidence": "adjudicated",
        }
        right_unit = {
            "unit_id": f"{unit['unit_id']}-b",
            "kind": "letter",
            "start_block_id": right_blks[0].block_id,
            "end_block_id": right_blks[-1].block_id,
            "start_anchor": make_boundary_anchor(right_blks[0]).model_dump(mode="json"),
            "end_anchor": make_boundary_anchor(right_blks[-1]).model_dump(mode="json"),
            "block_ids": [b.block_id for b in right_blks],
            "children": [],
            "label": None,
            "title": None,
            "senders": [],
            "recipients": [],
            "place": None,
            "date_raw": None,
            "date_iso": None,
            "boundary_confidence": "adjudicated",
        }
        return [*sec_units[:u_pos], left_unit, right_unit, *sec_units[u_pos + 1 :]]
    return sec_units


def _apply_enclosure_verdict(
    sec_units: list[dict[str, Any]],
    sec_blocks: list[BlockIR],
    u_pos: int,
    target_bid: str,
) -> list[dict[str, Any]]:
    idx_by_id = {b.block_id: i for i, b in enumerate(sec_blocks)}
    unit = sec_units[u_pos]
    if target_bid == unit["start_block_id"] and u_pos > 0:
        prev = sec_units[u_pos - 1]
        prev["block_ids"] = [*prev["block_ids"], *unit["block_ids"]]
        prev["end_block_id"] = unit["end_block_id"]
        prev["end_anchor"] = unit["end_anchor"]
        enc_blks = [sec_blocks[idx_by_id[bid]] for bid in unit["block_ids"]]
        child = _build_enclosure_child(enc_blks, f"child-enc-{len(prev.get('children', [])) + 1}")
        prev["children"] = [*prev.get("children", []), child]
        prev["boundary_confidence"] = "adjudicated"
        return [*sec_units[:u_pos], *sec_units[u_pos + 1 :]]

    if any(target_bid in c["block_ids"] for c in unit.get("children") or []):
        return sec_units

    pos_in_unit = unit["block_ids"].index(target_bid)
    has_explicit_end = any(
        sec_blocks[idx_by_id[bid]].epistolary_transition == "ends_enclosure"
        for bid in unit["block_ids"][pos_in_unit:]
    )
    end_pos = pos_in_unit
    target_type = sec_blocks[idx_by_id[target_bid]].type
    while end_pos + 1 < len(unit["block_ids"]):
        nxt_b = sec_blocks[idx_by_id[unit["block_ids"][end_pos + 1]]]
        if has_explicit_end:
            end_pos += 1
            if nxt_b.epistolary_transition == "ends_enclosure":
                break
        elif nxt_b.type in {"blockquote", "editorial"} and nxt_b.type == target_type:
            end_pos += 1
        else:
            break
    enc_blks = [sec_blocks[idx_by_id[bid]] for bid in unit["block_ids"][pos_in_unit : end_pos + 1]]
    child = _build_enclosure_child(enc_blks, f"child-enc-{len(unit.get('children', [])) + 1}")
    unit["children"] = [*unit.get("children", []), child]
    unit["boundary_confidence"] = "adjudicated"
    return sec_units


def _build_enclosure_child(enc_blks: list[BlockIR], child_id: str) -> dict[str, Any]:
    return {
        "child_id": child_id,
        "kind": "enclosure",
        "start_block_id": enc_blks[0].block_id,
        "end_block_id": enc_blks[-1].block_id,
        "start_anchor": make_boundary_anchor(enc_blks[0]).model_dump(mode="json"),
        "end_anchor": make_boundary_anchor(enc_blks[-1]).model_dump(mode="json"),
        "block_ids": [b.block_id for b in enc_blks],
        "title": None,
    }


def _resequence_section_units(sections_out: list[dict[str, Any]]) -> None:
    u_seq = 0
    for sec in sections_out:
        for unit in sec["units"]:
            u_seq += 1
            unit["unit_id"] = f"unit-{u_seq:04d}"


def _run_pass2b_sections(
    content_blocks: list[BlockIR],
    snapped_sections: list[dict[str, Any]],
    *,
    prompt_text: str,
    pass2b_model: str,
    fallback_model: str | None,
    ledger: CostLedger,
    provider_factory: Callable[[str], Any],
    repo_root: Path,
    max_concurrency: int,
    models_used: dict[str, str],
    max_out_tok: int,
) -> tuple[list[dict[str, Any]], int]:
    tasks: list[tuple[int, int, str, str, list[BlockIR]]] = []
    for sec_i, sp in enumerate(snapped_sections):
        sec_blks = content_blocks[int(sp["start_idx"]) : int(sp["end_idx"]) + 1]
        title_p = str(sp.get("title_plain") or f"Section {sec_i + 1}")
        skind = str(sp.get("kind") or "chapter")
        for sub_i, sub_blks in enumerate(_split_section_subbatches(sec_blks)):
            tasks.append((sec_i, sub_i, title_p, skind, sub_blks))

    subbatch_results: dict[tuple[int, int], tuple[list[dict[str, Any]], list[BlockIR], str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as pool:
        futs = {
            pool.submit(
                _run_pass2b_subbatch,
                sec_title=title_p,
                sec_kind=skind,
                batch_blocks=sub_blks,
                prompt_text=prompt_text,
                pass2b_model=pass2b_model,
                fallback_model=fallback_model,
                ledger=ledger,
                provider_factory=provider_factory,
                repo_root=repo_root,
            ): (sec_i, sub_i, sub_blks)
            for sec_i, sub_i, title_p, skind, sub_blks in tasks
        }
        for fut in as_completed(futs):
            sec_i, sub_i, sub_blks = futs[fut]
            snapped_u, used_m, out_tok = fut.result()
            max_out_tok = max(max_out_tok, out_tok)
            models_used.setdefault("pass2b", used_m)
            subbatch_results[(sec_i, sub_i)] = (snapped_u, sub_blks, used_m)

    sections_out: list[dict[str, Any]] = []
    u_seq, c_seq = 0, 0
    for sec_i, sp in enumerate(snapped_sections):
        sec_blks = content_blocks[int(sp["start_idx"]) : int(sp["end_idx"]) + 1]
        sec_units: list[dict[str, Any]] = []
        for key in sorted(k for k in subbatch_results if k[0] == sec_i):
            snapped_u, sub_blks, _ = subbatch_results[key]
            part_u, u_seq, c_seq = _materialize_units(snapped_u, sub_blks, u_seq, c_seq)
            sec_units.extend(part_u)
        title_p = str(sp.get("title_plain") or f"Section {sec_i + 1}")
        sections_out.append(
            {
                "section_id": f"sec-{sec_i + 1:02d}",
                "kind": str(sp.get("kind") or "chapter"),
                "title_plain": title_p,
                "title_tagged": title_p,
                "start_block_id": sec_blks[0].block_id,
                "end_block_id": sec_blks[-1].block_id,
                "start_anchor": make_boundary_anchor(sec_blks[0]).model_dump(mode="json"),
                "end_anchor": make_boundary_anchor(sec_blks[-1]).model_dump(mode="json"),
                "units": sec_units,
            }
        )
    return sections_out, max_out_tok


def _run_pass2c_verification(
    *,
    content_blocks: list[BlockIR],
    sections_out: list[dict[str, Any]],
    decision_model: str,
    escalation_model: str,
    fallback_model: str | None,
    max_boundaries: int,
    resolved_run_dir: Path | None,
    source_pdf: Path | None,
    crop_dir: Path,
    ledger: CostLedger,
    provider_factory: Callable[[str], Any],
    repo_root: Path,
    models_used: dict[str, str],
) -> tuple[int, int]:
    all_units = [u for sec in sections_out for u in sec["units"]]
    cands = _find_boundary_candidates(content_blocks, all_units, max_boundaries=max_boundaries)
    dec_provider = provider_factory(decision_model)
    if not (isinstance(dec_provider, GeminiProvider) or getattr(dec_provider, "name", "") == "mock"):
        return 0, 0

    verified, escalated = 0, 0
    for cand in cands:
        try:
            verdict, did_verify, did_esc = _adjudicate_single_boundary(
                cand=cand,
                blocks=content_blocks,
                decision_provider=dec_provider,
                decision_model=decision_model,
                escalation_model=escalation_model,
                fallback_model=fallback_model,
                run_dir=resolved_run_dir,
                source_pdf=source_pdf,
                crop_dir=crop_dir,
                ledger=ledger,
                provider_factory=provider_factory,
                repo_root=repo_root,
            )
        except CostCeilingExceeded:
            break
        if did_verify:
            verified += 1
            models_used.setdefault("pass2c_decision", decision_model)
        if did_esc:
            escalated += 1
            models_used.setdefault("pass2c_escalation", escalation_model)
        if verdict:
            bid = cand["block"].block_id
            for sec in sections_out:
                sec_ids = {u_bid for u in sec["units"] for u_bid in u["block_ids"]}
                if bid in sec_ids:
                    sec_blks = [b for b in content_blocks if b.block_id in sec_ids]
                    sec["units"] = _apply_boundary_verdict(sec["units"], sec_blks, bid, verdict)
                    break
    return verified, escalated


def run_structure_pass(
    *,
    pages: list[PageIR] | None = None,
    book_id: str = "book",
    active_run_id: str = "manual",
    run_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    source_pdf: str | Path | None = None,
    cost_ceiling_usd: float = 0.25,
    max_concurrency: int = 4,
    max_boundaries: int = 40,
    skip_decisions: bool = False,
    pass2a_model: str = DEFAULT_PASS2A_MODEL,
    pass2b_model: str = DEFAULT_PASS2B_MODEL,
    decision_model: str = DEFAULT_DECISION_MODEL,
    escalation_model: str = DEFAULT_ESCALATION_MODEL,
    fallback_model: str | None = DEFAULT_FALLBACK_MODEL,
    ledger: CostLedger | None = None,
    provider_factory: Callable[[str], Any] = build_provider,
) -> dict[str, Any]:
    """Execute Pass 2A -> Pass 2B -> Pass 2C and write schema-validated `structure_auto.json`."""
    repo = RepoPaths.from_root(repo_root)
    routes = [r for r in (pass2a_model, pass2b_model, decision_model, escalation_model, fallback_model) if r]
    non_mock_routes = [r for r in routes if parse_model_route(r)[0] != "mock"]
    if non_mock_routes:
        ensure_priced_routes(non_mock_routes, root=str(repo.root))

    content_blocks, resolved_run_dir = prepare_structure_blocks(
        pages,
        book_id=book_id,
        active_run_id=active_run_id,
        run_dir=run_dir,
        repo_root=repo.root,
    )
    target_out = (
        Path(out_path)
        if out_path is not None
        else ((resolved_run_dir / "ir" / "structure_auto.json") if resolved_run_dir else repo.structure_auto_path(book_id, active_run_id))
    )
    eff_ledger = ledger or CostLedger(target_out.parent / "structure_cost_ledger.jsonl", ceiling_usd=cost_ceiling_usd)
    spend_before = eff_ledger.total_spent_usd
    prompt_text = load_prompt("structure_pass_v1", root=str(repo.root))

    if not content_blocks:
        empty_plan = {
            "schema_version": "1.0.0",
            "book_id": book_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "decisions_verified": 0,
            "escalated_count": 0,
            "max_call_output_tokens": 0,
            "models_used": {},
            "total_cost_usd": 0.0,
            "sections": [],
        }
        atomic_write_json(target_out, empty_plan)
        return empty_plan

    snapped_sections, models_used, max_out_tok = run_pass_2a(
        content_blocks,
        prompt_text=prompt_text,
        pass2a_model=pass2a_model,
        fallback_model=fallback_model,
        ledger=eff_ledger,
        provider_factory=provider_factory,
        repo_root=repo.root,
    )
    sections_out, max_out_tok = _run_pass2b_sections(
        content_blocks,
        snapped_sections,
        prompt_text=prompt_text,
        pass2b_model=pass2b_model,
        fallback_model=fallback_model,
        ledger=eff_ledger,
        provider_factory=provider_factory,
        repo_root=repo.root,
        max_concurrency=max_concurrency,
        models_used=models_used,
        max_out_tok=max_out_tok,
    )
    decisions_verified, escalated_count = (
        (0, 0)
        if skip_decisions
        else _run_pass2c_verification(
            content_blocks=content_blocks,
            sections_out=sections_out,
            decision_model=decision_model,
            escalation_model=escalation_model,
            fallback_model=fallback_model,
            max_boundaries=max_boundaries,
            resolved_run_dir=resolved_run_dir,
            source_pdf=_resolve_pdf_hint(book_id, source_pdf, repo),
            crop_dir=target_out.parent / "_boundary_strips",
            ledger=eff_ledger,
            provider_factory=provider_factory,
            repo_root=repo.root,
            models_used=models_used,
        )
    )
    _resequence_section_units(sections_out)
    eff_ledger.flush()
    plan: dict[str, Any] = {
        "schema_version": "1.0.0",
        "book_id": book_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decisions_verified": decisions_verified,
        "escalated_count": escalated_count,
        "max_call_output_tokens": max_out_tok,
        "models_used": models_used,
        "total_cost_usd": round(max(0.0, eff_ledger.total_spent_usd - spend_before), 6),
        "sections": sections_out,
    }
    validate_data("structure_plan", plan, root=str(repo.root))
    atomic_write_json(target_out, plan)
    return plan


def _resolve_pdf_hint(book_id: str, source_pdf: str | Path | None, repo: RepoPaths) -> Path | None:
    if source_pdf:
        return Path(source_pdf)
    toml_p = repo.book_dir(book_id) / "book.toml"
    if not toml_p.is_file():
        return None
    return BookConfig.load(book_id, paths=repo).resolve_source_pdf_path(paths=repo)


def has_live_gemini_credentials() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())
