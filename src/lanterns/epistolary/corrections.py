"""ROLE: Book-scoped human-first corrections overlay, proof ledger, and proposal intake.
INPUTS: PageIR list, books/<book_id>/corrections/*.json, proof_ledger.json, scan_checks/*.json.
OUTPUTS: Corrected PageIR list, proof summary metrics, updated proof_ledger.json and corrections.
INVARIANT: Human-proofed page text (p1_proofed or verified) is sacred and never overwritten by AI runs.
"""

from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from lanterns.core.atomic import atomic_write_json
from lanterns.core.config import validate_data
from lanterns.core.hashes import sha256_bytes, sha256_text
from lanterns.core.models import (
     FURNITURE_BLOCK_TYPES,
    BlockAlignment,
    BlockIR,
    BlockType,
    EpistolaryTransition,
    PageIR,
    TableCell,
)
from lanterns.core.paths import RepoPaths
from lanterns.core.tags import parse_tagged_text
from lanterns.extraction.line_wraps import resolve_page_ir_line_wraps

LEDGER_VERSION = "proof-ledger-v2"
CORRECTION_VERSION = "correction-v2"
HUMAN_PROTECTED_STATUSES = frozenset({"p1_proofed", "verified"})
HUMAN_SIGNOFF_STATUSES = frozenset({"p1_proofed", "verified", "needs_review"})


def _validate_if_schema_present(schema_name: str, payload: Any, repo: RepoPaths) -> None:
    schema_file = repo.schemas_dir / f"{schema_name}.schema.json"
    if schema_file.is_file():
        validate_data(schema_name, payload, root=str(repo.root))


def compute_page_text_sha256(blocks: list[BlockIR]) -> str:
    """Compute deterministic SHA-256 over a page's non-noise blocks."""
    lines = [
        f"{blk.type}:{blk.alignment}:{blk.epistolary_transition}:{blk.tagged_text}"
        for blk in blocks
        if blk.type not in {"scanner_residue", "noise"}
    ]
    return sha256_text("\n".join(lines))


def is_human_protected_page(record: dict[str, Any] | None) -> bool:
    """Return True when a page carries sacred human P1 or P2 proofreading sign-off."""
    if not record:
        return False
    status = str(record.get("status") or "")
    method = str(record.get("signoff_method") or "human")
    return status in HUMAN_PROTECTED_STATUSES and method == "human"


def is_p2_human_verified_page(record: dict[str, Any] | None) -> bool:
    """Return True only when P1 and P2 were completed by two distinct human reviewers."""
    if not is_human_protected_page(record) or str((record or {}).get("status")) != "verified":
        return False
    rec = record or {}
    p1 = str(rec.get("p1_reviewer_id") or "").strip()
    p2 = str(rec.get("p2_verifier_id") or rec.get("reviewer") or "").strip()
    return bool(p1 and p2 and p1.casefold() != p2.casefold())


def load_book_corrections(book_id: str, *, repo_root: str | Path | None = None) -> list[dict[str, Any]]:
    """Load all anchor-matched corrections from `books/<book_id>/corrections/*.json`."""
    if not book_id:
        return []
    repo = RepoPaths.from_root(repo_root)
    corr_dir = repo.corrections_dir(book_id)
    if not corr_dir.is_dir():
        return []
    merged: list[dict[str, Any]] = []
    for path in sorted(corr_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        _validate_if_schema_present("correction_v2", doc, repo)
        doc_gen = doc.get("generated_by")
        for raw_item in doc.get("corrections") or []:
            item = dict(raw_item)
            if doc_gen and item.get("source") != "human":
                item["source"] = "machine"
            merged.append(item)
    return merged


def save_book_corrections(
    book_id: str,
    corrections: list[dict[str, Any]],
    *,
    filename: str = "canonical.json",
    repo_root: str | Path | None = None,
) -> Path:
    """Persist book-scoped corrections atomically."""
    repo = RepoPaths.from_root(repo_root)
    payload: dict[str, Any] = {
        "version": CORRECTION_VERSION,
        "book_id": book_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "corrections": corrections,
    }
    _validate_if_schema_present("correction_v2", payload, repo)
    return atomic_write_json(repo.corrections_dir(book_id) / filename, payload)


def _load_book_json(
    book_id: str,
    filename: str,
    schema_name: str | None,
    default: dict[str, Any],
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    if not book_id:
        return dict(default)
    repo = RepoPaths.from_root(repo_root)
    path = repo.book_dir(book_id) / filename
    if not path.is_file():
        return dict(default)
    data = json.loads(path.read_text(encoding="utf-8"))
    if schema_name:
        _validate_if_schema_present(schema_name, data, repo)
    return dict(data)


def load_proof_ledger(book_id: str, *, repo_root: str | Path | None = None) -> dict[str, Any]:
    """Load `books/<book_id>/proof_ledger.json` or return an empty ledger envelope."""
    return _load_book_json(
        book_id,
        "proof_ledger.json",
        "proof_ledger",
        {"version": LEDGER_VERSION, "book_id": book_id, "pages": {}},
        repo_root,
    )


def save_proof_ledger(
    book_id: str,
    ledger: dict[str, Any],
    *,
    repo_root: str | Path | None = None,
) -> Path:
    """Validate and atomically write `books/<book_id>/proof_ledger.json`."""
    repo = RepoPaths.from_root(repo_root)
    out = {
        "version": LEDGER_VERSION,
        "book_id": book_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "pages": dict(ledger.get("pages") or {}),
    }
    _validate_if_schema_present("proof_ledger", out, repo)
    return atomic_write_json(repo.book_dir(book_id) / "proof_ledger.json", out)


def load_letter_map(book_id: str, *, repo_root: str | Path | None = None) -> dict[str, Any]:
    """Load canonical `books/<book_id>/letter_map.json` overrides if present."""
    return _load_book_json(book_id, "letter_map.json", "letter_map", {}, repo_root)


def save_letter_map(
    book_id: str,
    letter_map: dict[str, Any],
    *,
    repo_root: str | Path | None = None,
) -> Path:
    """Validate and atomically save `books/<book_id>/letter_map.json`."""
    repo = RepoPaths.from_root(repo_root)
    payload = {
        "schema_version": str(letter_map.get("schema_version") or "1.0.0"),
        "book_id": book_id,
        "force_split_before_block_ids": list(letter_map.get("force_split_before_block_ids") or []),
        "forbid_split_before_block_ids": list(letter_map.get("forbid_split_before_block_ids") or []),
        "enclosure_block_ranges": list(letter_map.get("enclosure_block_ranges") or []),
        "explicit_letters": list(letter_map.get("explicit_letters") or []),
        "notes": letter_map.get("notes"),
    }
    _validate_if_schema_present("letter_map", payload, repo)
    return atomic_write_json(repo.book_dir(book_id) / "letter_map.json", payload)


def load_chapter_map(book_id: str, *, repo_root: str | Path | None = None) -> dict[str, Any]:
    """Load optional `books/<book_id>/chapter_map.json` section boundaries."""
    return _load_book_json(book_id, "chapter_map.json", None, {}, repo_root)


def _locate_target_block_index(blocks: list[BlockIR], corr: dict[str, Any]) -> int | None:
    target_id = str(corr.get("block_id") or corr.get("block_id_hint") or "").strip()
    anchor = str(corr.get("anchor_text") or "").strip()
    if target_id:
        for idx, blk in enumerate(blocks):
            if blk.block_id == target_id:
                if not anchor or anchor in blk.plain_text:
                    return idx
                head_slice = blk.plain_text[: len(anchor) + 40]
                if max(
                    difflib.SequenceMatcher(None, anchor, head_slice).ratio(),
                    difflib.SequenceMatcher(None, anchor, blk.plain_text).ratio(),
                ) >= 0.50:
                    return idx
    if not anchor:
        return None
    for idx, blk in enumerate(blocks):
        if anchor in blk.plain_text:
            return idx
    best_idx: int | None = None
    best_ratio = 0.0
    for idx, blk in enumerate(blocks):
        ratio = difflib.SequenceMatcher(None, anchor, blk.plain_text).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = idx
    return best_idx if best_ratio >= 0.65 else None


_FIELD_REPLACEMENT_MAP: tuple[tuple[str, str], ...] = (
    ("replacement_type", "type"),
    ("replacement_alignment", "alignment"),
    ("replacement_transition", "epistolary_transition"),
    ("replacement_note_label", "note_label"),
    ("replacement_line_breaks_preserved", "line_breaks_preserved"),
)


def _update_common_fields(blk: BlockIR, corr: dict[str, Any]) -> BlockIR:
    updates = {
        dst_key: corr[src_key]
        for src_key, dst_key in _FIELD_REPLACEMENT_MAP
        if corr.get(src_key) is not None
    }
    if corr.get("replacement_bbox"):
        updates["bbox"] = [int(v) for v in corr["replacement_bbox"]]
    return blk.model_copy(update=updates) if updates else blk


def _apply_split_or_merge(blocks: list[BlockIR], idx: int, corr: dict[str, Any]) -> list[BlockIR]:
    op = str(corr["op"])
    target = blocks[idx]
    if op == "split_block":
        specs = corr.get("split_blocks") or []
        parts: list[BlockIR] = []
        for s_idx, spec in enumerate(specs, start=1):
            raw_txt = str(spec.get("tagged_text") or spec.get("text") or "")
            tagged, plain, spans = parse_tagged_text(raw_txt)
            updates: dict[str, Any] = {
                "block_id": str(spec.get("block_id") or f"{target.block_id}-s{s_idx}"),
                "type": cast(BlockType, str(spec.get("type") or target.type)),
                "alignment": cast(BlockAlignment, str(spec.get("alignment") or target.alignment)),
                "epistolary_transition": cast(
                    EpistolaryTransition, str(spec.get("epistolary_transition") or "none")
                ),
                "tagged_text": tagged,
                "plain_text": plain,
                "spans": spans,
            }
            if "note_label" in spec:
                updates["note_label"] = spec.get("note_label")
            if "line_breaks_preserved" in spec:
                updates["line_breaks_preserved"] = bool(spec["line_breaks_preserved"])
            parts.append(target.model_copy(update=updates))
        return [*blocks[:idx], *parts, *blocks[idx + 1 :]] if parts else list(blocks)

    if idx + 1 >= len(blocks):
        return list(blocks)
    nxt = blocks[idx + 1]
    sep = "\n" if target.line_breaks_preserved else " "
    merged_tagged, merged_plain, merged_spans = parse_tagged_text(
        f"{target.tagged_text}{sep}{nxt.tagged_text}".strip()
    )
    merged_blk = target.model_copy(
        update={
            "tagged_text": merged_tagged,
            "plain_text": merged_plain,
            "spans": merged_spans,
            "continues_to_next": nxt.continues_to_next,
        }
    )
    return [*blocks[:idx], merged_blk, *blocks[idx + 2 :]]


def _apply_single_op(blocks: list[BlockIR], idx: int, corr: dict[str, Any]) -> list[BlockIR]:
    op = str(corr["op"])
    if op in {"split_block", "merge_with_next"}:
        updated_list = _apply_split_or_merge(blocks, idx, corr)
    else:
        blk = _update_common_fields(blocks[idx], corr)
        if op == "set_text":
            tagged, plain, spans = parse_tagged_text(str(corr.get("replacement_tagged_text") or ""))
            blk = blk.model_copy(update={"tagged_text": tagged, "plain_text": plain, "spans": spans})
        elif op == "set_note_label":
            blk = blk.model_copy(update={"note_label": corr.get("replacement_note_label")})
        elif op == "set_cells":
            raw_cells = corr.get("replacement_cells") or []
            cells = [
                TableCell(
                    text=parse_tagged_text(str(c.get("text", "") if isinstance(c, dict) else c))[0],
                    colspan=int(c.get("colspan", 1)) if isinstance(c, dict) else 1,
                    rowspan=int(c.get("rowspan", 1)) if isinstance(c, dict) else 1,
                )
                for c in raw_cells
            ]
            joined = " | ".join(c.text for c in cells)
            tagged, plain, spans = parse_tagged_text(joined)
            blk = blk.model_copy(
                update={"cells": cells, "tagged_text": tagged, "plain_text": plain, "spans": spans}
            )
        elif op == "drop_residue":
            blk = blk.model_copy(update={"type": "scanner_residue"})
        updated_list = [*blocks[:idx], blk, *blocks[idx + 1 :]]

    return [b.model_copy(update={"reading_order": order_i}) for order_i, b in enumerate(updated_list, start=1)]


def _restore_human_snapshot_if_present(
    page: PageIR,
    page_rec: dict[str, Any] | None,
) -> tuple[PageIR, set[str], bool]:
    if not is_human_protected_page(page_rec):
        return page, set(), False
    rec = page_rec or {}
    att = rec.get("attestation") or {}
    rec_scan = str(rec.get("source_page_sha256") or "")
    if rec_scan and page.source_page_sha256 and rec_scan != page.source_page_sha256:
        return page, set(), False
    baked_ids = {str(cid) for cid in (att.get("applied_correction_ids") or [])}
    snap_raw = att.get("accepted_blocks")
    if not isinstance(snap_raw, list) or (not snap_raw and page.blocks):
        return page, baked_ids, True
    restored_blocks = [BlockIR.model_validate(item) for item in snap_raw]
    restored_page = page.model_copy(
        update={
            "blocks": restored_blocks,
            "failed_closed": False,
            "failure_reason": None,
        }
    )
    return restored_page, baked_ids, True


def _build_proof_summary(
    pages: list[PageIR],
    ledger_pages: dict[str, Any],
    applied: int,
    unmatched: int,
    skipped_human: int,
) -> dict[str, Any]:
    counts = {
        "unproofed": 0,
        "p1_proofed": 0,
        "verified": 0,
        "machine_reread": 0,
        "needs_review": 0,
    }
    confirmed_block_ids: set[str] = set()
    for page in pages:
        rec = ledger_pages.get(str(page.page_number)) or {}
        status = str(rec.get("status") or "unproofed")
        if status == "verified" and not is_p2_human_verified_page(rec):
            status = "p1_proofed" if is_human_protected_page(rec) else "unproofed"
        bucket = status if status in counts else "unproofed"
        counts[bucket] += 1
        ev = rec.get("evidence") or {}
        for bid in ev.get("confirmed_block_ids") or []:
            confirmed_block_ids.add(str(bid))
        if is_human_protected_page(rec):
            for blk in page.blocks:
                if blk.type not in FURNITURE_BLOCK_TYPES:
                    confirmed_block_ids.add(blk.block_id)

    denom = len(pages) or 1
    human_done = counts["p1_proofed"] + counts["verified"]
    return {
        "total_pages": len(pages),
        **counts,
        "proofed_ratio": round(human_done / denom, 4),
        "verified_ratio": round(counts["verified"] / denom, 4),
        "applied_corrections": applied,
        "unmatched_corrections": unmatched,
        "skipped_human_protected": skipped_human,
        "confirmed_block_ids": sorted(confirmed_block_ids),
    }


def apply_corrections_and_proof_overlay(
    pages: list[PageIR],
    *,
    book_id: str = "",
    repo_root: str | Path | None = None,
    corrections: list[dict[str, Any]] | None = None,
    proof_ledger: dict[str, Any] | None = None,
) -> tuple[list[PageIR], dict[str, Any]]:
    """Replay human snapshots and anchor-matched corrections onto `pages` without mutating inputs."""
    eff_corrs = list(corrections) if corrections is not None else load_book_corrections(book_id, repo_root=repo_root)
    eff_ledger = dict(proof_ledger) if proof_ledger is not None else load_proof_ledger(book_id, repo_root=repo_root)
    ledger_pages: dict[str, Any] = dict(eff_ledger.get("pages") or {})

    pages_by_num: dict[int, PageIR] = {}
    baked_by_page: dict[int, set[str]] = {}
    protected_pages: set[int] = set()
    for page in pages:
        restored, baked, is_prot = _restore_human_snapshot_if_present(
            page.model_copy(deep=True), ledger_pages.get(str(page.page_number))
        )
        pages_by_num[page.page_number] = restored
        baked_by_page[page.page_number] = baked
        if is_prot:
            protected_pages.add(page.page_number)

    applied = 0
    unmatched = 0
    skipped_human = 0
    for corr in eff_corrs:
        pnum = int(corr["page_number"])
        cid = str(corr.get("correction_id") or "")
        if pnum not in pages_by_num:
            unmatched += 1
            continue
        if cid and cid in baked_by_page.get(pnum, set()):
            continue
        is_machine = str(corr.get("source") or "human") == "machine" or bool(corr.get("generated_by"))
        if is_machine and pnum in protected_pages:
            skipped_human += 1
            continue
        target_page = pages_by_num[pnum]
        idx = _locate_target_block_index(target_page.blocks, corr)
        if idx is None:
            unmatched += 1
            continue
        new_blocks = _apply_single_op(target_page.blocks, idx, corr)
        pages_by_num[pnum] = target_page.model_copy(update={"blocks": new_blocks})
        applied += 1

    ordered_pages = [pages_by_num[p.page_number] for p in sorted(pages, key=lambda item: item.page_number)]
    summary = _build_proof_summary(ordered_pages, ledger_pages, applied, unmatched, skipped_human)
    return ordered_pages, summary


def record_page_proof(
    book_id: str,
    *,
    page: PageIR,
    reviewer: str,
    status: str,
    proposal_sha256: str,
    proposal_file: str = "proposal.json",
    p1_reviewer_hint: str | None = None,
    applied_correction_ids: list[str] | None = None,
    note: str | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Record a human P1 or P2 page sign-off while enforcing the P1 != P2 two-person rule."""
    clean_rev = reviewer.strip()
    if status not in HUMAN_SIGNOFF_STATUSES or not clean_rev:
        raise ValueError(f"Invalid human sign-off status={status!r} or empty reviewer={reviewer!r}")

    ledger = load_proof_ledger(book_id, repo_root=repo_root)
    pages_map = dict(ledger.get("pages") or {})
    prev = pages_map.get(str(page.page_number)) or {}
    now_iso = datetime.now(timezone.utc).isoformat()

    p1_id = str(prev.get("p1_reviewer_id") or p1_reviewer_hint or "").strip() or None
    p1_at = str(prev.get("p1_verified_at") or "").strip() or None
    p2_id: str | None = None
    p2_at: str | None = None

    if status == "p1_proofed":
        p1_id = clean_rev
        p1_at = now_iso
    elif status == "verified":
        if not p1_id:
            raise ValueError(
                f"Cannot promote page {page.page_number} directly to 'verified' without prior P1 reviewer."
            )
        if p1_id.casefold() == clean_rev.casefold():
            raise ValueError(
                f"Two-person rule violation on page {page.page_number}: P2 verifier {clean_rev!r} matches P1 reviewer {p1_id!r}."
            )
        p2_id = clean_rev
        p2_at = now_iso

    prev_baked = set((prev.get("attestation") or {}).get("applied_correction_ids") or [])
    baked_all = sorted(prev_baked | set(applied_correction_ids or []))
    attestation: dict[str, Any] = {
        "channel": "community_proposal",
        "proposal_sha256": proposal_sha256,
        "proposal_file": proposal_file,
        "applied_correction_ids": baked_all,
    }
    if status in HUMAN_PROTECTED_STATUSES and page.blocks:
        attestation["accepted_blocks"] = [b.model_dump(mode="json") for b in page.blocks]

    rec: dict[str, Any] = {
        "page_number": page.page_number,
        "status": status,
        "signoff_method": "human",
        "p1_reviewer_id": p1_id,
        "p1_verified_at": p1_at,
        "p2_verifier_id": p2_id,
        "p2_verified_at": p2_at,
        "reviewer": clean_rev,
        "verified_at": now_iso,
        "source_page_sha256": page.source_page_sha256 or None,
        "page_text_sha256": compute_page_text_sha256(page.blocks),
        "attestation": attestation,
        "evidence": prev.get("evidence"),
        "notes": note,
    }
    pages_map[str(page.page_number)] = rec
    ledger["pages"] = pages_map
    save_proof_ledger(book_id, ledger, repo_root=repo_root)
    return rec


def record_machine_reread(
    book_id: str,
    *,
    page_number: int,
    source_page_sha256: str,
    confirmed_block_ids: list[str],
    reread_block_ids: list[str] | None = None,
    method: str = "machine_reread",
    verify_id: str = "hotspot-verify",
    reviewer: str = "hotspot-verifier",
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Record machine hotspot or native scan-check verification without touching human-proofed pages."""
    ledger = load_proof_ledger(book_id, repo_root=repo_root)
    pages_map = dict(ledger.get("pages") or {})
    prev = pages_map.get(str(page_number)) or {}
    if is_human_protected_page(prev):
        return {"skipped_human": True, "record": prev}

    eff_method = "scan_check" if method == "scan_check" else "machine_reread"
    prev_ev = prev.get("evidence") or {}
    merged_confirmed = sorted(set(prev_ev.get("confirmed_block_ids") or []) | set(confirmed_block_ids))
    merged_reread = sorted(
        set(prev_ev.get("reread_block_ids") or []) | set(reread_block_ids or confirmed_block_ids)
    )
    rec: dict[str, Any] = {
        "page_number": page_number,
        "status": "machine_reread",
        "signoff_method": eff_method,
        "p1_reviewer_id": None,
        "p1_verified_at": None,
        "p2_verifier_id": None,
        "p2_verified_at": None,
        "reviewer": reviewer,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "source_page_sha256": source_page_sha256 or None,
        "page_text_sha256": prev.get("page_text_sha256"),
        "attestation": None,
        "evidence": {
            "kind": eff_method,
            "verify_id": verify_id,
            "reread_block_ids": merged_reread,
            "confirmed_block_ids": merged_confirmed,
        },
        "notes": prev.get("notes"),
    }
    pages_map[str(page_number)] = rec
    ledger["pages"] = pages_map
    save_proof_ledger(book_id, ledger, repo_root=repo_root)
    return {"skipped_human": False, "record": rec}


def _merge_letter_map_patch(book_id: str, patch: dict[str, Any], repo_root: str | Path | None) -> None:
    current = load_letter_map(book_id, repo_root=repo_root)
    force_ids = list(dict.fromkeys([*(current.get("force_split_before_block_ids") or []), *(patch.get("force_split_before_block_ids") or [])]))
    forbid_ids = list(dict.fromkeys([*(current.get("forbid_split_before_block_ids") or []), *(patch.get("forbid_split_before_block_ids") or [])]))
    explicit = [*(current.get("explicit_letters") or []), *(patch.get("explicit_letters") or [])]
    current.update(
        {
            "force_split_before_block_ids": force_ids,
            "forbid_split_before_block_ids": forbid_ids,
            "explicit_letters": explicit,
        }
    )
    save_letter_map(book_id, current, repo_root=repo_root)


def _ingest_scan_check_payload(
    book_id: str,
    doc: dict[str, Any],
    ppath: Path,
    pages_by_num: dict[int, PageIR],
    repo_root: str | Path | None,
) -> dict[str, Any]:
    pnum = int(doc["page_number"])
    checks = doc.get("checks") or []
    confirmed = [
        str(c["block_id"])
        for c in checks
        if c.get("block_id") and c.get("verdict") in {"printed_as_read", "confirmed_original_compositor_ink"}
    ]
    all_checked = [str(c["block_id"]) for c in checks if c.get("block_id")]
    if not all_checked and isinstance(doc.get("blocks"), list):
        all_checked = [f"p{pnum:04d}-b{i:04d}" for i in range(1, len(doc["blocks"]) + 1)]
        confirmed = list(all_checked)
    scan_sha = str(doc.get("source_page_sha256") or (pages_by_num[pnum].source_page_sha256 if pnum in pages_by_num else ""))
    res = record_machine_reread(
        book_id,
        page_number=pnum,
        source_page_sha256=scan_sha,
        confirmed_block_ids=confirmed,
        reread_block_ids=all_checked,
        method="scan_check",
        verify_id=str(doc.get("check_id") or ppath.stem),
        reviewer=str(doc.get("reviewer") or "native-vision-scan-check"),
        repo_root=repo_root,
    )
    return {
        "book_id": book_id,
        "kind": "scan_check",
        "page_number": pnum,
        "skipped_human": res["skipped_human"],
        "confirmed_blocks": len(confirmed),
    }


def intake_proposal(
    book_id: str,
    proposal_path: str | Path,
    *,
    repo_root: str | Path | None = None,
    pages: list[PageIR] | None = None,
    witness_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Ingest a community `correction-v2` proposal or `scan_checks/page_NNNN.json` into `books/<book_id>/`."""
    repo = RepoPaths.from_root(repo_root)
    ppath = Path(proposal_path)
    raw_bytes = ppath.read_bytes()
    doc = json.loads(raw_bytes.decode("utf-8"))
    if doc.get("book_id") and str(doc["book_id"]) != book_id:
        raise ValueError(f"Proposal book_id {doc['book_id']!r} does not match target book_id {book_id!r}")

    pages_by_num = {p.page_number: p for p in (pages or [])}
    if doc.get("version") != CORRECTION_VERSION and "page_number" in doc and ("checks" in doc or "blocks" in doc):
        return _ingest_scan_check_payload(book_id, doc, ppath, pages_by_num, repo_root)

    _validate_if_schema_present("correction_v2", doc, repo)

    signoffs = [*(doc.get("page_signoffs") or []), *([doc["page_signoff"]] if doc.get("page_signoff") else [])]
    if doc.get("generated_by") and signoffs:
        raise ValueError("Automated proposals with `generated_by` may not submit human page sign-offs.")

    existing = load_book_corrections(book_id, repo_root=repo_root)
    existing_ids = {str(c["correction_id"]) for c in existing}
    added_ids: list[str] = []
    for raw_c in doc.get("corrections") or []:
        corr = dict(raw_c)
        if doc.get("generated_by"):
            corr["source"] = "machine"
        cid = str(corr["correction_id"])
        if cid not in existing_ids:
            existing.append(corr)
            existing_ids.add(cid)
            added_ids.append(cid)
    if added_ids:
        save_book_corrections(book_id, existing, repo_root=repo_root)

    if doc.get("letter_map_patch"):
        _merge_letter_map_patch(book_id, dict(doc["letter_map_patch"]), repo_root)

    overlay_pages, _ = apply_corrections_and_proof_overlay(
        list(pages_by_num.values()), book_id=book_id, repo_root=repo_root, corrections=existing
    )
    if overlay_pages:
        overlay_pages, _ = resolve_page_ir_line_wraps(overlay_pages, witness_dir=witness_dir)
    overlay_by_num = {p.page_number: p for p in overlay_pages}
    proposal_sha = sha256_bytes(raw_bytes)
    recorded_signoffs: list[dict[str, Any]] = []
    for s_item in signoffs:
        pnum = int(s_item["page_number"])
        page_obj = overlay_by_num.get(pnum) or PageIR(
            page_number=pnum,
            source_page_sha256=str(s_item.get("source_page_sha256") or ""),
            blocks=[],
        )
        page_corr_ids = [
            str(c["correction_id"]) for c in existing if int(c["page_number"]) == pnum and c.get("source", "human") == "human"
        ]
        rec = record_page_proof(
            book_id,
            page=page_obj,
            reviewer=str(s_item["reviewer"]),
            status=str(s_item["status"]),
            proposal_sha256=proposal_sha,
            proposal_file=ppath.name,
            p1_reviewer_hint=s_item.get("p1_reviewer"),
            applied_correction_ids=page_corr_ids,
            note=s_item.get("note"),
            repo_root=repo_root,
        )
        recorded_signoffs.append(rec)

    return {
        "book_id": book_id,
        "kind": "correction_proposal",
        "added_corrections": len(added_ids),
        "recorded_signoffs": len(recorded_signoffs),
        "signoffs": recorded_signoffs,
    }
