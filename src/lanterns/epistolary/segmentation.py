"""ROLE: Deterministic block-level epistolary boundary compiler and section partitioner.
INPUTS: Non-furniture/non-footnote BlockIR stream, page folios, letter_map, and chapter_map.
OUTPUTS: Assembled BookSection list and LetterUnit list with linked editorial framing.
INVARIANT: Routes strictly on BlockIR structural fields and maps; zero content-word regexes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, cast

from lanterns.core.models import (
    BlockIR,
    BookSection,
    BoundaryConfidence,
    LetterUnit,
    SectionItem,
    SectionKind,
)
from lanterns.epistolary.metadata import build_letter_slug, extract_epistolary_metadata

EPISTOLARY_OPENER_TYPES = frozenset({"letter_header", "dateline", "salutation"})
EPISTOLARY_CLOSER_TYPES = frozenset({"valediction", "signature", "postscript", "address_block"})
EPISTOLARY_CORE_TYPES = EPISTOLARY_OPENER_TYPES | EPISTOLARY_CLOSER_TYPES
LETTER_BODY_TYPES = frozenset({"body", "verse", "blockquote", "list_item", "table_row"})

_ROMAN_FOLIO_RE = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)


@dataclass
class _MapDirectives:
    force_split: set[str] = field(default_factory=set)
    forbid_split: set[str] = field(default_factory=set)
    force_close_after: set[str] = field(default_factory=set)
    enclosure_ids: set[str] = field(default_factory=set)
    explicit_by_start_block: dict[str, dict[str, Any]] = field(default_factory=dict)
    explicit_by_seq: dict[int, dict[str, Any]] = field(default_factory=dict)
    explicit_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class _StreamState:
    items: list[SectionItem] = field(default_factory=list)
    raw_letters: list[tuple[list[BlockIR], BoundaryConfidence]] = field(default_factory=list)
    active_blocks: list[BlockIR] = field(default_factory=list)
    pending_prose: list[BlockIR] = field(default_factory=list)
    pending_non_letter: list[BlockIR] = field(default_factory=list)
    enclosure_depth: int = 0
    has_body: bool = False
    has_closer: bool = False
    has_dateline: bool = False
    in_postscript: bool = False
    confidence: BoundaryConfidence = "high"
    saw_ornamental_break: bool = False

    def flush_non_letter(self) -> None:
        if self.pending_non_letter:
            self.items.append(SectionItem(kind="blocks", blocks=list(self.pending_non_letter)))
            self.pending_non_letter.clear()

    def flush_pending_prose_as_blocks(self) -> None:
        if self.pending_prose:
            self.pending_non_letter.extend(self.pending_prose)
            self.pending_prose.clear()
            self.flush_non_letter()

    def pop_leading_margin_notes(self, page_number: int) -> list[BlockIR]:
        if self.active_blocks:
            tail: list[BlockIR] = []
            idx = len(self.active_blocks) - 1
            while idx >= 0 and self.active_blocks[idx].type == "margin_note" and self.active_blocks[idx].page_number == page_number:
                tail.append(self.active_blocks[idx])
                idx -= 1
            if tail and idx >= 0:
                prev_non_margin = self.active_blocks[idx]
                if self.has_closer or prev_non_margin.page_number < page_number:
                    del self.active_blocks[idx + 1 :]
                    tail.reverse()
                    return tail
            return []
        if self.pending_prose and all(b.type == "margin_note" and b.page_number == page_number for b in self.pending_prose):
            notes = list(self.pending_prose)
            self.pending_prose.clear()
            return notes
        if self.pending_non_letter:
            idx = len(self.pending_non_letter) - 1
            while idx >= 0 and self.pending_non_letter[idx].type == "margin_note" and self.pending_non_letter[idx].page_number == page_number:
                idx -= 1
            if idx < len(self.pending_non_letter) - 1:
                notes = list(self.pending_non_letter[idx + 1 :])
                del self.pending_non_letter[idx + 1 :]
                return notes
        return []

    def resolve_pending_prose(self, sec_kind: SectionKind) -> None:
        if not self.pending_prose:
            return
        is_mid_volume_continuation = (
            sec_kind == "epistolary_part"
            and not self.raw_letters
            and any(b.type in LETTER_BODY_TYPES and b.page_number > 1 for b in self.pending_prose)
        )
        if is_mid_volume_continuation:
            prose = list(self.pending_prose)
            self.pending_prose.clear()
            self.start_letter(prose, confidence="ambiguous")
            self.close_active_letter()
        else:
            self.flush_pending_prose_as_blocks()

    def close_active_letter(self) -> None:
        if not self.active_blocks:
            return
        self.flush_non_letter()
        idx = len(self.raw_letters)
        placeholder_id = f"__raw_letter_{idx}__"
        blocks_copy = list(self.active_blocks)
        self.raw_letters.append((blocks_copy, self.confidence))
        self.items.append(SectionItem(kind="letter", letter_id=placeholder_id, blocks=blocks_copy))
        self.active_blocks.clear()
        self.enclosure_depth = 0
        self.has_body = False
        self.has_closer = False
        self.has_dateline = False
        self.in_postscript = False
        self.confidence = "high"
        self.saw_ornamental_break = False

    def start_letter(self, initial: list[BlockIR], confidence: BoundaryConfidence = "high") -> None:
        self.close_active_letter()
        self.flush_non_letter()
        self.active_blocks = []
        self.confidence = confidence
        for blk in initial:
            self.append_to_letter(blk)

    def append_to_letter(self, blk: BlockIR) -> None:
        self.active_blocks.append(blk)
        if blk.epistolary_transition == "starts_enclosure" and len(self.active_blocks) > 1:
            self.enclosure_depth += 1
        if blk.epistolary_transition == "ends_enclosure" and self.enclosure_depth > 0:
            self.enclosure_depth -= 1
        if self.enclosure_depth == 0:
            is_editorial_quote = (
                blk.type == "blockquote"
                and not self.has_body
                and not self.has_dateline
                and len(self.active_blocks) >= 2
                and self.active_blocks[-2].type == "editorial"
            )
            if blk.type in LETTER_BODY_TYPES and not is_editorial_quote:
                self.has_body = True
            if blk.type in {"valediction", "signature"}:
                self.has_closer = True
            if blk.type == "dateline":
                self.has_dateline = True
            if blk.type == "postscript":
                self.in_postscript = True


def _build_map_directives(blocks: list[BlockIR], letter_map: dict[str, Any] | None) -> _MapDirectives:
    directives = _MapDirectives()
    if not letter_map:
        return directives
    directives.force_split = {str(x) for x in (letter_map.get("force_split_before_block_ids") or [])}
    directives.forbid_split = {str(x) for x in (letter_map.get("forbid_split_before_block_ids") or [])}

    id_to_idx = {b.block_id: idx for idx, b in enumerate(blocks)}
    for rng in letter_map.get("enclosure_block_ranges") or []:
        s_idx = id_to_idx.get(str(rng.get("start_block_id") or ""))
        e_idx = id_to_idx.get(str(rng.get("end_block_id") or ""))
        if s_idx is not None and e_idx is not None and s_idx <= e_idx:
            for blk in blocks[s_idx : e_idx + 1]:
                directives.enclosure_ids.add(blk.block_id)

    for exp in letter_map.get("explicit_letters") or []:
        if exp.get("letter_id"):
            directives.explicit_by_id[str(exp["letter_id"])] = exp
        if exp.get("sequence_index") is not None:
            directives.explicit_by_seq[int(exp["sequence_index"])] = exp
        exp_bids = exp.get("block_ids")
        if isinstance(exp_bids, list) and exp_bids:
            first_id = str(exp_bids[0])
            directives.force_split.add(first_id)
            directives.explicit_by_start_block[first_id] = exp
            for inner_id in exp_bids[1:]:
                directives.forbid_split.add(str(inner_id))
            directives.force_close_after.add(str(exp_bids[-1]))
        elif exp.get("start_block_id"):
            s_id = str(exp["start_block_id"])
            directives.force_split.add(s_id)
            directives.explicit_by_start_block[s_id] = exp
            e_id = str(exp.get("end_block_id") or "")
            if s_id in id_to_idx and e_id in id_to_idx and id_to_idx[s_id] <= id_to_idx[e_id]:
                for blk in blocks[id_to_idx[s_id] + 1 : id_to_idx[e_id] + 1]:
                    directives.forbid_split.add(blk.block_id)
                directives.force_close_after.add(e_id)
    return directives


def _is_epistolary_signal(blk: BlockIR, directives: _MapDirectives) -> bool:
    return (
        blk.type in EPISTOLARY_CORE_TYPES
        or blk.epistolary_transition == "starts_new_letter"
        or blk.block_id in directives.force_split
    )


def _is_roman_frontmatter_block(blk: BlockIR) -> bool:
    folio = (blk.printed_folio or "").strip("[](). \t")
    return bool(folio and _ROMAN_FOLIO_RE.match(folio))


def _is_arabic_folio_block(blk: BlockIR) -> bool:
    folio = (blk.printed_folio or "").strip("[](). \t")
    return bool(folio and folio.isdigit())


def _slice_unchaptered_sections(
    blocks: list[BlockIR],
    directives: _MapDirectives,
    book_title: str,
) -> list[tuple[SectionKind, str, str, list[BlockIR]]]:
    first_ep = next((i for i, b in enumerate(blocks) if _is_epistolary_signal(b, directives)), None)
    if first_ep is None:
        if any(b.type == "index_entry" for b in blocks):
            return [("index", "Index", "Index", list(blocks))]
        if any(b.type in {"imprint", "toc_entry"} or _is_roman_frontmatter_block(b) for b in blocks):
            return [("frontmatter", "Frontmatter", "Frontmatter", list(blocks))]
        title = book_title or "Correspondence"
        return [("epistolary_part", title, title, list(blocks))]

    ep_start_page = blocks[first_ep].page_number
    fm_cut = 0
    for idx in range(first_ep):
        blk = blocks[idx]
        if blk.type in {"imprint", "toc_entry"} or (
            _is_roman_frontmatter_block(blk) and blk.page_number < ep_start_page
        ):
            fm_cut = idx + 1
    while fm_cut < first_ep and blocks[fm_cut - 1 : fm_cut] and blocks[fm_cut].page_number == blocks[fm_cut - 1].page_number and blocks[fm_cut].type != "editorial":
        fm_cut += 1

    last_ep = max(i for i, b in enumerate(blocks) if _is_epistolary_signal(b, directives))
    idx_cut = next(
        (i for i in range(last_ep + 1, len(blocks)) if blocks[i].type == "index_entry"),
        len(blocks),
    )
    if idx_cut < len(blocks) and idx_cut > last_ep + 1 and blocks[idx_cut - 1].type == "heading":
        idx_cut -= 1

    out: list[tuple[SectionKind, str, str, list[BlockIR]]] = []
    if fm_cut > 0:
        out.append(("frontmatter", "Frontmatter", "Frontmatter", blocks[:fm_cut]))
    ep_slice = blocks[fm_cut:idx_cut]
    if ep_slice:
        title = book_title or "Correspondence"
        out.append(("epistolary_part", title, title, ep_slice))
    if idx_cut < len(blocks):
        tail = blocks[idx_cut:]
        h_blk = tail[0] if tail[0].type == "heading" else None
        t_tag, t_pln = (h_blk.tagged_text, h_blk.plain_text) if h_blk else ("Index", "Index")
        out.append(("index", t_tag, t_pln, tail))
    return out


def _is_frontmatter_slice(
    idx: int,
    sl_blks: list[BlockIR],
    fm_cues: list[bool],
    last_fm_cue: int | None,
    first_ep_slice: int | None,
) -> bool:
    if fm_cues[idx] or (last_fm_cue is not None and idx <= last_fm_cue):
        return True
    if first_ep_slice is not None and idx < first_ep_slice:
        return not any(_is_arabic_folio_block(b) for b in sl_blks)
    return False


def _slice_has_true_epistolary_start(
    idx: int,
    sl_blks: list[BlockIR],
    last_toc_slice: int | None,
    directives: _MapDirectives,
) -> bool:
    is_pre_arabic_fm = (last_toc_slice is not None and idx <= last_toc_slice) or (
        any(_is_roman_frontmatter_block(b) for b in sl_blks)
        and not any(_is_arabic_folio_block(b) for b in sl_blks)
    )
    if is_pre_arabic_fm:
        return any(
            b.type == "letter_header"
            or b.epistolary_transition == "starts_new_letter"
            or b.block_id in directives.force_split
            for b in sl_blks
        )
    return any(_is_epistolary_signal(b, directives) for b in sl_blks)


def _classify_h1_slices(
    raw_slices: list[tuple[str, str, list[BlockIR]]],
    directives: _MapDirectives,
) -> list[tuple[SectionKind, str, str, list[BlockIR]]]:
    last_toc_slice = max(
        (i for i, (_, _, sl_blks) in enumerate(raw_slices) if any(b.type == "toc_entry" for b in sl_blks)),
        default=None,
    )
    has_ep = [
        _slice_has_true_epistolary_start(i, sl_blks, last_toc_slice, directives)
        for i, (_, _, sl_blks) in enumerate(raw_slices)
    ]
    first_ep_slice = next((i for i, flag in enumerate(has_ep) if flag), None)
    last_ep_slice = max((i for i, flag in enumerate(has_ep) if flag), default=None)
    fm_cues = [
        any(b.type in {"imprint", "toc_entry"} or _is_roman_frontmatter_block(b) for b in sl_blks)
        and not has_ep[i]
        for i, (_, _, sl_blks) in enumerate(raw_slices)
    ]
    last_fm_cue = max(
        (i for i, flag in enumerate(fm_cues) if flag and (first_ep_slice is None or i < first_ep_slice)),
        default=None,
    )

    typed: list[tuple[SectionKind, str, str, list[BlockIR]]] = []
    for idx, (t_tag, t_pln, sl_blks) in enumerate(raw_slices):
        has_index = any(b.type == "index_entry" for b in sl_blks) and not has_ep[idx]
        if has_index:
            kind: SectionKind = "index"
        elif _is_frontmatter_slice(idx, sl_blks, fm_cues, last_fm_cue, first_ep_slice):
            kind = "frontmatter"
        elif last_ep_slice is not None and idx > last_ep_slice and any(b.type == "imprint" for b in sl_blks):
            kind = "backmatter"
        else:
            kind = "chapter"
        typed.append((kind, t_tag, t_pln, sl_blks))
    return typed


def _find_h1_section_starts(
    blocks: list[BlockIR],
    enclosure_ids: set[str],
) -> list[tuple[int, str, str]]:
    starts: list[tuple[int, str, str]] = []
    idx = 0
    while idx < len(blocks):
        blk = blocks[idx]
        if blk.type == "heading" and blk.heading_level == 1 and blk.block_id not in enclosure_ids:
            end_run = idx
            last_h1 = idx
            while (
                end_run + 1 < len(blocks)
                and blocks[end_run + 1].type == "heading"
                and blocks[end_run + 1].page_number == blk.page_number
                and blocks[end_run + 1].block_id not in enclosure_ids
            ):
                end_run += 1
                if blocks[end_run].heading_level == 1:
                    last_h1 = end_run
            span = blocks[idx : last_h1 + 1]
            t_tag = " ".join(" ".join(b.tagged_text.split()) for b in span)
            t_pln = " ".join(" ".join(b.plain_text.split()) for b in span)
            starts.append((idx, t_tag, t_pln))
            idx = last_h1 + 1
            continue
        idx += 1
    return starts


def _partition_sections(
    blocks: list[BlockIR],
    directives: _MapDirectives,
    book_title: str,
    chapter_map: dict[str, Any] | None,
) -> list[tuple[SectionKind, str, str, list[BlockIR]]]:
    if not blocks:
        title = book_title or "Correspondence"
        return [("epistolary_part", title, title, [])]

    if chapter_map and chapter_map.get("sections"):
        mapped: list[tuple[SectionKind, str, str, list[BlockIR]]] = []
        id_to_idx = {b.block_id: i for i, b in enumerate(blocks)}
        for spec in chapter_map["sections"]:
            kind = cast(SectionKind, str(spec.get("kind") or "chapter"))
            title_p = str(spec.get("title") or "Section")
            title_t = str(spec.get("title_tagged") or title_p)
            if spec.get("start_block_id") in id_to_idx:
                s_i = id_to_idx[str(spec["start_block_id"])]
                e_i = id_to_idx.get(str(spec.get("end_block_id") or ""), len(blocks) - 1) + 1
                sl = blocks[s_i:e_i]
            else:
                sp, ep = int(spec["start_page"]), int(spec["end_page"])
                sl = [b for b in blocks if sp <= b.page_number <= ep]
            if sl:
                mapped.append((kind, title_t, title_p, sl))
        if mapped:
            return mapped

    h1_starts = _find_h1_section_starts(blocks, directives.enclosure_ids)
    if not h1_starts:
        return _slice_unchaptered_sections(blocks, directives, book_title)

    out: list[tuple[SectionKind, str, str, list[BlockIR]]] = []
    if h1_starts[0][0] > 0:
        lead = blocks[: h1_starts[0][0]]
        if any(_is_epistolary_signal(b, directives) for b in lead):
            title = book_title or "Correspondence"
            out.append(("epistolary_part", title, title, lead))
        else:
            out.append(("frontmatter", "Frontmatter", "Frontmatter", lead))

    raw_h1: list[tuple[str, str, list[BlockIR]]] = []
    for pos_i, (start_idx, t_tag, t_pln) in enumerate(h1_starts):
        end_idx = h1_starts[pos_i + 1][0] if pos_i + 1 < len(h1_starts) else len(blocks)
        raw_h1.append((t_tag, t_pln, blocks[start_idx:end_idx]))

    out.extend(_classify_h1_slices(raw_h1, directives))
    return out


def _editorial_run_precedes_boundary(
    sec_blocks: list[BlockIR],
    start_idx: int,
    directives: _MapDirectives,
) -> bool:
    idx = start_idx
    while idx < len(sec_blocks) and sec_blocks[idx].type in {"editorial", "ornamental_break", "illustration", "caption"}:
        idx += 1
    if idx >= len(sec_blocks):
        return True
    nxt = sec_blocks[idx]
    return (
        nxt.type in {"letter_header", "heading", "dateline", "salutation"}
        or (nxt.type in {"blockquote", "verse"} and nxt.epistolary_transition != "starts_enclosure")
        or nxt.epistolary_transition == "starts_new_letter"
        or nxt.block_id in directives.force_split
    )


def _is_bottom_dateline(state: _StreamState, next_blk: BlockIR | None, directives: _MapDirectives) -> bool:
    return (
        state.has_closer
        or next_blk is None
        or next_blk.epistolary_transition == "starts_new_letter"
        or next_blk.block_id in directives.force_split
        or next_blk.type not in {"salutation", "body", "verse"}
        or not state.has_dateline
    )


def _is_dateline_split_candidate(state: _StreamState) -> bool:
    return state.has_closer or (state.has_dateline and state.has_body)


def _continues_header_only_letter(state: _StreamState, blk: BlockIR) -> bool:
    if not state.active_blocks or state.has_body or state.has_dateline or state.has_closer:
        return False
    return blk.type in {"dateline", "salutation", "body", "verse"}


def _is_trailing_prose_date(
    state: _StreamState,
    blk: BlockIR,
    next_blk: BlockIR | None,
    directives: _MapDirectives,
) -> bool:
    if blk.type != "dateline" or not state.pending_prose:
        return False
    if next_blk is None or next_blk.type in {"heading", "ornamental_break", "editorial"}:
        return True
    return _is_epistolary_signal(next_blk, directives)


def _handle_inactive_block(
    state: _StreamState,
    blk: BlockIR,
    next_blk: BlockIR | None,
    sec_kind: SectionKind,
    directives: _MapDirectives,
) -> None:
    if _is_trailing_prose_date(state, blk, next_blk, directives):
        state.resolve_pending_prose(sec_kind)
        state.pending_non_letter.append(blk)
        return

    if sec_kind in {"chapter", "epistolary_part"} and (
        blk.type in EPISTOLARY_OPENER_TYPES
        or (blk.type == "address_block" and next_blk is not None and next_blk.type in {"salutation", "body", "dateline"})
    ):
        lead_margins = state.pop_leading_margin_notes(blk.page_number)
        state.resolve_pending_prose(sec_kind)
        state.start_letter([*lead_margins, blk], confidence="high")
        return

    if blk.type in {"valediction", "signature"} and state.pending_prose:
        conf: BoundaryConfidence = "high" if state.saw_ornamental_break else "ambiguous"
        prose = list(state.pending_prose)
        state.pending_prose.clear()
        state.start_letter([*prose, blk], confidence=conf)
        return

    if blk.type in {"body", "verse", "blockquote"} and sec_kind in {"chapter", "epistolary_part"}:
        state.pending_prose.append(blk)
        return

    if blk.type == "margin_note" and sec_kind in {"chapter", "epistolary_part"} and (state.pending_prose or blk.page_number > 1):
        state.pending_prose.append(blk)
        return

    state.resolve_pending_prose(sec_kind)
    if blk.type == "ornamental_break":
        state.saw_ornamental_break = True
    state.pending_non_letter.append(blk)


def _handle_active_block(
    state: _StreamState,
    sec_blocks: list[BlockIR],
    idx: int,
    directives: _MapDirectives,
    sec_kind: SectionKind,
) -> None:
    blk = sec_blocks[idx]
    next_blk = sec_blocks[idx + 1] if idx + 1 < len(sec_blocks) else None

    is_post_head = blk.type in {"heading", "illustration", "caption"} and (
        blk.heading_level == 1
        or (state.has_closer and _editorial_run_precedes_boundary(sec_blocks, idx, directives))
        or (
            state.has_body
            and blk.type == "heading"
            and next_blk is not None
            and next_blk.type == "editorial"
            and _editorial_run_precedes_boundary(sec_blocks, idx + 1, directives)
        )
    )
    if is_post_head:
        state.close_active_letter()
        state.pending_non_letter.append(blk)
        return

    if blk.type == "ornamental_break":
        opens_after = (
            next_blk is None
            or next_blk.type in (EPISTOLARY_OPENER_TYPES | {"editorial", "heading"})
            or next_blk.epistolary_transition == "starts_new_letter"
            or next_blk.block_id in directives.force_split
        )
        if state.has_closer or opens_after:
            state.close_active_letter()
            state.saw_ornamental_break = True
            state.pending_non_letter.append(blk)
        else:
            state.append_to_letter(blk)
        return

    if blk.type == "editorial":
        is_body_cont = (
            blk.continues_from_prev
            and bool(state.active_blocks)
            and state.active_blocks[-1].type in LETTER_BODY_TYPES
        )
        if is_body_cont or (not state.has_body and not state.has_closer):
            state.append_to_letter(blk)
        elif state.has_closer or _editorial_run_precedes_boundary(sec_blocks, idx, directives):
            state.close_active_letter()
            state.pending_non_letter.append(blk)
        else:
            state.append_to_letter(blk)
        return

    if (blk.type == "letter_header" and (state.has_body or state.has_closer)) or (
        blk.type == "salutation" and state.has_closer
    ):
        lead_margins = state.pop_leading_margin_notes(blk.page_number)
        state.start_letter([*lead_margins, blk], confidence="high")
        return

    if blk.type == "dateline" and _is_dateline_split_candidate(state):
        if _is_bottom_dateline(state, next_blk, directives):
            state.append_to_letter(blk)
        else:
            lead_margins = state.pop_leading_margin_notes(blk.page_number)
            state.start_letter([*lead_margins, blk], confidence="high")
        return

    if state.has_closer and not state.in_postscript and blk.type in {"body", "verse"}:
        if (
            next_blk is None
            or next_blk.type in {"signature", "valediction", "postscript", "ornamental_break", "letter_header", "heading", "editorial"}
            or next_blk.epistolary_transition == "starts_new_letter"
        ):
            state.in_postscript = True
            state.append_to_letter(blk)
            return
        state.close_active_letter()
        if sec_kind in {"chapter", "epistolary_part"}:
            state.pending_prose.append(blk)
        else:
            state.pending_non_letter.append(blk)
        return

    state.append_to_letter(blk)


def _segment_section_blocks(
    sec_blocks: list[BlockIR],
    sec_kind: SectionKind,
    directives: _MapDirectives,
) -> tuple[list[SectionItem], list[tuple[list[BlockIR], BoundaryConfidence]]]:
    state = _StreamState()
    for idx, blk in enumerate(sec_blocks):
        next_blk = sec_blocks[idx + 1] if idx + 1 < len(sec_blocks) else None
        bid = blk.block_id
        is_forbid = bid in directives.forbid_split or bid in directives.enclosure_ids

        if bid in directives.force_split:
            lead_margins = state.pop_leading_margin_notes(blk.page_number)
            state.resolve_pending_prose(sec_kind)
            state.start_letter([*lead_margins, blk], confidence="human_mapped")
        elif blk.epistolary_transition == "starts_new_letter" and not is_forbid:
            if _continues_header_only_letter(state, blk) or (blk.type == "postscript" and state.active_blocks):
                state.append_to_letter(blk)
            else:
                lead_margins = state.pop_leading_margin_notes(blk.page_number)
                state.resolve_pending_prose(sec_kind)
                state.start_letter([*lead_margins, blk], confidence="high")
        elif is_forbid:
            if not state.active_blocks and state.pending_prose:
                prose = list(state.pending_prose)
                state.pending_prose.clear()
                state.start_letter(prose, confidence="human_mapped")
            if state.active_blocks:
                if bid in directives.forbid_split:
                    state.confidence = "human_mapped"
                state.append_to_letter(blk)
            else:
                state.start_letter([blk], confidence="human_mapped")
        elif state.active_blocks and (state.enclosure_depth > 0 or blk.epistolary_transition == "starts_enclosure"):
            state.append_to_letter(blk)
        elif not state.active_blocks:
            _handle_inactive_block(state, blk, next_blk, sec_kind, directives)
        else:
            _handle_active_block(state, sec_blocks, idx, directives, sec_kind)

        if bid in directives.force_close_after and state.active_blocks:
            state.confidence = "human_mapped"
            state.close_active_letter()

    state.close_active_letter()
    state.resolve_pending_prose(sec_kind)
    state.flush_non_letter()
    return state.items, state.raw_letters


def _attach_framing_side(
    prev_lu: LetterUnit | None,
    next_lu: LetterUnit | None,
    blocks: list[BlockIR],
) -> None:
    if not any(b.type == "editorial" for b in blocks):
        return
    ed_ids = [b.block_id for b in blocks if b.type == "editorial" or (b.type == "heading" and b.heading_level != 1)]
    if prev_lu is None and next_lu is not None:
        next_lu.preceding_editorial_block_ids = list(dict.fromkeys([*next_lu.preceding_editorial_block_ids, *ed_ids]))
    elif next_lu is None and prev_lu is not None:
        prev_lu.following_editorial_block_ids = list(dict.fromkeys([*prev_lu.following_editorial_block_ids, *ed_ids]))
    elif prev_lu is not None and next_lu is not None:
        orn_idx = next(
            (
                i
                for i, b in enumerate(blocks)
                if b.type == "ornamental_break"
                and any(x.type == "editorial" for x in blocks[:i])
                and any(x.type == "editorial" for x in blocks[i + 1 :])
                and i + 1 < len(blocks)
                and blocks[i + 1].type != "heading"
            ),
            None,
        )
        if orn_idx is not None:
            _attach_framing_side(prev_lu, None, blocks[:orn_idx])
            _attach_framing_side(None, next_lu, blocks[orn_idx + 1 :])
        elif any(b.type == "heading" and b.heading_level != 1 for b in blocks) or (
            blocks[-1].page_number == prev_lu.end_page and next_lu.start_page > prev_lu.end_page
        ):
            prev_lu.following_editorial_block_ids = list(dict.fromkeys([*prev_lu.following_editorial_block_ids, *ed_ids]))
        else:
            next_lu.preceding_editorial_block_ids = list(dict.fromkeys([*next_lu.preceding_editorial_block_ids, *ed_ids]))


def _link_editorial_framing(
    items: list[SectionItem],
    letters_by_id: dict[str, LetterUnit],
) -> None:
    for idx, item in enumerate(items):
        if item.kind != "blocks":
            continue
        prev_lu = (
            letters_by_id.get(str(items[idx - 1].letter_id))
            if idx > 0 and items[idx - 1].kind == "letter"
            else None
        )
        next_lu = (
            letters_by_id.get(str(items[idx + 1].letter_id))
            if idx + 1 < len(items) and items[idx + 1].kind == "letter"
            else None
        )
        _attach_framing_side(prev_lu, next_lu, item.blocks)


def _assign_unavailable_pages(
    sections: list[BookSection],
    failed_pages: set[int],
    all_page_numbers: list[int],
) -> None:
    if not sections:
        return
    min_p = min(all_page_numbers) if all_page_numbers else 1
    max_p = max(all_page_numbers) if all_page_numbers else 1
    sections[0].start_page = min(sections[0].start_page, min_p)
    sections[-1].end_page = max(sections[-1].end_page, max_p)
    for pnum in sorted(failed_pages):
        target = next((s for s in sections if s.start_page <= pnum <= s.end_page), None) or min(
            sections, key=lambda s: min(abs(s.start_page - pnum), abs(s.end_page - pnum))
        )
        target.start_page = min(target.start_page, pnum)
        target.end_page = max(target.end_page, pnum)
        if pnum not in target.unavailable_pages:
            target.unavailable_pages.append(pnum)


def segment_epistolary_collection(
    content_blocks: list[BlockIR],
    *,
    book_title: str = "",
    all_page_numbers: list[int] | None = None,
    failed_pages: set[int] | None = None,
    letter_map: dict[str, Any] | None = None,
    chapter_map: dict[str, Any] | None = None,
) -> tuple[list[BookSection], list[LetterUnit]]:
    """Compile non-furniture/non-footnote blocks into `BookSection`s and `LetterUnit`s."""
    directives = _build_map_directives(content_blocks, letter_map)
    raw_sections = _partition_sections(content_blocks, directives, book_title, chapter_map)

    sections: list[BookSection] = []
    all_letters: list[LetterUnit] = []
    used_slugs: set[str] = set()
    seq_counter = 0

    for sec_i, (sec_kind, title_tag, title_pln, sec_blocks) in enumerate(raw_sections, start=1):
        sec_id = f"sec-{sec_i:02d}"
        raw_items, raw_letters = _segment_section_blocks(sec_blocks, sec_kind, directives)

        placeholder_map: dict[str, LetterUnit] = {}
        for raw_idx, (l_blocks, base_conf) in enumerate(raw_letters):
            seq_counter += 1
            default_lid = f"letter-{seq_counter:04d}"
            first_bid = l_blocks[0].block_id
            exp_ovr = (
                directives.explicit_by_start_block.get(first_bid)
                or directives.explicit_by_seq.get(seq_counter)
                or directives.explicit_by_id.get(default_lid)
            )
            letter_id = str((exp_ovr or {}).get("letter_id") or default_lid)
            meta = extract_epistolary_metadata(
                l_blocks,
                enclosure_block_ids=directives.enclosure_ids,
                explicit_override=exp_ovr,
            )
            slug = build_letter_slug(
                seq_counter,
                meta,
                explicit_slug=(exp_ovr or {}).get("slug"),
                used_slugs=used_slugs,
            )
            conf = cast(
                BoundaryConfidence,
                str((exp_ovr or {}).get("boundary_confidence") or base_conf),
            )
            lu = LetterUnit(
                letter_id=letter_id,
                slug=slug,
                sequence_index=seq_counter,
                section_id=sec_id,
                start_page=l_blocks[0].page_number,
                end_page=l_blocks[-1].page_number,
                start_folio=l_blocks[0].printed_folio,
                end_folio=l_blocks[-1].printed_folio,
                block_ids=[b.block_id for b in l_blocks],
                blocks=l_blocks,
                metadata=meta,
                boundary_confidence=conf,
            )
            placeholder_map[f"__raw_letter_{raw_idx}__"] = lu
            all_letters.append(lu)

        final_items: list[SectionItem] = []
        for it in raw_items:
            if it.kind == "letter" and it.letter_id in placeholder_map:
                lu_ref = placeholder_map[str(it.letter_id)]
                final_items.append(SectionItem(kind="letter", letter_id=lu_ref.letter_id, blocks=lu_ref.blocks))
            else:
                final_items.append(it)

        _link_editorial_framing(final_items, {lu.letter_id: lu for lu in placeholder_map.values()})

        page_nums = [b.page_number for b in sec_blocks] or (all_page_numbers or [1])
        sections.append(
            BookSection(
                section_id=sec_id,
                kind=sec_kind,
                title_tagged=title_tag,
                title_plain=title_pln,
                start_page=min(page_nums),
                end_page=max(page_nums),
                items=final_items,
            )
        )

    _assign_unavailable_pages(sections, failed_pages or set(), all_page_numbers or [1])
    return sections, all_letters
