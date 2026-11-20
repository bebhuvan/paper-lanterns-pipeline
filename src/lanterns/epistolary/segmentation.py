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


def _pop_slice(buf: list[BlockIR], start: int) -> list[BlockIR]:
    out = list(buf[start:])
    del buf[start:]
    return out


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
            idx = len(self.active_blocks) - 1
            while idx >= 0 and self.active_blocks[idx].type == "margin_note" and self.active_blocks[idx].page_number == page_number:
                idx -= 1
            can_detach = idx >= 0 and (self.has_closer or self.active_blocks[idx].page_number < page_number)
            return _pop_slice(self.active_blocks, idx + 1) if can_detach else []
        if self.pending_prose and all(b.type == "margin_note" and b.page_number == page_number for b in self.pending_prose):
            return _pop_slice(self.pending_prose, 0)
        idx = len(self.pending_non_letter) - 1
        while idx >= 0 and self.pending_non_letter[idx].type == "margin_note" and self.pending_non_letter[idx].page_number == page_number:
            idx -= 1
        return _pop_slice(self.pending_non_letter, idx + 1)

    def resolve_pending_prose(self, sec_kind: SectionKind) -> None:
        if not self.pending_prose:
            return
        is_mid_vol = (
            sec_kind == "epistolary_part"
            and not self.raw_letters
            and any(b.type in LETTER_BODY_TYPES and b.page_number > 1 for b in self.pending_prose)
        )
        if is_mid_vol:
            self.start_letter(_pop_slice(self.pending_prose, 0), confidence="ambiguous")
            self.close_active_letter()
        else:
            self.flush_pending_prose_as_blocks()

    def close_active_letter(self) -> None:
        if not self.active_blocks:
            return
        self.flush_non_letter()
        placeholder_id = f"__raw_letter_{len(self.raw_letters)}__"
        blocks_copy = list(self.active_blocks)
        self.raw_letters.append((blocks_copy, self.confidence))
        self.items.append(SectionItem(kind="letter", letter_id=placeholder_id, blocks=blocks_copy))
        self.active_blocks.clear()
        self.enclosure_depth = 0
        self.has_body = self.has_closer = self.has_dateline = self.in_postscript = self.saw_ornamental_break = False
        self.confidence = "high"

    def start_letter(self, initial: list[BlockIR], confidence: BoundaryConfidence = "high") -> None:
        self.close_active_letter()
        self.flush_non_letter()
        self.active_blocks = []
        self.confidence = confidence
        for blk in initial:
            self.append_to_letter(blk)

    def append_to_letter(self, blk: BlockIR) -> None:
        is_enc_start = blk.epistolary_transition == "starts_enclosure" and bool(self.active_blocks)
        self.active_blocks.append(blk)
        self.enclosure_depth += int(is_enc_start)
        if blk.epistolary_transition == "ends_enclosure" and self.enclosure_depth > 0:
            self.enclosure_depth -= 1
        if self.enclosure_depth == 0:
            is_ed_quote = (
                blk.type == "blockquote"
                and not (self.has_body or self.has_dateline)
                and len(self.active_blocks) >= 2
                and self.active_blocks[-2].type == "editorial"
            )
            self.has_body = self.has_body or (blk.type in LETTER_BODY_TYPES and not is_ed_quote)
            self.has_closer = self.has_closer or blk.type in {"valediction", "signature"}
            self.has_dateline = self.has_dateline or blk.type == "dateline"
            self.in_postscript = self.in_postscript or blk.type == "postscript"


def _slice_block_range(blocks: list[BlockIR], id_to_idx: dict[str, int], start_id: str, end_id: str) -> list[str]:
    s_i, e_i = id_to_idx.get(start_id), id_to_idx.get(end_id)
    if s_i is not None and e_i is not None and s_i <= e_i:
        return [b.block_id for b in blocks[s_i : e_i + 1]]
    return []


def _resolve_span_ids(exp: dict[str, Any], blocks: list[BlockIR], id_to_idx: dict[str, int]) -> list[str]:
    if isinstance(exp.get("block_ids"), list) and exp["block_ids"]:
        return [str(x) for x in exp["block_ids"]]
    s_id = str(exp.get("start_block_id") or "")
    e_id = str(exp.get("end_block_id") or "")
    return _slice_block_range(blocks, id_to_idx, s_id, e_id)


def _build_map_directives(blocks: list[BlockIR], letter_map: dict[str, Any] | None) -> _MapDirectives:
    directives = _MapDirectives()
    if not letter_map:
        return directives
    directives.force_split = {str(x) for x in (letter_map.get("force_split_before_block_ids") or [])}
    directives.forbid_split = {str(x) for x in (letter_map.get("forbid_split_before_block_ids") or [])}
    id_to_idx = {b.block_id: idx for idx, b in enumerate(blocks)}
    directives.enclosure_ids = {
        bid
        for rng in (letter_map.get("enclosure_block_ranges") or [])
        for bid in _slice_block_range(
            blocks, id_to_idx, str(rng.get("start_block_id") or ""), str(rng.get("end_block_id") or "")
        )
    }

    exps = list(letter_map.get("explicit_letters") or [])
    directives.explicit_by_id = {str(exp["letter_id"]): exp for exp in exps if exp.get("letter_id")}
    directives.explicit_by_seq = {int(exp["sequence_index"]): exp for exp in exps if exp.get("sequence_index") is not None}
    for exp in exps:
        span_ids = _resolve_span_ids(exp, blocks, id_to_idx)
        s_id = (span_ids and span_ids[0]) or str(exp.get("start_block_id") or "")
        if s_id:
            directives.force_split.add(s_id)
            directives.explicit_by_start_block[s_id] = exp
        if span_ids:
            directives.forbid_split.update(span_ids[1:])
            directives.force_close_after.add(span_ids[-1])
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
        is_idx = any(b.type == "index_entry" for b in blocks)
        is_fm = any(b.type in {"imprint", "toc_entry"} or _is_roman_frontmatter_block(b) for b in blocks)
        kind, title = next(
            ((k, t) for cond, k, t in ((is_idx, "index", "Index"), (is_fm, "frontmatter", "Frontmatter")) if cond),
            ("epistolary_part", book_title or "Correspondence"),
        )
        return [(kind, title, title, list(blocks))]

    ep_start_page = blocks[first_ep].page_number
    fm_cut = max(
        (
            idx + 1
            for idx, blk in enumerate(blocks[:first_ep])
            if blk.type in {"imprint", "toc_entry"} or (_is_roman_frontmatter_block(blk) and blk.page_number < ep_start_page)
        ),
        default=0,
    )
    while 0 < fm_cut < first_ep and blocks[fm_cut].page_number == blocks[fm_cut - 1].page_number and blocks[fm_cut].type != "editorial":
        fm_cut += 1

    last_ep = max(i for i, b in enumerate(blocks) if _is_epistolary_signal(b, directives))
    idx_cut = next((i for i in range(last_ep + 1, len(blocks)) if blocks[i].type == "index_entry"), len(blocks))
    idx_cut -= int(last_ep + 1 < idx_cut < len(blocks) and blocks[idx_cut - 1].type == "heading")

    raw_parts: list[tuple[SectionKind, str, str, list[BlockIR]]] = [
        ("frontmatter", "Frontmatter", "Frontmatter", blocks[:fm_cut]),
        ("epistolary_part", book_title or "Correspondence", book_title or "Correspondence", blocks[fm_cut:idx_cut]),
    ]
    out = [item for item in raw_parts if item[3]]
    if idx_cut < len(blocks):
        tail = blocks[idx_cut:]
        t_tag, t_pln = (tail[0].tagged_text, tail[0].plain_text) if tail[0].type == "heading" else ("Index", "Index")
        out.append(("index", t_tag, t_pln, tail))
    return out


def _is_frontmatter_slice(
    idx: int,
    sl_blks: list[BlockIR],
    fm_cues: list[bool],
    last_fm_cue: int | None,
    first_ep_slice: int | None,
) -> bool:
    return (
        fm_cues[idx]
        or (last_fm_cue is not None and idx <= last_fm_cue)
        or (first_ep_slice is not None and idx < first_ep_slice and not any(_is_arabic_folio_block(b) for b in sl_blks))
    )


def _slice_has_true_epistolary_start(
    idx: int,
    sl_blks: list[BlockIR],
    last_toc_slice: int | None,
    directives: _MapDirectives,
) -> bool:
    is_pre_arabic_fm = (last_toc_slice is not None and idx <= last_toc_slice) or (
        any(_is_roman_frontmatter_block(b) for b in sl_blks) and not any(_is_arabic_folio_block(b) for b in sl_blks)
    )
    if is_pre_arabic_fm:
        return any(
            b.type == "letter_header" or b.epistolary_transition == "starts_new_letter" or b.block_id in directives.force_split
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
    has_ep = [_slice_has_true_epistolary_start(i, sl_blks, last_toc_slice, directives) for i, (_, _, sl_blks) in enumerate(raw_slices)]
    first_ep_slice = next((i for i, flag in enumerate(has_ep) if flag), None)
    last_ep_slice = max((i for i, flag in enumerate(has_ep) if flag), default=None)
    fm_cues = [
        any(b.type in {"imprint", "toc_entry"} or _is_roman_frontmatter_block(b) for b in sl_blks) and not has_ep[i]
        for i, (_, _, sl_blks) in enumerate(raw_slices)
    ]
    last_fm_cue = max((i for i, flag in enumerate(fm_cues) if flag and (first_ep_slice is None or i < first_ep_slice)), default=None)

    typed: list[tuple[SectionKind, str, str, list[BlockIR]]] = []
    for idx, (t_tag, t_pln, sl_blks) in enumerate(raw_slices):
        kind_specs: tuple[tuple[bool, SectionKind], ...] = (
            (any(b.type == "index_entry" for b in sl_blks) and not has_ep[idx], "index"),
            (_is_frontmatter_slice(idx, sl_blks, fm_cues, last_fm_cue, first_ep_slice), "frontmatter"),
            (last_ep_slice is not None and idx > last_ep_slice and any(b.type == "imprint" for b in sl_blks), "backmatter"),
        )
        kind: SectionKind = next((k for cond, k in kind_specs if cond), "chapter")
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
        if not (blk.type == "heading" and blk.heading_level == 1 and blk.block_id not in enclosure_ids):
            idx += 1
            continue
        end_run = last_h1 = idx
        while (
            end_run + 1 < len(blocks)
            and blocks[end_run + 1].type == "heading"
            and blocks[end_run + 1].page_number == blk.page_number
            and blocks[end_run + 1].block_id not in enclosure_ids
        ):
            end_run += 1
            last_h1 = end_run if blocks[end_run].heading_level == 1 else last_h1
        span = blocks[idx : last_h1 + 1]
        starts.append((idx, " ".join(" ".join(b.tagged_text.split()) for b in span), " ".join(" ".join(b.plain_text.split()) for b in span)))
        idx = last_h1 + 1
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
            s_id = str(spec.get("start_block_id") or "")
            if s_id in id_to_idx:
                sl = blocks[id_to_idx[s_id] : id_to_idx.get(str(spec.get("end_block_id") or ""), len(blocks) - 1) + 1]
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
        title = book_title or "Correspondence"
        lead_choices: dict[bool, tuple[SectionKind, str, str, list[BlockIR]]] = {
            True: ("epistolary_part", title, title, lead),
            False: ("frontmatter", "Frontmatter", "Frontmatter", lead),
        }
        out.append(lead_choices[any(_is_epistolary_signal(b, directives) for b in lead)])

    end_bounds = [*[s[0] for s in h1_starts[1:]], len(blocks)]
    raw_h1 = [(t_tag, t_pln, blocks[s_i:e_i]) for (s_i, t_tag, t_pln), e_i in zip(h1_starts, end_bounds, strict=True)]
    out.extend(_classify_h1_slices(raw_h1, directives))
    return out


def _editorial_run_precedes_boundary(
    sec_blocks: list[BlockIR],
    start_idx: int,
    directives: _MapDirectives,
) -> bool:
    idx = start_idx
    while idx < len(sec_blocks) and sec_blocks[idx].type in {
        "editorial",
        "ornamental_break",
        "illustration",
        "facsimile_plate",
        "caption",
    }:
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
        state.start_letter([*_pop_slice(state.pending_prose, 0), blk], confidence=conf)
        return

    is_chapter_prose = sec_kind in {"chapter", "epistolary_part"} and (
        blk.type in {"body", "verse", "blockquote"}
        or (blk.type == "margin_note" and (bool(state.pending_prose) or blk.page_number > 1))
    )
    if is_chapter_prose:
        state.pending_prose.append(blk)
        return

    state.resolve_pending_prose(sec_kind)
    state.saw_ornamental_break = state.saw_ornamental_break or blk.type == "ornamental_break"
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

    is_post_head = blk.type in {"heading", "illustration", "facsimile_plate", "caption"} and (
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
            return
    elif blk.type == "editorial":
        is_body_cont = (
            blk.continues_from_prev and bool(state.active_blocks) and state.active_blocks[-1].type in LETTER_BODY_TYPES
        )
        if not is_body_cont and (state.has_closer or ((state.has_body or state.has_closer) and _editorial_run_precedes_boundary(sec_blocks, idx, directives))):
            state.close_active_letter()
            state.pending_non_letter.append(blk)
            return
    elif (blk.type == "letter_header" and (state.has_body or state.has_closer)) or (
        blk.type == "salutation" and state.has_closer
    ) or (blk.type == "dateline" and _is_dateline_split_candidate(state) and not _is_bottom_dateline(state, next_blk, directives)):
        lead_margins = state.pop_leading_margin_notes(blk.page_number)
        state.start_letter([*lead_margins, blk], confidence="high")
        return
    elif state.has_closer and not state.in_postscript and blk.type in {"body", "verse"}:
        continues_ps = (
            next_blk is None
            or next_blk.type in {"signature", "valediction", "postscript", "ornamental_break", "letter_header", "heading", "editorial"}
            or next_blk.epistolary_transition == "starts_new_letter"
        )
        if continues_ps:
            state.in_postscript = True
        else:
            state.close_active_letter()
            target_buf = state.pending_prose if sec_kind in {"chapter", "epistolary_part"} else state.pending_non_letter
            target_buf.append(blk)
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
        is_forced_start = bid in directives.force_split or (
            blk.epistolary_transition == "starts_new_letter"
            and not is_forbid
            and not (_continues_header_only_letter(state, blk) or (blk.type == "postscript" and bool(state.active_blocks)))
        )

        if is_forced_start:
            lead_margins = state.pop_leading_margin_notes(blk.page_number)
            state.resolve_pending_prose(sec_kind)
            state.start_letter([*lead_margins, blk], confidence="human_mapped" if bid in directives.force_split else "high")
        elif is_forbid:
            if not state.active_blocks:
                state.start_letter([*_pop_slice(state.pending_prose, 0), blk], confidence="human_mapped")
            else:
                if bid in directives.forbid_split:
                    state.confidence = "human_mapped"
                state.append_to_letter(blk)
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


def _append_framing_ids(letter: LetterUnit, attr: str, ed_ids: list[str]) -> None:
    cur = getattr(letter, attr)
    setattr(letter, attr, list(dict.fromkeys([*cur, *ed_ids])))


def _attach_framing_side(
    prev_lu: LetterUnit | None,
    next_lu: LetterUnit | None,
    blocks: list[BlockIR],
) -> None:
    if not any(b.type == "editorial" for b in blocks):
        return
    ed_ids = [b.block_id for b in blocks if b.type == "editorial" or (b.type == "heading" and b.heading_level != 1)]
    if prev_lu is None and next_lu is not None:
        _append_framing_ids(next_lu, "preceding_editorial_block_ids", ed_ids)
    elif next_lu is None and prev_lu is not None:
        _append_framing_ids(prev_lu, "following_editorial_block_ids", ed_ids)
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
            _append_framing_ids(prev_lu, "following_editorial_block_ids", ed_ids)
        else:
            _append_framing_ids(next_lu, "preceding_editorial_block_ids", ed_ids)


def _adjacent_letter(
    items: list[SectionItem],
    pos: int,
    letters_by_id: dict[str, LetterUnit],
) -> LetterUnit | None:
    if 0 <= pos < len(items) and items[pos].kind == "letter":
        return letters_by_id.get(str(items[pos].letter_id))
    return None


def _link_editorial_framing(
    items: list[SectionItem],
    letters_by_id: dict[str, LetterUnit],
) -> None:
    for idx, item in enumerate(items):
        if item.kind != "blocks":
            continue
        _attach_framing_side(
            _adjacent_letter(items, idx - 1, letters_by_id),
            _adjacent_letter(items, idx + 1, letters_by_id),
            item.blocks,
        )


def _assign_unavailable_pages(
    sections: list[BookSection],
    failed_pages: set[int],
    all_page_numbers: list[int],
) -> None:
    if not sections:
        return
    sections[0].start_page = min(sections[0].start_page, min(all_page_numbers, default=1))
    sections[-1].end_page = max(sections[-1].end_page, max(all_page_numbers, default=1))
    for pnum in sorted(failed_pages):
        target = next((s for s in sections if s.start_page <= pnum <= s.end_page), None) or min(
            sections, key=lambda s: min(abs(s.start_page - pnum), abs(s.end_page - pnum))
        )
        target.start_page = min(target.start_page, pnum)
        target.end_page = max(target.end_page, pnum)
        target.unavailable_pages = list(dict.fromkeys([*target.unavailable_pages, pnum]))


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
