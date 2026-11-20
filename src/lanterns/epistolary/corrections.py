"""ROLE: Book-scoped epistolary maps, anchored structure overrides, and ProofOverlay re-exports.

INPUTS: `books/<book_id>/{letter_map,chapter_map,structure_overrides}.json`, `PageIR` list, and community proposals.
OUTPUTS: Epistolary map payloads, rebased `BoundaryAnchor` IDs, and re-exported `PageIR` proof helpers.
INVARIANT: Human-proofed page text (`p1_proofed` or `verified`) is sacred and never overwritten by AI runs.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from lanterns.core.atomic import atomic_write_json
from lanterns.core.models import BlockIR, BoundaryAnchor, PageIR, make_boundary_anchor
from lanterns.core.paths import RepoPaths
from lanterns.extraction.proof_overlay import (
    CORRECTION_VERSION,
    HUMAN_PROTECTED_STATUSES,
    HUMAN_SIGNOFF_STATUSES,
    LEDGER_VERSION,
    apply_corrections_and_proof_overlay,
    compute_page_text_sha256,
    intake_page_overlay_proposal,
    is_human_protected_page,
    is_p2_human_verified_page,
    load_book_corrections,
    load_book_json,
    load_proof_ledger,
    record_machine_reread,
    record_page_proof,
    save_book_corrections,
    save_proof_ledger,
    validate_if_schema_present,
)

__all__ = [
    "CORRECTION_VERSION",
    "HUMAN_PROTECTED_STATUSES",
    "HUMAN_SIGNOFF_STATUSES",
    "LEDGER_VERSION",
    "apply_corrections_and_proof_overlay",
    "compute_page_text_sha256",
    "intake_proposal",
    "is_human_protected_page",
    "is_p2_human_verified_page",
    "load_book_corrections",
    "load_chapter_map",
    "load_letter_map",
    "load_proof_ledger",
    "load_structure_overrides",
    "make_boundary_anchor",
    "rebase_boundary_anchor",
    "record_machine_reread",
    "record_page_proof",
    "save_book_corrections",
    "save_letter_map",
    "save_proof_ledger",
]


def load_letter_map(book_id: str, *, repo_root: str | Path | None = None) -> dict[str, Any]:
    """Load canonical `books/<book_id>/letter_map.json` overrides if present."""
    return load_book_json(book_id, "letter_map.json", "letter_map", {}, repo_root)


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
    validate_if_schema_present("letter_map", payload, repo)
    return atomic_write_json(repo.book_dir(book_id) / "letter_map.json", payload)


def load_chapter_map(book_id: str, *, repo_root: str | Path | None = None) -> dict[str, Any]:
    """Load optional `books/<book_id>/chapter_map.json` section boundaries."""
    return load_book_json(book_id, "chapter_map.json", None, {}, repo_root)


def load_structure_overrides(book_id: str, *, repo_root: str | Path | None = None) -> dict[str, Any]:
    """Load optional sparse human `books/<book_id>/structure_overrides.json` overrides."""
    return load_book_json(book_id, "structure_overrides.json", "structure_overrides", {}, repo_root)


def rebase_boundary_anchor(anchor: BoundaryAnchor, blocks: list[BlockIR]) -> str | None:
    """Resolve `anchor` onto live `blocks` after block splits, merges, or single-page re-extracts."""
    prefix = " ".join(anchor.anchor_text_prefix.split())
    p_head = prefix[:24]
    by_id = {b.block_id: b for b in blocks}
    exact = by_id.get(anchor.block_id)
    if exact is not None and exact.page_number == anchor.page_number:
        exact_txt = " ".join(exact.plain_text.split())
        if not p_head or exact_txt.startswith(p_head) or p_head in exact_txt:
            return exact.block_id

    pool = [b for b in blocks if b.page_number == anchor.page_number] or [
        b for b in blocks if abs(b.page_number - anchor.page_number) <= 1
    ]
    if not pool or not prefix:
        return exact.block_id if exact is not None else (pool[0].block_id if pool else None)

    def _score(item: tuple[int, BlockIR]) -> tuple[bool, float, float, int]:
        pos, blk = item
        raw_norm = " ".join(blk.plain_text.split())
        txt = raw_norm[: max(len(prefix) + 24, 80)]
        is_first_split = blk.block_id == f"{anchor.block_id}-s1"
        hit = (
            txt.startswith(p_head)
            or p_head in txt
            or (len(raw_norm) >= 8 and prefix.startswith(raw_norm[:32]))
            or is_first_split
        )
        cmp_prefix = prefix[: max(len(txt), 16)]
        ratio = difflib.SequenceMatcher(None, cmp_prefix, txt).ratio()
        y_pen = -abs(float(blk.bbox[1]) - anchor.bbox_y0) if (blk.bbox and anchor.bbox_y0 is not None) else 0.0
        return (ratio >= 0.72 or hit, ratio + (0.05 * is_first_split), y_pen, -pos)

    best_pos, best_blk = max(enumerate(pool), key=_score)
    best_ok = _score((best_pos, best_blk))[0]
    if best_ok:
        return best_blk.block_id
    return exact.block_id if exact is not None else None


def _explicit_letter_key(item: dict[str, Any]) -> tuple[str, ...]:
    lid = str(item.get("letter_id") or "").strip()
    if lid:
        return ("id", lid)
    return ("span", str(item.get("start_block_id") or ""), str(item.get("end_block_id") or ""))


def _merge_letter_map_patch(book_id: str, patch: dict[str, Any], repo_root: str | Path | None) -> None:
    current = load_letter_map(book_id, repo_root=repo_root)
    force_ids = list(
        dict.fromkeys([*(current.get("force_split_before_block_ids") or []), *(patch.get("force_split_before_block_ids") or [])])
    )
    forbid_ids = list(
        dict.fromkeys([*(current.get("forbid_split_before_block_ids") or []), *(patch.get("forbid_split_before_block_ids") or [])])
    )
    enc_map = {
        (str(r.get("start_block_id") or ""), str(r.get("end_block_id") or "")): dict(r)
        for r in [*(current.get("enclosure_block_ranges") or []), *(patch.get("enclosure_block_ranges") or [])]
        if isinstance(r, dict)
    }
    exp_map = {
        _explicit_letter_key(item): dict(item)
        for item in [*(current.get("explicit_letters") or []), *(patch.get("explicit_letters") or [])]
        if isinstance(item, dict)
    }
    current.update(
        {
            "force_split_before_block_ids": force_ids,
            "forbid_split_before_block_ids": forbid_ids,
            "enclosure_block_ranges": list(enc_map.values()),
            "explicit_letters": list(exp_map.values()),
        }
    )
    save_letter_map(book_id, current, repo_root=repo_root)


def intake_proposal(
    book_id: str,
    proposal_path: str | Path,
    *,
    repo_root: str | Path | None = None,
    pages: list[PageIR] | None = None,
    witness_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Ingest a community `correction-v2` proposal or `scan_checks/page_NNNN.json` into `books/<book_id>/`."""
    return intake_page_overlay_proposal(
        book_id,
        proposal_path,
        repo_root=repo_root,
        pages=pages,
        witness_dir=witness_dir,
        on_letter_map_patch=_merge_letter_map_patch,
    )
