"""ROLE: Unified CollectionAST compiler with cross-page stitching and dual footnote projection.
INPUTS: PageIR list (or run_dir on disk), human corrections/proof_ledger, letter_map, chapter_map.
OUTPUTS: Complete CollectionAST serving both Whole-Book Editions and Standalone Online Letters.
INVARIANT: 100% conservation of non-furniture blocks; zero content-word regexes; zero validation imports.
"""

from __future__ import annotations

import json
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
from lanterns.core.paths import RepoPaths
from lanterns.core.tags import (
    extract_inline_markers,
    parse_tagged_text,
    resolve_endnote_row_target,
    split_side_by_side_footnote_tagged,
)
from lanterns.core.unicode_norm import is_binder_signature
from lanterns.extraction.line_wraps import (
    count_corpus_word_forms,
    dedup_or_join_cross_page_tagged,
    resolve_page_ir_line_wraps,
)
from lanterns.epistolary.corrections import (
    apply_corrections_and_proof_overlay,
    load_chapter_map,
    load_letter_map,
)
from lanterns.epistolary.segmentation import segment_epistolary_collection

CONTINUABLE_PROSE_TYPES = frozenset(
    {"body", "blockquote", "editorial", "postscript", "list_item", "index_entry", "salutation"}
)
FLOAT_BLOCK_TYPES = frozenset({"illustration", "caption", "margin_note"})
_COMPAT_PROSE_TYPES = frozenset({"body", "editorial"})
SYMBOL_NOTE_LABELS = frozenset({"*", "**", "†", "‡", "§", "‖", "||", "¶"})
_LEADING_SUP_RE = re.compile(r"^\s*<sup>\s*([^<]+?)\s*</sup>\s*")
_LEADING_WORD_NO_TAG_RE = re.compile(r"^([^\s<]+)(?:\s+(.*))?$", re.DOTALL)
_TRAILING_HYPHEN_RE = re.compile(r"([-‐])((?:</(?:i|b|sc|sup|sub|unclear)>)*)\s*$")
_SUP_SPAN_MASK_RE = re.compile(r"<sup>.*?</sup>", re.DOTALL)
_ENDNOTE_PAGE_RE = re.compile(r"^Pages?\s+(\d+|[ivxlcdm]+)\b", re.IGNORECASE)
_LEGEND_CARRIER_TYPES = frozenset({"dateline", "letter_header", "salutation"})


def normalize_note_label(raw_label: str | None) -> str:
    """Normalize printed footnote marker for deterministic callout-to-note pairing."""
    if raw_label is None:
        return ""
    cleaned = str(raw_label).strip().strip("()[]")
    if cleaned.endswith(".") and len(cleaned) > 1 and cleaned[:-1].isalnum():
        cleaned = cleaned[:-1]
    return cleaned.strip()


def _strip_redundant_leading_label(tagged_text: str, norm_label: str) -> str:
    if not norm_label:
        return tagged_text
    match = _LEADING_SUP_RE.match(tagged_text)
    if match and normalize_note_label(match.group(1)) == norm_label:
        rest = tagged_text[match.end() :].lstrip()
        return rest if rest else tagged_text
    if norm_label in SYMBOL_NOTE_LABELS and tagged_text.startswith(f"{norm_label} "):
        return tagged_text[len(norm_label) :].lstrip()
    stripped_paren = re.sub(rf"^\s*\(\s*{re.escape(norm_label)}\s*\)\s*", "", tagged_text)
    return stripped_paren if stripped_paren else tagged_text


def _starts_lowercase(text: str) -> bool:
    alpha = next((ch for ch in text if ch.isalpha()), "")
    return bool(alpha and alpha.islower())


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


def _promote_bare_symbol_in_tagged(tagged_text: str, sym: str) -> tuple[str, bool]:
    esc = re.escape(sym)
    first_ch = re.escape(sym[0])
    pat = re.compile(rf"(?:[\w.,;:!?'\"“”‘’)\]]|</(?:i|b|sc|unclear)>)[ \t]?({esc})(?!{first_ch})(?![ \t]*{first_ch})")
    masked = _SUP_SPAN_MASK_RE.sub(lambda m: " " * len(m.group(0)), tagged_text)
    matches = list(pat.finditer(masked))
    if not matches:
        return tagged_text, False
    parts: list[str] = []
    cursor = 0
    for match in matches:
        parts.append(tagged_text[cursor : match.start(1)])
        parts.append(f"<sup>{sym}</sup>")
        cursor = match.end(1)
    parts.append(tagged_text[cursor:])
    return "".join(parts), True


def _promote_page_symbol_callouts(pages: list[PageIR]) -> list[PageIR]:
    promoted_pages: list[PageIR] = []
    for page in pages:
        if page.failed_closed:
            promoted_pages.append(page)
            continue
        syms = sorted(
            {
                normalize_note_label(b.note_label)
                for b in page.blocks
                if b.type == "footnote" and normalize_note_label(b.note_label) in SYMBOL_NOTE_LABELS
            },
            key=len,
            reverse=True,
        )
        if not syms:
            promoted_pages.append(page)
            continue
        new_blocks: list[BlockIR] = []
        for blk in page.blocks:
            if blk.type in FURNITURE_BLOCK_TYPES:
                new_blocks.append(blk)
                continue
            own_lbl = normalize_note_label(blk.note_label) if blk.type == "footnote" else ""
            curr_tagged = blk.tagged_text
            changed = False
            for sym in syms:
                if sym == own_lbl:
                    continue
                curr_tagged, hit = _promote_bare_symbol_in_tagged(curr_tagged, sym)
                changed = changed or hit
            if changed:
                t_str, p_str, sp_list = parse_tagged_text(curr_tagged)
                blk = blk.model_copy(update={"tagged_text": t_str, "plain_text": p_str, "spans": sp_list})
            new_blocks.append(blk)
        promoted_pages.append(page.model_copy(update={"blocks": new_blocks}))
    return promoted_pages


def _propagate_page_folios(pages: list[PageIR]) -> list[PageIR]:
    updated_pages: list[PageIR] = []
    for page in pages:
        folio = None if (page.printed_folio and is_binder_signature(page.printed_folio, page.page_number)) else page.printed_folio
        if not folio:
            folio = next(
                (
                    b.plain_text.strip()
                    for b in page.blocks
                    if b.type == "folio"
                    and b.plain_text.strip()
                    and not is_binder_signature(b.plain_text.strip(), page.page_number)
                ),
                None,
            )
        eff_blocks = [
            b
            if (b.printed_folio and not is_binder_signature(b.printed_folio, page.page_number))
            else b.model_copy(update={"printed_folio": folio})
            for b in page.blocks
        ]
        updated_pages.append(page.model_copy(update={"printed_folio": folio, "blocks": eff_blocks}))
    for idx, pg in enumerate(updated_pages[:-1]):
        if not pg.printed_folio and (updated_pages[idx + 1].printed_folio or "").strip() == "2":
            eff_blks = [b.model_copy(update={"printed_folio": "1"}) for b in pg.blocks]
            updated_pages[idx] = pg.model_copy(update={"printed_folio": "1", "blocks": eff_blks})
    return updated_pages


def _append_footnote_continuation(
    parent: FootnoteItem,
    blk: BlockIR,
    *,
    join_inline: bool,
    raw_text: str | None = None,
) -> None:
    src = blk.tagged_text if raw_text is None else raw_text
    if join_inline:
        joined_raw = _join_hyphenated_tagged(parent.tagged_text, src)
    else:
        joined_raw = f"{parent.tagged_text.rstrip()}\n\n{src.lstrip()}"
    tagged, plain, _ = parse_tagged_text(joined_raw)
    parent.tagged_text = tagged
    parent.plain_text = plain
    if blk.block_id not in parent.block_ids:
        parent.block_ids.append(blk.block_id)


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
            if blk.type == "heading":
                m_end = _ENDNOTE_PAGE_RE.match(blk.plain_text.strip())
                if m_end:
                    endnote_target_folio = m_end.group(1)
                    endnote_target_page = folio_to_pnum.get(endnote_target_folio)
                    active_on_page = None
                    continue
                if blk.heading_level == 1:
                    endnote_target_page = None
                    endnote_target_folio = None
            elif blk.type in {"letter_header", "index_entry"}:
                endnote_target_page = None
                endnote_target_folio = None
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
            own_lbl = normalize_note_label(blk.note_label) if blk.type == "footnote" else ""
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
                if blk.block_id not in matched.referenced_in_block_ids:
                    matched.referenced_in_block_ids.append(blk.block_id)
                refs_by_block.setdefault(blk.block_id, []).append(matched.footnote_id)

    for fn in footnotes.values():
        if not fn.referenced_in_block_ids:
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


def _boundary_prose_index(blocks: list[BlockIR], *, from_end: bool) -> int | None:
    indices = range(len(blocks) - 1, -1, -1) if from_end else range(len(blocks))
    return next((i for i in indices if blocks[i].type not in FLOAT_BLOCK_TYPES), None)


def _join_cross_page_pair(
    left_blk: BlockIR,
    right_blk: BlockIR,
    joined_counts: Counter[str],
    hyphen_counts: Counter[str],
) -> tuple[BlockIR, BlockIR]:
    can_harmonize = (
        left_blk.continues_to_next
        and right_blk.continues_from_prev
        and {left_blk.type, right_blk.type} <= _COMPAT_PROSE_TYPES
        and left_blk.type != right_blk.type
    )
    target_type = left_blk.type if can_harmonize else right_blk.type
    l_raw, r_raw = dedup_or_join_cross_page_tagged(
        left_blk.tagged_text,
        right_blk.tagged_text,
        joined_counts,
        hyphen_counts,
    )
    lt, lp, lsp = parse_tagged_text(l_raw)
    new_left = left_blk.model_copy(
        update={"tagged_text": lt, "plain_text": lp, "spans": lsp, "continues_to_next": True}
    )
    rt, rp, rsp = parse_tagged_text(r_raw)
    new_right = right_blk.model_copy(
        update={
            "type": target_type,
            "tagged_text": rt,
            "plain_text": rp,
            "spans": rsp,
            "continues_from_prev": True,
        }
    )
    return new_left, new_right


def _apply_cross_page_prose_joins(
    pages: list[PageIR],
    *,
    witness_dir: str | Path | None = None,
) -> list[BlockIR]:
    joined_counts, hyphen_counts = count_corpus_word_forms(pages, witness_dir=witness_dir)
    page_blocks_map: dict[int, list[BlockIR]] = {}
    for page in pages:
        if not page.failed_closed:
            content = [
                b.model_copy(deep=True)
                for b in page.blocks
                if b.type not in FURNITURE_BLOCK_TYPES and b.type != "footnote"
            ]
            if content:
                page_blocks_map[page.page_number] = content

    sorted_pnums = sorted(page_blocks_map)
    prose_pages = [
        (pnum, first_i, last_i)
        for pnum in sorted_pnums
        if (first_i := _boundary_prose_index(page_blocks_map[pnum], from_end=False)) is not None
        and (last_i := _boundary_prose_index(page_blocks_map[pnum], from_end=True)) is not None
    ]

    for idx in range(len(prose_pages) - 1):
        p_curr, _, left_idx = prose_pages[idx]
        p_next, right_idx, _ = prose_pages[idx + 1]
        left_blk = page_blocks_map[p_curr][left_idx]
        right_blk = page_blocks_map[p_next][right_idx]
        if (
            0 < p_next - p_curr <= 4
            and left_blk.type in CONTINUABLE_PROSE_TYPES
            and right_blk.type in CONTINUABLE_PROSE_TYPES
            and right_blk.epistolary_transition != "starts_new_letter"
            and (left_blk.continues_to_next or right_blk.continues_from_prev)
        ):
            if right_blk.alignment == "indented" and not _starts_lowercase(right_blk.plain_text):
                page_blocks_map[p_curr][left_idx] = left_blk.model_copy(update={"continues_to_next": False})
                page_blocks_map[p_next][right_idx] = right_blk.model_copy(update={"continues_from_prev": False})
            else:
                new_l, new_r = _join_cross_page_pair(left_blk, right_blk, joined_counts, hyphen_counts)
                page_blocks_map[p_curr][left_idx] = new_l
                page_blocks_map[p_next][right_idx] = new_r

    return [blk for pnum in sorted_pnums for blk in page_blocks_map[pnum]]


def _project_letter_footnotes(
    letters: list[LetterUnit],
    footnotes: dict[str, FootnoteItem],
    refs_by_block: dict[str, list[str]],
) -> None:
    for lu in letters:
        frame_and_letter = [*lu.preceding_editorial_block_ids, *lu.block_ids, *lu.following_editorial_block_ids]
        direct = list(dict.fromkeys(fid for bid in frame_and_letter for fid in refs_by_block.get(bid, [])))
        sub_fids = [sfid for fid in direct for fn_bid in footnotes[fid].block_ids for sfid in refs_by_block.get(fn_bid, [])]
        ordered_fids = list(dict.fromkeys([*direct, *sub_fids]))
        lu.footnote_ids = ordered_fids
        for fid in ordered_fids:
            if lu.letter_id not in footnotes[fid].referenced_in_letter_ids:
                footnotes[fid].referenced_in_letter_ids.append(lu.letter_id)


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
        sec_fids = list(dict.fromkeys([*direct, *sub_fids]))
        sec.footnote_ids = sec_fids
        for fid in sec_fids:
            if footnotes[fid].section_id is None:
                footnotes[fid].section_id = sec.section_id


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
        if fn.footnote_id not in target_sec.footnote_ids:
            target_sec.footnote_ids.append(fn.footnote_id)


def load_pages_from_run_dir(run_dir: str | Path) -> list[PageIR]:
    """Load extracted `PageIR` objects from a canonical or experiment `run_dir`."""
    rdir = Path(run_dir)
    for rel in ("ir/pages.json", "pages.json", "collection_ast.json"):
        cand = rdir / rel
        if cand.is_file():
            raw = json.loads(cand.read_text(encoding="utf-8"))
            items = raw.get("pages") if isinstance(raw, dict) else raw
            if isinstance(items, list):
                return sorted((PageIR.model_validate(p) for p in items), key=lambda p: p.page_number)

    for sub in ("pages", "ir/pages", "extractions"):
        pdir = rdir / sub
        if pdir.is_dir():
            files = sorted(p for p in pdir.glob("*.json") if not p.name.endswith(".raw.json"))
            if files:
                return sorted(
                    (PageIR.model_validate_json(f.read_text(encoding="utf-8")) for f in files),
                    key=lambda p: p.page_number,
                )
    raise FileNotFoundError(f"No extracted PageIR artifacts found in {rdir}")


def _read_ledger_spend(run_dir: Path | None, pages: list[PageIR]) -> float:
    page_sum = round(sum(float(p.cost_usd or 0.0) for p in pages), 6)
    if run_dir is None:
        return page_sum
    ledger_file = run_dir / "cost_ledger.jsonl"
    if not ledger_file.is_file():
        return page_sum
    total = 0.0
    for line in ledger_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            total += float(rec.get("cost_usd") or 0.0)
    return round(max(total, page_sum), 6)


def _resolve_book_metadata(
    book_id: str,
    title: str,
    collection: str,
    author_or_editor: str,
    year: int | None,
    archive_id: str | None,
    active_run_id: str,
    repo_root: str | Path | None,
) -> tuple[str, str, str, int | None, str | None, str]:
    repo = RepoPaths.from_root(repo_root)
    toml_path = repo.book_dir(book_id) / "book.toml"
    if not toml_path.is_file():
        return (title or book_id, collection, author_or_editor, year, archive_id, active_run_id)
    cfg = BookConfig.load(book_id, paths=repo)
    return (
        title or cfg.title or book_id,
        collection if collection != "world-correspondence" else (cfg.collection or collection),
        author_or_editor or cfg.author_or_editor,
        cfg.year if year is None else year,
        archive_id or cfg.archive_id or None,
        (cfg.active_run_id or active_run_id) if active_run_id == "manual" else active_run_id,
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
    total_spend_usd: float | None = None,
) -> CollectionAST:
    """Compile extracted `PageIR`s into a unified `CollectionAST` for Book + Standalone Letter editions."""
    eff_title, eff_coll, eff_author, eff_year, eff_archive, eff_run = _resolve_book_metadata(
        book_id, title, collection, author_or_editor, year, archive_id, active_run_id, repo_root
    )
    resolved_run_dir = Path(run_dir) if run_dir is not None else None
    if pages is None:
        if resolved_run_dir is None:
            resolved_run_dir = RepoPaths.from_root(repo_root).find_run_dir(book_id, eff_run)
        raw_pages = load_pages_from_run_dir(resolved_run_dir)
    else:
        raw_pages = list(pages)

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

    eff_letter_map = letter_map if letter_map is not None else load_letter_map(book_id, repo_root=repo_root)
    eff_chapter_map = chapter_map if chapter_map is not None else load_chapter_map(book_id, repo_root=repo_root)

    footnotes = _stitch_footnotes(folio_pages)
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
    sections, letters = segment_epistolary_collection(
        joined_content_blocks,
        book_title=eff_title,
        all_page_numbers=all_pnums,
        failed_pages=failed_page_nums,
        letter_map=eff_letter_map,
        chapter_map=eff_chapter_map,
    )
    _project_dual_footnotes(sections, letters, footnotes, refs_by_block)

    model_breakdown: dict[str, int] = {}
    for p in folio_pages:
        if not p.failed_closed:
            key = f"{p.provider}/{p.model}".strip("/") or "unknown"
            model_breakdown[key] = model_breakdown.get(key, 0) + 1

    eff_total_pages = total_pages if total_pages is not None else (max(all_pnums) if all_pnums else 0)
    eff_spend = total_spend_usd if total_spend_usd is not None else _read_ledger_spend(resolved_run_dir, folio_pages)

    return CollectionAST(
        book_id=book_id,
        collection=eff_coll,
        title=eff_title,
        author_or_editor=eff_author,
        year=eff_year,
        archive_id=eff_archive,
        active_run_id=eff_run,
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
