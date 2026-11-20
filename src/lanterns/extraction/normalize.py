"""Normalize `PageExtractWire` observations into canonical `PageIR` and `BlockIR`.

Handles:
  - Diplomatic NFC normalization (`U+FB00..U+FB06` presentation ligatures only)
  - Stripping anti-recitation `[L01]`..`[L999]` scanline markers while preserving `\\n`
  - Constrained inline tag parsing into `(tagged_text, plain_text, list[StyleSpan])`
  - Clean two-column reading-order sorting ONLY when `two_column_left` / `two_column_right`
    appear, NEVER scrambling single-column right-aligned datelines or signatures
  - Canonicalizing `footnote.note_label` across numeric, symbol (`*`, `†`, `‡`), and alpha (`a`, `b`) registers
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, cast

from lanterns.core.models import (
    BLOCK_ALIGNMENTS,
    BLOCK_TYPES,
    EPISTOLARY_TRANSITIONS,
    FURNITURE_BLOCK_TYPES,
    BlockAlignment,
    BlockIR,
    BlockType,
    EpistolaryTransition,
    PageIR,
    TableCell,
)
from lanterns.core.tags import extract_inline_markers, parse_tagged_text
from lanterns.core.unicode_norm import (
    CONTROL_CHARS_WARNING,
    is_binder_signature,
    normalize_diplomatic_text,
    strip_control_chars,
)

_INLINE_SCANLINE_MARKER_RE = re.compile(r"(?<!\n)[ \t]*\[L\d{1,3}\][ \t]*")
_LEADING_SCANLINE_MARKER_RE = re.compile(r"(?m)^[ \t]*\[L\d{1,3}\][ \t]*")
_TYPO_FN_SYMBOLS = frozenset({"*", "**", "†", "††", "‡", "§", "‖", "¶"})
_LEADING_PLAIN_SYMBOL_RE = re.compile(r"^\s*([*†‡§‖¶]{1,2})\s+\S")
_LEADING_NOTE_MARKER_RE = re.compile(
    r"^\s*(?:<sup>\s*([*†‡§‖¶]+|\d+|[a-zA-Z])\s*</sup>|([*†‡§‖¶]+|\d+[.)](?=\s)|[a-zA-Z]\)))\s*"
)
_LABEL_TAG_RE = re.compile(r"</?(?:sup|sub|i|b|sc|unclear)>", re.IGNORECASE)
_LINE_BREAK_EPISTOLARY_TYPES = frozenset(
    {"dateline", "salutation", "valediction", "signature", "address_block", "verse", "epigraph"}
)


def strip_scanline_markers(text: str) -> str:
    """Strip anti-recitation `[L01]`..`[L999]` markers while preserving `\\n` line breaks."""
    if "[L" not in text:
        return text
    cleaned = _LEADING_SCANLINE_MARKER_RE.sub("", text)
    cleaned = _INLINE_SCANLINE_MARKER_RE.sub("\n", cleaned)
    return _LEADING_SCANLINE_MARKER_RE.sub("", cleaned).strip("\n")


def split_note_label_markers(note_label: str | None) -> tuple[str, ...]:
    """Expand single or comma-consolidated `note_label` into bare marker strings."""
    if note_label is None:
        return ()
    raw = normalize_diplomatic_text(str(note_label)).strip()
    if not raw:
        return ()
    if "," in raw:
        parts = [p.strip().strip("()[]").rstrip(".") for p in raw.split(",") if p.strip()]
        if len(parts) >= 2 and all(
            p in _TYPO_FN_SYMBOLS or (p.isdigit() and len(p) <= 3) or (len(p) == 1 and p.isalpha()) for p in parts
        ):
            return tuple(dict.fromkeys(parts))
    cleaned = raw.strip("()[]").rstrip(".")
    return (cleaned,) if cleaned else ()


def canonicalize_footnote_label(
    note_label: str | None,
    plain_text: str,
    tagged_text: str = "",
    *,
    continues_from_prev: bool = False,
) -> str | None:
    """Return bare canonical `note_label` (`'1'`, `'*'`, `'†'`, `'a'`), or `None` for continuations."""
    if continues_from_prev and not note_label:
        return None
    bare = _LABEL_TAG_RE.sub("", normalize_diplomatic_text(str(note_label or ""))).strip().strip("\"'“”‘’")
    bare = bare.strip("()[]").rstrip(".")
    txt = (plain_text or "").strip()
    m_sym = _LEADING_PLAIN_SYMBOL_RE.match(txt)
    if m_sym and not txt.startswith("* *"):
        lead_sym = m_sym.group(1)
        if not bare or (bare != lead_sym and not bare.isdigit()):
            return lead_sym
    if bare:
        return bare
    if not continues_from_prev and tagged_text:
        m_lead = _LEADING_NOTE_MARKER_RE.match(tagged_text)
        if m_lead:
            inferred = (m_lead.group(1) or m_lead.group(2) or "").strip("(). ")
            return inferred or None
    return None


def _normalize_bbox(raw_bbox: Any) -> list[int]:
    if isinstance(raw_bbox, list) and len(raw_bbox) == 4:
        vals = [max(0, min(1000, int(v))) for v in raw_bbox]
        return [min(vals[0], vals[2]), min(vals[1], vals[3]), max(vals[0], vals[2]), max(vals[1], vals[3])]
    return [0, 0, 1000, 1000]


def _normalize_cells(raw_cells: Any, btype: str) -> list[TableCell] | None:
    if btype != "table_row" or not isinstance(raw_cells, list) or not raw_cells:
        return None
    cells: list[TableCell] = []
    for item in raw_cells:
        if isinstance(item, dict):
            raw_cell_text = strip_scanline_markers(normalize_diplomatic_text(str(item.get("text") or "")))
            ctagged, _, _ = parse_tagged_text(raw_cell_text)
            cells.append(
                TableCell(
                    text=ctagged.strip(),
                    colspan=max(1, int(item.get("colspan") or 1)),
                    rowspan=max(1, int(item.get("rowspan") or 1)),
                )
            )
    return cells or None


def sort_two_column_blocks(blocks: list[BlockIR]) -> list[BlockIR]:
    """Order `two_column_left` runs before `two_column_right` runs ONLY when explicit two-column alignments appear.

    Single-column pages (including right-aligned datelines, cascading valedictions, and signatures)
    are returned in their exact original sequence without modification.
    """
    if not any(b.alignment in {"two_column_left", "two_column_right"} for b in blocks):
        return blocks

    reordered: list[BlockIR] = []
    idx = 0
    total = len(blocks)
    while idx < total:
        if blocks[idx].alignment not in {"two_column_left", "two_column_right"}:
            reordered.append(blocks[idx])
            idx += 1
            continue
        end = idx
        while end < total and blocks[end].alignment in {"two_column_left", "two_column_right"}:
            end += 1
        col_slice = blocks[idx:end]
        left_col = sorted(
            (b for b in col_slice if b.alignment == "two_column_left"),
            key=lambda b: (b.bbox[1], b.reading_order),
        )
        right_col = sorted(
            (b for b in col_slice if b.alignment == "two_column_right"),
            key=lambda b: (b.bbox[1], b.reading_order),
        )
        reordered.extend(left_col)
        reordered.extend(right_col)
        idx = end

    final_blocks: list[BlockIR] = []
    for order_idx, blk in enumerate(reordered):
        page_num = blk.page_number
        final_blocks.append(
            blk.model_copy(
                update={
                    "reading_order": order_idx,
                    "block_id": f"p{page_num:04d}-b{order_idx + 1:04d}",
                }
            )
        )
    return final_blocks


def _coerce_block_type(raw_type: Any) -> BlockType:
    val = str(raw_type or "body").strip()
    if val in BLOCK_TYPES:
        return cast(BlockType, val)
    return "body"


def _coerce_alignment(raw_align: Any) -> BlockAlignment:
    val = str(raw_align or "left").strip()
    if val in BLOCK_ALIGNMENTS:
        return cast(BlockAlignment, val)
    return "left"


def _coerce_transition(raw_trans: Any) -> EpistolaryTransition:
    val = str(raw_trans or "none").strip()
    if val in EPISTOLARY_TRANSITIONS:
        return cast(EpistolaryTransition, val)
    return "none"


def _resolve_heading_level(btype: str, raw_hl: Any) -> int | None:
    if btype != "heading":
        return None
    if isinstance(raw_hl, int) and 1 <= raw_hl <= 6:
        return int(raw_hl)
    return 2


def _build_single_block_ir(
    raw: dict[str, Any],
    *,
    idx: int,
    page_number: int,
    printed_folio: str | None,
    provider: str,
    model: str,
    harness: str,
    source_page_sha256: str,
) -> tuple[BlockIR, str | None]:
    btype = _coerce_block_type(raw.get("type"))
    alignment = _coerce_alignment(raw.get("alignment"))
    transition = _coerce_transition(raw.get("epistolary_transition"))
    cleaned_text = strip_scanline_markers(normalize_diplomatic_text(str(raw.get("text") or "")))
    canonical_tagged, plain_text, spans = parse_tagged_text(cleaned_text)

    eff_folio = printed_folio
    if btype == "folio" and eff_folio is None and plain_text.strip():
        cand = plain_text.strip()
        if not is_binder_signature(cand, page_number):
            eff_folio = cand

    cells = _normalize_cells(raw.get("cells"), btype)
    if btype == "table_row" and cells and not plain_text.strip():
        canonical_tagged, plain_text, spans = parse_tagged_text(" | ".join(c.text for c in cells))

    cont_prev = bool(raw.get("continues_from_prev", False))
    note_label = (
        canonicalize_footnote_label(raw.get("note_label"), plain_text, canonical_tagged, continues_from_prev=cont_prev)
        if btype in {"footnote", "margin_note"}
        else None
    )
    preserve_lb = bool(raw.get("line_breaks_preserved", False)) or (
        btype in _LINE_BREAK_EPISTOLARY_TYPES and ("\n" in canonical_tagged or btype in {"verse", "epigraph"})
    )
    blk = BlockIR(
        block_id=f"p{page_number:04d}-b{idx + 1:04d}",
        page_number=page_number,
        reading_order=idx,
        printed_folio=eff_folio,
        type=btype,
        bbox=_normalize_bbox(raw.get("bbox")),
        tagged_text=canonical_tagged,
        plain_text=plain_text,
        spans=spans,
        alignment=alignment,
        epistolary_transition=transition,
        line_breaks_preserved=preserve_lb,
        heading_level=_resolve_heading_level(btype, raw.get("heading_level")),
        note_label=note_label,
        continues_from_prev=cont_prev,
        continues_to_next=bool(raw.get("continues_to_next", False)),
        table_role=str(raw["table_role"]) if (btype == "table_row" and raw.get("table_role")) else None,
        cells=cells,
        provenance_provider=provider,
        provenance_model=model,
        provenance_harness=harness,
        source_page_sha256=source_page_sha256,
    )
    return blk, eff_folio


_SUP_SPLIT_RE = re.compile(r"(<sup>.*?</sup>)", re.DOTALL | re.IGNORECASE)


def _symbol_callout_regex(sym: str) -> re.Pattern[str]:
    esc = re.escape(sym)
    tail = r"(?=$|[\s.,;:!?)”\"’\]—–\-<|])"
    if sym in {"*", "**"}:
        return re.compile(rf"(?<=[\w.,;:!?)”\"’\]>])([ \t]?)(?<!\*)({esc})(?!\*)(?![ \t]*\*){tail}")
    if sym in {"§", "¶"}:
        return re.compile(rf"(?<=[\w.,;:!?)”\"’\]>])([ \t]?)({esc})(?!\s*\d){tail}")
    return re.compile(rf"(?<=[\w.,;:!?)”\"’\]>])([ \t]?)({esc}){tail}")


def _wrap_one_bare_symbol(tagged: str, sym: str) -> tuple[str, bool]:
    if sym not in tagged:
        return tagged, False
    pat = _symbol_callout_regex(sym)
    parts = _SUP_SPLIT_RE.split(tagged)
    replaced = False
    out: list[str] = []
    for idx, chunk in enumerate(parts):
        if idx % 2 == 1 or replaced or sym not in chunk:
            out.append(chunk)
            continue
        new_chunk, count = pat.subn(
            lambda m: f"{m.group(1)}<sup>{m.group(2)}</sup>",
            chunk,
            count=1,
        )
        if count > 0:
            replaced = True
        out.append(new_chunk)
    return "".join(out), replaced


def _promote_bare_symbol_callouts(blocks: list[BlockIR]) -> list[BlockIR]:
    """Promote attested page-local bare footnote symbols (`Chiapé*`) into canonical `<sup>*</sup>` spans."""
    attested: list[str] = []
    existing_sups: set[str] = set()
    for blk in blocks:
        if blk.type == "footnote" and not blk.continues_from_prev:
            for lbl in split_note_label_markers(blk.note_label):
                if lbl in _TYPO_FN_SYMBOLS and lbl not in attested:
                    attested.append(lbl)
        elif blk.type not in FURNITURE_BLOCK_TYPES and blk.type != "footnote":
            existing_sups.update(extract_inline_markers(blk.tagged_text))

    needed = [sym for sym in attested if sym not in existing_sups]
    if not needed:
        return blocks

    updated = list(blocks)
    for sym in needed:
        for i, blk in enumerate(updated):
            if blk.type in FURNITURE_BLOCK_TYPES or blk.type in {"footnote", "ornamental_break"}:
                continue
            new_tagged, hit = _wrap_one_bare_symbol(blk.tagged_text, sym)
            if hit:
                ct, cp, csp = parse_tagged_text(new_tagged)
                updated[i] = blk.model_copy(update={"tagged_text": ct, "plain_text": cp, "spans": csp})
                break
    return updated


def normalize_wire_to_page_ir(
    wire_data: dict[str, Any],
    *,
    page_number: int,
    source_page_sha256: str = "",
    provider: str = "",
    model: str = "",
    harness: str = "sdk",
    rescued: bool = False,
    cost_usd: float = 0.0,
    extra_warnings: list[str] | None = None,
) -> PageIR:
    """Convert raw `PageExtractWire` dict into a normalized `PageIR` with typed `BlockIR` records."""
    cleaned_wire, had_ctrl = strip_control_chars(wire_data)
    raw_folio = cleaned_wire.get("printed_folio")
    printed_folio = normalize_diplomatic_text(str(raw_folio)).strip() if raw_folio is not None else None
    if not printed_folio or is_binder_signature(printed_folio, page_number):
        printed_folio = None

    built_blocks: list[BlockIR] = []
    for idx, raw in enumerate(cleaned_wire.get("blocks") or []):
        if not isinstance(raw, dict):
            continue
        blk, printed_folio = _build_single_block_ir(
            raw,
            idx=idx,
            page_number=page_number,
            printed_folio=printed_folio,
            provider=provider,
            model=model,
            harness=harness,
            source_page_sha256=source_page_sha256,
        )
        built_blocks.append(blk)

    if printed_folio is not None:
        built_blocks = [
            b.model_copy(update={"printed_folio": printed_folio}) if b.printed_folio != printed_folio else b
            for b in built_blocks
        ]

    warnings = list(cleaned_wire.get("warning_codes") or [])
    if had_ctrl and CONTROL_CHARS_WARNING not in warnings:
        warnings.append(CONTROL_CHARS_WARNING)
    for w in extra_warnings or []:
        if w not in warnings:
            warnings.append(w)

    ordered_blocks = _promote_bare_symbol_callouts(sort_two_column_blocks(built_blocks))
    return PageIR(
        page_number=page_number,
        printed_folio=printed_folio,
        page_orientation=str(cleaned_wire.get("page_orientation") or "upright"),
        warning_codes=warnings,
        failed_closed=False,
        failure_reason=None,
        source_page_sha256=source_page_sha256,
        blocks=ordered_blocks,
        provider=provider,
        model=model,
        harness=harness,
        rescued=rescued,
        cost_usd=round(float(cost_usd), 8),
    )


def solve_monotonic_folios(pages: list[PageIR]) -> list[PageIR]:
    """Interpolate missing Arabic folios on unnumbered chapter/part openings while preserving printed Roman/Arabic folios."""
    arabic_obs: dict[int, int] = {}
    for page in pages:
        if page.failed_closed or not page.printed_folio:
            continue
        clean = re.sub(r"^(?:p\.?|page)\s*", "", page.printed_folio.strip(), flags=re.IGNORECASE).strip(" []().—–-")
        if clean.isdigit():
            arabic_obs[page.page_number] = int(clean)

    if len(arabic_obs) < 3:
        return pages

    offsets = Counter(pnum - fval for pnum, fval in arabic_obs.items())
    dominant_offset, dom_count = offsets.most_common(1)[0]
    if dom_count < 2:
        return pages
    first_arabic = min(arabic_obs)

    updated_pages: list[PageIR] = []
    for page in pages:
        if page.failed_closed or page.printed_folio or page.page_number < first_arabic:
            updated_pages.append(page)
            continue
        inferred = page.page_number - dominant_offset
        if inferred < 1:
            updated_pages.append(page)
            continue
        folio_str = str(inferred)
        upd_blocks = [b.model_copy(update={"printed_folio": folio_str}) for b in page.blocks]
        updated_pages.append(page.model_copy(update={"printed_folio": folio_str, "blocks": upd_blocks}))
    return updated_pages
