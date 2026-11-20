"""ROLE: Unified CollectionAST compiler with cross-page stitching and dual footnote projection.
INPUTS: PageIR list (or run_dir on disk), human corrections/proof_ledger, letter_map, chapter_map.
OUTPUTS: Complete CollectionAST serving both Whole-Book Editions and Standalone Online Letters.
INVARIANT: 100% conservation of non-furniture blocks; zero content-word regexes; zero validation imports.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lanterns.core.config import BookConfig
from lanterns.core.models import (
    FURNITURE_BLOCK_TYPES,
    BlockIR,
    BookSection,
    CollectionAST,
    FootnoteItem,
    LetterUnit,
    PageIR,
    ValidationFinding,
)
from lanterns.core.tags import (
    SYMBOL_NOTE_LABELS,
    extract_inline_markers,
    normalize_note_label,
    parse_tagged_text,
    resolve_endnote_row_target,
    split_side_by_side_footnote_tagged,
    strip_redundant_leading_label,
)
from lanterns.extraction.line_wraps import (
    apply_cross_page_prose_joins as _apply_cross_page_prose_joins,
    resolve_page_ir_line_wraps,
    starts_lowercase as _starts_lowercase,
)
from lanterns.extraction.normalize import (
    promote_attested_legacy_sup_callouts as _promote_attested_legacy_sup_callouts,
    promote_page_symbol_callouts as _promote_page_symbol_callouts,
    propagate_page_folios as _propagate_page_folios,
)
from lanterns.extraction.pipeline import (
    load_pages_from_run_dir,
    read_ledger_spend as _read_ledger_spend,
    resolve_compile_pages,
)
from lanterns.extraction.structure_pass import load_structure_auto
from lanterns.epistolary.corrections import (
    apply_corrections_and_proof_overlay,
    load_chapter_map,
    load_letter_map,
    load_structure_overrides,
)
from lanterns.epistolary.segmentation import segment_epistolary_collection
from lanterns.epistolary.structure_compile import (
    compile_from_structure_plan,
    populate_section_units_from_items,
)

__all__ = [
    "compile_collection_ast",
    "load_pages_from_run_dir",
    "normalize_note_label",
    "resolve_block_callout_targets",
]

_LEADING_WORD_NO_TAG_RE = re.compile(r"^([^\s<]+)(?:\s+(.*))?$", re.DOTALL)
_TRAILING_HYPHEN_RE = re.compile(r"([-‐])((?:</(?:i|b|sc|fnref|sup|sub|unclear)>)*)\s*$")
_ENDNOTE_PAGE_RE = re.compile(r"^Pages?\s+(\d+|[ivxlcdm]+)\b", re.IGNORECASE)
_LEGEND_CARRIER_TYPES = frozenset({"dateline", "letter_header", "salutation"})
_strip_redundant_leading_label = strip_redundant_leading_label


def _join_hyphenated_tagged(left_tagged: str, right_tagged: str) -> str:
    hyph_match = _TRAILING_HYPHEN_RE.search(left_tagged)
    word_match = _LEADING_WORD_NO_TAG_RE.match(right_tagged.lstrip())
    if hyph_match and word_match and _starts_lowercase(word_match.group(1)):
        head_word = word_match.group(1)
        rest = (word_match.group(2) or "").strip()
        closing_tags = hyph_match.group(2)
        stem_left = left_tagged[: hyph_match.start()]
        joined_word = f"{stem_left}{head_word}{closing_tags}"
        return f"{joined_word} {rest}".strip() if rest else joined_word
    return f"{left_tagged.rstrip()} {right_tagged.lstrip()}".strip()


def _append_footnote_continuation(
    parent: FootnoteItem,
    blk: BlockIR,
    *,
    join_inline: bool,
    raw_text: str | None = None,
) -> None:
    src = blk.tagged_text if raw_text is None else raw_text
    joined_raw = (
        _join_hyphenated_tagged(parent.tagged_text, src)
        if join_inline
        else f"{parent.tagged_text.rstrip()}\n\n{src.lstrip()}"
    )
    tagged, plain, _ = parse_tagged_text(joined_raw)
    parent.tagged_text = tagged
    parent.plain_text = plain
    parent.block_ids = list(dict.fromkeys([*parent.block_ids, blk.block_id]))


def _stitch_footnote_segment(
    assembled: list[FootnoteItem],
    active_on_page: FootnoteItem | None,
    *,
    page: PageIR,
    blk: BlockIR,
    eff_lbl: str,
    seg_text: str,
    target_page: int | None,
    target_folio: str | None,
    page_seq: dict[int, int],
) -> FootnoteItem:
    if (blk.continues_from_prev or target_page is None) and not eff_lbl and active_on_page is None and assembled:
        max_gap = 4 if blk.continues_from_prev else 1
        candidates = [fn for fn in assembled if 0 < page.page_number - fn.origin_page <= max_gap]
        if candidates:
            _append_footnote_continuation(candidates[-1], blk, join_inline=bool(blk.continues_from_prev), raw_text=seg_text)
            return candidates[-1]

    if not eff_lbl and (active_on_page is not None or (target_page is not None and assembled)):
        parent = active_on_page or assembled[-1]
        _append_footnote_continuation(parent, blk, join_inline=blk.continues_from_prev, raw_text=seg_text)
        return parent

    orig_p = target_page or page.page_number
    seq = page_seq.get(orig_p, 0) + 1
    page_seq[orig_p] = seq
    fn_id = f"fn-p{orig_p:04d}-{seq}"
    cleaned_raw = _strip_redundant_leading_label(seg_text, eff_lbl)
    tagged, plain, _ = parse_tagged_text(cleaned_raw)
    new_fn = FootnoteItem(
        footnote_id=fn_id,
        label=eff_lbl,
        origin_page=orig_p,
        origin_folio=target_folio or blk.printed_folio or page.printed_folio,
        block_ids=[blk.block_id],
        tagged_text=tagged,
        plain_text=plain,
    )
    assembled.append(new_fn)
    return new_fn


def _stitch_footnotes(pages: list[PageIR]) -> dict[str, FootnoteItem]:
    folio_to_pnum = {
        p.printed_folio.strip(): p.page_number
        for p in pages
        if not p.failed_closed and p.printed_folio and p.printed_folio.strip()
    }
    assembled: list[FootnoteItem] = []
    page_seq: dict[int, int] = {}
    endnote_target_page: int | None = None
    endnote_target_folio: str | None = None

    for page in pages:
        if page.failed_closed:
            continue
        active_on_page: FootnoteItem | None = None
        for blk in page.blocks:
            if blk.type == "heading" and (m_end := _ENDNOTE_PAGE_RE.match(blk.plain_text.strip())):
                endnote_target_folio = m_end.group(1)
                endnote_target_page = folio_to_pnum.get(endnote_target_folio)
                active_on_page = None
                continue
            if (blk.type == "heading" and blk.heading_level == 1) or blk.type in {"letter_header", "index_entry"}:
                endnote_target_page = endnote_target_folio = None
            if blk.type != "footnote":
                continue

            endnote_target_page, endnote_target_folio, raw_lbl, row_text = resolve_endnote_row_target(
                blk.tagged_text,
                blk.note_label,
                folio_to_pnum,
                page.page_number,
                endnote_target_page,
                endnote_target_folio,
            )
            base_lbl = normalize_note_label(raw_lbl)
            for seg_idx, (seg_lbl, seg_text) in enumerate(split_side_by_side_footnote_tagged(row_text)):
                eff_lbl = seg_lbl or (base_lbl if seg_idx == 0 else "")
                active_on_page = _stitch_footnote_segment(
                    assembled,
                    active_on_page,
                    page=page,
                    blk=blk,
                    eff_lbl=eff_lbl,
                    seg_text=seg_text,
                    target_page=endnote_target_page,
                    target_folio=endnote_target_folio,
                    page_seq=page_seq,
                )
    return {fn.footnote_id: fn for fn in assembled}


def _collect_page_callout_labels(pages: list[PageIR]) -> dict[int, Counter[str]]:
    return {
        page.page_number: Counter(
            normalize_note_label(raw_m)
            for blk in page.blocks
            if blk.type not in FURNITURE_BLOCK_TYPES and blk.type != "footnote"
            for raw_m in extract_inline_markers(blk.tagged_text)
        )
        for page in pages
        if not page.failed_closed
    }


def _unclaimed_with_label(notes: list[FootnoteItem], marker_norm: str) -> list[FootnoteItem]:
    return [fn for fn in notes if not fn.referenced_in_block_ids and normalize_note_label(fn.label) == marker_norm]


def _match_single_callout(
    marker_norm: str,
    page_num: int,
    notes_by_page: dict[int, list[FootnoteItem]],
    page_callout_labels: dict[int, Counter[str]],
    *,
    continues_from_prev: bool = False,
) -> FootnoteItem | None:
    prev_unclaimed = _unclaimed_with_label(notes_by_page.get(page_num - 1, []), marker_norm)
    same_unclaimed = _unclaimed_with_label(notes_by_page.get(page_num, []), marker_norm)
    if prev_unclaimed and (not same_unclaimed or continues_from_prev):
        return prev_unclaimed[0]
    if same_unclaimed:
        return same_unclaimed[0]
    next_unclaimed = _unclaimed_with_label(notes_by_page.get(page_num + 1, []), marker_norm)
    if len(next_unclaimed) > page_callout_labels.get(page_num + 1, Counter()).get(marker_norm, 0):
        return next_unclaimed[0]
    return next((fn for fn in notes_by_page.get(page_num, []) if normalize_note_label(fn.label) == marker_norm), None)


def _link_callouts_to_footnotes(
    pages: list[PageIR],
    footnotes: dict[str, FootnoteItem],
) -> tuple[dict[str, list[str]], list[ValidationFinding]]:
    notes_by_page: dict[int, list[FootnoteItem]] = {}
    for fn in footnotes.values():
        notes_by_page.setdefault(fn.origin_page, []).append(fn)
    page_callout_labels = _collect_page_callout_labels(pages)

    refs_by_block: dict[str, list[str]] = {}
    findings: list[ValidationFinding] = []
    legend_by_sym: dict[str, FootnoteItem] = {}
    margin_labels_by_page = {
        page.page_number: {normalize_note_label(b.note_label) for b in page.blocks if b.type == "margin_note" and b.note_label}
        for page in pages
    }
    for page in pages:
        if page.failed_closed:
            continue
        for blk in page.blocks:
            if blk.type in FURNITURE_BLOCK_TYPES:
                continue
            own_lbl = (blk.type == "footnote" and normalize_note_label(blk.note_label)) or ""
            for raw_marker in extract_inline_markers(blk.tagged_text):
                m_norm = normalize_note_label(raw_marker)
                if not m_norm or (blk.type == "footnote" and (m_norm == own_lbl or m_norm not in SYMBOL_NOTE_LABELS)):
                    continue
                matched = _match_single_callout(
                    m_norm,
                    page.page_number,
                    notes_by_page,
                    page_callout_labels,
                    continues_from_prev=blk.continues_from_prev,
                )
                if matched is None and m_norm in SYMBOL_NOTE_LABELS and blk.type in _LEGEND_CARRIER_TYPES:
                    matched = legend_by_sym.get(m_norm)
                if matched is None:
                    if blk.type == "toc_entry" or m_norm in margin_labels_by_page.get(page.page_number, set()):
                        continue
                    findings.append(
                        ValidationFinding(
                            code="unmatched_callout",
                            severity="warning",
                            page_number=page.page_number,
                            block_id=blk.block_id,
                            message=(
                                f"Inline footnote callout {raw_marker!r} on p. {blk.printed_folio or page.page_number} "
                                "has no matching footnote block."
                            ),
                            evidence={"marker": raw_marker},
                        )
                    )
                    continue
                if m_norm in SYMBOL_NOTE_LABELS and blk.type in _LEGEND_CARRIER_TYPES:
                    legend_by_sym.setdefault(m_norm, matched)
                matched.referenced_in_block_ids = list(dict.fromkeys([*matched.referenced_in_block_ids, blk.block_id]))
                refs_by_block.setdefault(blk.block_id, []).append(matched.footnote_id)

    for fn in [fn for fn in footnotes.values() if not fn.referenced_in_block_ids]:
        fn.is_orphan = True
        folio_disp = fn.origin_folio or str(fn.origin_page)
        findings.append(
            ValidationFinding(
                code="unlinked_footnote",
                severity="warning",
                page_number=fn.origin_page,
                block_id=fn.block_ids[0] if fn.block_ids else None,
                message=f"[Unlinked note, p. {folio_disp}] Footnote {fn.footnote_id} ({fn.label or 'unlabelled'}) has no matching inline callout.",
                evidence={"footnote_id": fn.footnote_id, "label": fn.label},
            )
        )
    return refs_by_block, findings


def resolve_block_callout_targets(
    block: BlockIR,
    footnotes: Mapping[str, FootnoteItem],
) -> list[tuple[str, str]]:
    """Return ordered `(raw_marker, footnote_id)` pairs for inline `<sup>` callouts in `block`."""
    candidates = [fn for fn in footnotes.values() if block.block_id in fn.referenced_in_block_ids]
    if not candidates:
        return []
    used_ids: set[str] = set()
    resolved: list[tuple[str, str]] = []
    for raw_marker in extract_inline_markers(block.tagged_text):
        m_norm = normalize_note_label(raw_marker)
        chosen = next(
            (fn for fn in candidates if fn.footnote_id not in used_ids and normalize_note_label(fn.label) == m_norm),
            None,
        ) or next((fn for fn in candidates if normalize_note_label(fn.label) == m_norm), None)
        if chosen is not None:
            used_ids.add(chosen.footnote_id)
            resolved.append((raw_marker, chosen.footnote_id))
    return resolved


def _project_letter_footnotes(
    letters: list[LetterUnit],
    footnotes: dict[str, FootnoteItem],
    refs_by_block: dict[str, list[str]],
) -> None:
    for lu in letters:
        frame_and_letter = [*lu.preceding_editorial_block_ids, *lu.block_ids, *lu.following_editorial_block_ids]
        direct = list(dict.fromkeys(fid for bid in frame_and_letter for fid in refs_by_block.get(bid, [])))
        sub_fids = [sfid for fid in direct for fn_bid in footnotes[fid].block_ids for sfid in refs_by_block.get(fn_bid, [])]
        lu.footnote_ids = list(dict.fromkeys([*direct, *sub_fids]))
        for fid in lu.footnote_ids:
            footnotes[fid].referenced_in_letter_ids = list(
                dict.fromkeys([*footnotes[fid].referenced_in_letter_ids, lu.letter_id])
            )


def _project_section_footnotes(
    sections: list[BookSection],
    footnotes: dict[str, FootnoteItem],
    refs_by_block: dict[str, list[str]],
) -> None:
    for sec in sections:
        direct = list(
            dict.fromkeys(fid for item in sec.items for blk in item.blocks for fid in refs_by_block.get(blk.block_id, []))
        )
        sub_fids = [sfid for fid in direct for fn_bid in footnotes[fid].block_ids for sfid in refs_by_block.get(fn_bid, [])]
        sec.footnote_ids = list(dict.fromkeys([*direct, *sub_fids]))
        for unit in sec.units:
            u_dir = list(dict.fromkeys(fid for bid in unit.block_ids for fid in refs_by_block.get(bid, [])))
            u_sub = [sfid for fid in u_dir for fn_bid in footnotes[fid].block_ids for sfid in refs_by_block.get(fn_bid, [])]
            unit.footnote_ids = list(dict.fromkeys([*u_dir, *u_sub]))
        for fid in sec.footnote_ids:
            footnotes[fid].section_id = footnotes[fid].section_id or sec.section_id


def _project_dual_footnotes(
    sections: list[BookSection],
    letters: list[LetterUnit],
    footnotes: dict[str, FootnoteItem],
    refs_by_block: dict[str, list[str]],
) -> None:
    _project_letter_footnotes(letters, footnotes, refs_by_block)
    _project_section_footnotes(sections, footnotes, refs_by_block)
    if not sections:
        return
    for fn in footnotes.values():
        if fn.section_id is not None:
            continue
        target_sec = next((s for s in sections if s.start_page <= fn.origin_page <= s.end_page), None) or min(
            sections,
            key=lambda s: min(abs(s.start_page - fn.origin_page), abs(s.end_page - fn.origin_page)),
        )
        fn.section_id = target_sec.section_id
        target_sec.footnote_ids = list(dict.fromkeys([*target_sec.footnote_ids, fn.footnote_id]))


def _resolve_book_metadata(
    book_id: str,
    title: str,
    collection: str,
    author_or_editor: str,
    year: int | None,
    archive_id: str | None,
    active_run_id: str,
    repo_root: str | Path | None,
) -> dict[str, Any]:
    return BookConfig.resolve_collection_defaults(
        book_id,
        title=title,
        collection=collection,
        author_or_editor=author_or_editor,
        year=year,
        archive_id=archive_id,
        active_run_id=active_run_id,
        repo_root=repo_root,
    )


def compile_collection_ast(
    pages: list[PageIR] | None = None,
    *,
    book_id: str = "book",
    title: str = "",
    collection: str = "world-correspondence",
    author_or_editor: str = "",
    year: int | None = None,
    archive_id: str | None = None,
    active_run_id: str = "manual",
    total_pages: int | None = None,
    run_dir: str | Path | None = None,
    repo_root: str | Path | None = None,
    letter_map: dict[str, Any] | None = None,
    chapter_map: dict[str, Any] | None = None,
    corrections: list[dict[str, Any]] | None = None,
    proof_ledger: dict[str, Any] | None = None,
    structure_plan: dict[str, Any] | None = None,
    structure_overrides: dict[str, Any] | None = None,
    total_spend_usd: float | None = None,
) -> CollectionAST:
    """Compile extracted `PageIR`s into a unified `CollectionAST` for Book + Standalone Letter editions."""
    meta = _resolve_book_metadata(
        book_id, title, collection, author_or_editor, year, archive_id, active_run_id, repo_root
    )
    eff_title = str(meta["title"])
    eff_run = str(meta["active_run_id"])
    raw_pages, resolved_run_dir = resolve_compile_pages(pages, run_dir, book_id, eff_run, repo_root)

    overlaid_pages, proof_summary = apply_corrections_and_proof_overlay(
        raw_pages,
        book_id=book_id,
        repo_root=repo_root,
        corrections=corrections,
        proof_ledger=proof_ledger,
    )
    witness_dir = resolved_run_dir / "witnesses" if resolved_run_dir else None
    dehyphenated_pages, _ = resolve_page_ir_line_wraps(overlaid_pages, witness_dir=witness_dir)
    folio_pages = _promote_page_symbol_callouts(_propagate_page_folios(dehyphenated_pages))

    eff_letter_map = load_letter_map(book_id, repo_root=repo_root) if letter_map is None else letter_map
    eff_chapter_map = load_chapter_map(book_id, repo_root=repo_root) if chapter_map is None else chapter_map

    footnotes = _stitch_footnotes(folio_pages)
    folio_pages = _promote_attested_legacy_sup_callouts(folio_pages, footnotes)
    refs_by_block, fn_findings = _link_callouts_to_footnotes(folio_pages, footnotes)
    joined_content_blocks = _apply_cross_page_prose_joins(folio_pages, witness_dir=witness_dir)

    failed_page_nums = {p.page_number for p in folio_pages if p.failed_closed}
    findings: list[ValidationFinding] = [
        ValidationFinding(
            code="failed_closed_page",
            severity="blocker",
            page_number=p.page_number,
            message=(
                f"[Page {p.page_number} unavailable — inspect source scan] "
                f"({p.failure_reason or 'extraction failed closed'})"
            ),
            evidence={"failure_reason": p.failure_reason or "failed_closed"},
        )
        for p in folio_pages
        if p.failed_closed
    ]
    findings.extend(fn_findings)

    all_pnums = [p.page_number for p in folio_pages]
    eff_plan = structure_plan if structure_plan is not None else (
        load_structure_auto(resolved_run_dir) if (letter_map is None and chapter_map is None) else None
    )
    if eff_plan:
        eff_ovr = load_structure_overrides(book_id, repo_root=repo_root) if structure_overrides is None else structure_overrides
        sections, letters = compile_from_structure_plan(
            joined_content_blocks,
            book_title=eff_title,
            all_page_numbers=all_pnums,
            failed_pages=failed_page_nums,
            structure_plan=eff_plan,
            structure_overrides=eff_ovr,
        )
    else:
        sections, letters = segment_epistolary_collection(
            joined_content_blocks,
            book_title=eff_title,
            all_page_numbers=all_pnums,
            failed_pages=failed_page_nums,
            letter_map=eff_letter_map,
            chapter_map=eff_chapter_map,
        )
        populate_section_units_from_items(sections, {lu.letter_id: lu for lu in letters}, all_letters=letters)
    _project_dual_footnotes(sections, letters, footnotes, refs_by_block)

    model_breakdown = dict(
        Counter(f"{p.provider}/{p.model}".strip("/") or "unknown" for p in folio_pages if not p.failed_closed)
    )
    eff_total_pages = max(all_pnums, default=0) if total_pages is None else total_pages
    eff_spend = _read_ledger_spend(resolved_run_dir, folio_pages) if total_spend_usd is None else total_spend_usd

    return CollectionAST(
        book_id=book_id,
        collection=str(meta["collection"]),
        title=eff_title,
        author_or_editor=str(meta["author_or_editor"]),
        year=meta["year"],
        archive_id=meta["archive_id"],
        active_run_id=eff_run,
        language=str(meta["language"]),
        text_direction=meta["text_direction"],
        series_id=meta["series_id"],
        volume_number=meta["volume_number"],
        total_pages=eff_total_pages,
        pages=folio_pages,
        sections=sections,
        letters=letters,
        footnotes=footnotes,
        findings=findings,
        proof_summary=proof_summary,
        model_breakdown=model_breakdown,
        total_spend_usd=eff_spend,
    )
