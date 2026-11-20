"""Safe XHTML/HTML5, Markdown, and TEI inline & block rendering primitives for Paper Lanterns v2.

ROLE: Pure structural projection of CollectionAST blocks, constrained inline tags, folio turns, and pre-linked footnotes.
INPUTS: BlockIR, FootnoteItem, and CollectionAST data structures from lanterns.core.models.
OUTPUTS: Strictly escaped, stack-balanced HTML5/XHTML/TEI/Markdown fragments and shared editorial CSS.
INVARIANT: Never inspects prose content words, never branches on book_id, never re-implements segmentation or footnote matching.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from lanterns.core.atomic import atomic_write_json, atomic_write_text
from lanterns.core.models import BlockIR, FootnoteItem, LetterUnit, StructuralUnit, TableCell
from lanterns.core.tags import (
    alloc_fnref_id,
    fn_anchor_id,
    normalize_note_label,
    sanitize_xml_id,
    split_tagged_lines_balanced,
    split_tagged_paragraphs_balanced,
    strip_constrained_tags,
    tokenize_tagged_text,
)
from lanterns.core.unicode_norm import fold_diacritics_for_search

ALLOWED_INLINE_TAGS: frozenset[str] = frozenset({"i", "b", "sc", "fnref", "sup", "sub", "unclear"})

READER_STRICT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://iiif.archive.org https://archive.org; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'none'"
)

_PLACEHOLDER_NOTE_RE = re.compile(r"\[\[NOTE:([^:\]]+):(?:(\d+):)?([^\]]+)\]\]")
_PLACEHOLDER_FOLIO_RE = re.compile(r"\[\[(?:FN)?FOLIO:(\d+):([^\]]*)\]\]")
_BRACKET_FOLIO_RE = re.compile(r"\[p\.\s*([^\]]+?)\]")

HTML_TAG_OPEN: dict[str, str] = {
    "i": "<i>",
    "b": "<b>",
    "sc": '<span class="small-caps">',
    "fnref": '<sup class="fn-ref">',
    "sup": "<sup>",
    "sub": "<sub>",
    "unclear": '<span class="unclear" title="Uncertain reading in source scan">',
}

HTML_TAG_CLOSE: dict[str, str] = {
    "i": "</i>",
    "b": "</b>",
    "sc": "</span>",
    "fnref": "</sup>",
    "sup": "</sup>",
    "sub": "</sub>",
    "unclear": "</span>",
}

TEI_TAG_OPEN: dict[str, str] = {
    "i": '<hi rend="italic">',
    "b": '<hi rend="bold">',
    "sc": '<hi rend="smallcaps">',
    "fnref": '<hi rend="sup">',
    "sup": '<hi rend="sup">',
    "sub": '<hi rend="sub">',
    "unclear": "<unclear>",
}

TEI_TAG_CLOSE: dict[str, str] = {
    "i": "</hi>",
    "b": "</hi>",
    "sc": "</hi>",
    "fnref": "</hi>",
    "sup": "</hi>",
    "sub": "</hi>",
    "unclear": "</unclear>",
}

MD_TAG_OPEN: dict[str, str] = {
    "i": "*",
    "b": "**",
    "sc": '<span class="small-caps">',
    "fnref": "<sup>",
    "sup": "<sup>",
    "sub": "<sub>",
    "unclear": "<mark>",
}

MD_TAG_CLOSE: dict[str, str] = {
    "i": "*",
    "b": "**",
    "sc": "</span>",
    "fnref": "</sup>",
    "sup": "</sup>",
    "sub": "</sub>",
    "unclear": "</mark>",
}

BLOCK_TYPE_CLASS: dict[str, str] = {
    "heading": "block-heading",
    "letter_header": "block-letter-header",
    "dateline": "block-dateline",
    "salutation": "block-salutation",
    "body": "block-body",
    "blockquote": "block-blockquote",
    "verse": "block-verse",
    "valediction": "block-valediction",
    "signature": "block-signature",
    "postscript": "block-postscript",
    "address_block": "block-address",
    "editorial": "block-editorial",
    "ornamental_break": "block-ornamental-break",
    "list_item": "block-list-item",
    "toc_entry": "block-toc-entry",
    "index_entry": "block-index-entry",
    "table_caption": "block-table-caption",
    "table_row": "block-table-row",
    "footnote": "block-footnote",
    "margin_note": "block-margin-note",
    "illustration": "block-illustration",
    "facsimile_plate": "block-facsimile-plate",
    "caption": "block-caption",
    "epigraph": "block-epigraph",
    "imprint": "block-imprint",
}

ALIGNMENT_CLASS: dict[str, str] = {
    "left": "align-left",
    "center": "align-center",
    "right": "align-right",
    "indented": "align-indented",
    "cascading": "align-cascading",
    "inline": "align-inline",
    "two_column_left": "two-col-left",
    "two_column_right": "two-col-right",
}

_MD_STRIP_FOLIO_RE = re.compile(r"\[\[(?:FN)?FOLIO:\d+:[^\]]*\]\]\s*")
_MD_STRIP_NOTE_RE = re.compile(r"\[\[NOTE:([^:\]]+):(?:\d+:)?[^\]]+\]\]")
_HTML_INLINE_TAG_MAP = {"blockquote": "blockquote", "epigraph": "blockquote"}
__all___atomic_keep = (
    alloc_fnref_id,
    atomic_write_json,
    atomic_write_text,
    fn_anchor_id,
    fold_diacritics_for_search,
    sanitize_xml_id,
    split_tagged_paragraphs_balanced,
    strip_constrained_tags,
    tokenize_tagged_text,
)
_normalize_note_label = normalize_note_label


def _build_callout_queue(block_footnotes: list[FootnoteItem] | None) -> dict[str, list[FootnoteItem]]:
    queue: dict[str, list[FootnoteItem]] = {}
    for fn in block_footnotes or []:
        key = _normalize_note_label(fn.label)
        queue.setdefault(key, []).append(fn)
    return queue


def html_lang_dir_attrs(ast: Any, *, xhtml: bool = False) -> str:
    """Build `lang`/`xml:lang` and optional `dir="rtl"` root `<html>` attributes from `CollectionAST`."""
    lang_esc = html.escape((getattr(ast, "language", None) or "en").strip() or "en", quote=True)
    dir_attr = (getattr(ast, "text_direction", "ltr") == "rtl" and ' dir="rtl"') or ""
    xml_attr = (xhtml and f'xml:lang="{lang_esc}" ') or ""
    return f'{xml_attr}lang="{lang_esc}"{dir_attr}'


def _format_fnref_html(fn_id: str, label_text: str, ref_registry: dict[str, list[str]], *, epub: bool) -> str:
    target_id = html.escape(fn_anchor_id(fn_id), quote=True)
    ref_id = html.escape(alloc_fnref_id(fn_id, ref_registry), quote=True)
    lbl_esc = html.escape(label_text, quote=True)
    epub_attr = (epub and ' epub:type="noteref"') or ""
    return f'<sup class="fn-ref"><a id="{ref_id}"{epub_attr} role="doc-noteref" href="#{target_id}">{lbl_esc}</a></sup>'


def _format_folio_html(
    page_number: int | None,
    folio_label: str,
    *,
    seen_page_ids: set[int] | None,
    proof_href_prefix: str | None,
    epub: bool,
) -> str:
    fol_esc = html.escape(folio_label.strip() or str(page_number or ""), quote=True)
    is_new_id = page_number is not None and seen_page_ids is not None and page_number not in seen_page_ids
    if is_new_id and seen_page_ids is not None and page_number is not None:
        seen_page_ids.add(page_number)
    id_attr = f' id="page-{page_number}"' if is_new_id else ""
    if epub:
        pb_attrs = f' epub:type="pagebreak" role="doc-pagebreak" title="{fol_esc}"' if id_attr else ""
        return f'<span{id_attr} class="folio-marker"{pb_attrs}>[p. {fol_esc}]</span>'
    if proof_href_prefix and page_number is not None:
        href = html.escape(f"{proof_href_prefix}#page-{page_number}", quote=True)
        return (
            f'<a{id_attr} class="folio-marker" href="{href}" '
            f'data-page="{page_number}" data-folio="{fol_esc}">[p. {fol_esc}]</a>'
        )
    return f'<span{id_attr} class="folio-marker">[p. {fol_esc}]</span>'


def _render_text_chunk_html(
    raw_chunk: str,
    *,
    available_footnotes: dict[str, FootnoteItem] | None,
    ref_registry: dict[str, list[str]],
    seen_page_ids: set[int] | None,
    folio_to_page: dict[str, int] | None,
    default_page: int | None,
    proof_href_prefix: str | None,
    epub: bool,
) -> str:
    """Escape plain text while turning explicit `[[NOTE:...]]`, `[[FOLIO:...]]`, and `[p. N]` markers into HTML."""
    pattern = re.compile(
        r"\[\[NOTE:([^:\]]+):(?:(\d+):)?([^\]]+)\]\]"
        r"|\[\[(?:FN)?FOLIO:(\d+):([^\]]*)\]\]"
        r"|\[p\.\s*([^\]]+?)\]"
    )
    out: list[str] = []
    last = 0
    for match in pattern.finditer(raw_chunk):
        if match.start() > last:
            out.append(html.escape(raw_chunk[last : match.start()], quote=False))
        last = match.end()
        if match.group(1) is not None:
            fn_id, label = match.group(1), match.group(3)
            if available_footnotes is None or fn_id in available_footnotes:
                out.append(_format_fnref_html(fn_id, label, ref_registry, epub=epub))
            else:
                out.append(f"<sup>{html.escape(label, quote=False)}</sup>")
        elif match.group(4) is not None:
            pnum = int(match.group(4))
            out.append(
                _format_folio_html(
                    pnum,
                    match.group(5),
                    seen_page_ids=seen_page_ids,
                    proof_href_prefix=proof_href_prefix,
                    epub=epub,
                )
            )
        else:
            fol_str = match.group(6).strip()
            pnum = (folio_to_page or {}).get(fol_str, default_page)
            out.append(
                _format_folio_html(
                    pnum,
                    fol_str,
                    seen_page_ids=seen_page_ids,
                    proof_href_prefix=proof_href_prefix,
                    epub=epub,
                )
            )
    if last < len(raw_chunk):
        out.append(html.escape(raw_chunk[last:], quote=False))
    return "".join(out)


def _render_single_line_html(
    line_tagged: str,
    *,
    callout_queue: dict[str, list[FootnoteItem]],
    available_footnotes: dict[str, FootnoteItem] | None,
    ref_registry: dict[str, list[str]],
    seen_page_ids: set[int] | None,
    folio_to_page: dict[str, int] | None,
    default_page: int | None,
    proof_href_prefix: str | None,
    epub: bool,
) -> str:
    tokens = tokenize_tagged_text(line_tagged)
    out: list[str] = []
    idx = 0
    while idx < len(tokens):
        kind, val = tokens[idx]
        if (
            kind == "open"
            and val in {"fnref", "sup"}
            and idx + 2 < len(tokens)
            and tokens[idx + 1][0] == "text"
            and tokens[idx + 2] == ("close", val)
        ):
            sup_txt = tokens[idx + 1][1]
            note_match = _PLACEHOLDER_NOTE_RE.fullmatch(sup_txt.strip())
            if note_match and (available_footnotes is None or note_match.group(1) in available_footnotes):
                out.append(_format_fnref_html(note_match.group(1), note_match.group(3), ref_registry, epub=epub))
                idx += 3
                continue
            norm_lbl = _normalize_note_label(sup_txt)
            bucket = callout_queue.get(norm_lbl)
            if bucket:
                fn = bucket.pop(0)
                out.append(_format_fnref_html(fn.footnote_id, sup_txt, ref_registry, epub=epub))
                idx += 3
                continue
        if kind == "open":
            out.append(HTML_TAG_OPEN[val])
        elif kind == "close":
            out.append(HTML_TAG_CLOSE[val])
        else:
            out.append(
                _render_text_chunk_html(
                    val,
                    available_footnotes=available_footnotes,
                    ref_registry=ref_registry,
                    seen_page_ids=seen_page_ids,
                    folio_to_page=folio_to_page,
                    default_page=default_page,
                    proof_href_prefix=proof_href_prefix,
                    epub=epub,
                )
            )
        idx += 1
    return "".join(out)


def render_inline_html(
    tagged_text: str,
    *,
    block_footnotes: list[FootnoteItem] | None = None,
    available_footnotes: dict[str, FootnoteItem] | None = None,
    ref_registry: dict[str, list[str]] | None = None,
    seen_page_ids: set[int] | None = None,
    folio_to_page: dict[str, int] | None = None,
    default_page: int | None = None,
    proof_href_prefix: str | None = None,
    preserve_line_breaks: bool = False,
    cascading: bool = False,
    epub: bool = False,
) -> str:
    """Convert constrained `tagged_text` into valid XHTML/HTML5 with bidirectional `#fnref` and folio links."""
    registry = ref_registry if ref_registry is not None else {}
    queue = _build_callout_queue(block_footnotes)
    is_multi = (cascading or preserve_line_breaks) and "\n" in tagged_text
    lines = split_tagged_lines_balanced(tagged_text) if is_multi else [tagged_text]
    if cascading and is_multi:
        lines = [ln for ln in lines if ln.strip()]
    rendered = [
        _render_single_line_html(
            ln,
            callout_queue=queue,
            available_footnotes=available_footnotes,
            ref_registry=registry,
            seen_page_ids=seen_page_ids,
            folio_to_page=folio_to_page,
            default_page=default_page,
            proof_href_prefix=proof_href_prefix,
            epub=epub,
        )
        for ln in lines
    ]
    if cascading and is_multi:
        rendered = [f'<span class="cascade-line" style="--cascade-step: {i};">{s}</span>' for i, s in enumerate(rendered)]
    return "<br />\n".join(rendered)


def block_css_classes(block: BlockIR) -> str:
    """Return semantic `.block-*` and spatial `.align-*` / `.two-col-*` CSS class string."""
    classes = [
        "archival-block",
        BLOCK_TYPE_CLASS.get(block.type, "block-body"),
        ALIGNMENT_CLASS.get(block.alignment, "align-left"),
    ]
    if block.line_breaks_preserved:
        classes.append("lines-preserved")
    if block.epistolary_transition == "starts_enclosure":
        classes.append("enclosure-start")
    return " ".join(classes)


def index_ast_blocks(ast: Any) -> dict[str, BlockIR]:
    """Build a complete `block_id -> BlockIR` lookup preferring post-join blocks in `sections`/`letters`."""
    by_id: dict[str, BlockIR] = {}
    for page in ast.pages:
        for blk in page.blocks:
            by_id[blk.block_id] = blk
    for sec in ast.sections:
        for item in sec.items:
            for blk in item.blocks:
                by_id[blk.block_id] = blk
    for ltr in ast.letters:
        for blk in ltr.blocks:
            by_id[blk.block_id] = blk
    return by_id


def iter_section_render_units(
    section: Any,
    ast: Any,
    block_lookup: dict[str, BlockIR],
) -> list[tuple[str, list[BlockIR], LetterUnit | None, StructuralUnit | None]]:
    """Return source-ordered `(unit_kind, blocks, letter_or_none, structural_unit_or_none)` tuples."""
    letters_by_id: dict[str, LetterUnit] = {ltr.letter_id: ltr for ltr in ast.letters}
    by_start: dict[str, LetterUnit] = {ltr.blocks[0].block_id: ltr for ltr in ast.letters if ltr.blocks}
    if section.units:
        out: list[tuple[str, list[BlockIR], LetterUnit | None, StructuralUnit | None]] = []
        for unit in section.units:
            blks = [block_lookup[bid] for bid in unit.block_ids if bid in block_lookup]
            lu = (blks and by_start.get(blks[0].block_id)) or None
            out.append((unit.kind, blks, lu, unit))
        return out
    fallback: list[tuple[str, list[BlockIR], LetterUnit | None, StructuralUnit | None]] = []
    for item in section.items:
        lu = letters_by_id.get(str(item.letter_id or "")) if item.kind == "letter" else None
        blks = lu.blocks if lu is not None else list(item.blocks)
        fallback.append(("letter" if lu is not None else "narrative", blks, lu, None))
    return fallback


def collect_section_footnotes(section: Any, ast: Any) -> tuple[list[str], dict[str, FootnoteItem]]:
    """Collect ordered footnote IDs and subset map for a `BookSection` across its units and letters."""
    ordered_ids: list[str] = list(section.footnote_ids)
    for unit in section.units:
        ordered_ids = list(dict.fromkeys([*ordered_ids, *unit.footnote_ids]))
    letters_by_id = {ltr.letter_id: ltr for ltr in ast.letters}
    for item in section.items:
        if item.kind == "letter" and item.letter_id in letters_by_id:
            ordered_ids = list(dict.fromkeys([*ordered_ids, *letters_by_id[item.letter_id].footnote_ids]))
    return ordered_ids, {fid: ast.footnotes[fid] for fid in ordered_ids if fid in ast.footnotes}


def format_markdown_footnote(fn: FootnoteItem, *, include_orphan_prefix: bool = True) -> str:
    """Format a single or multi-paragraph `FootnoteItem` as valid CommonMark `[^{id}]:` with 4-space continuation indent."""
    orphan = f"[Unlinked note, p. {fn.origin_folio or fn.origin_page}] " if (include_orphan_prefix and fn.is_orphan) else ""
    paras = [render_inline_markdown(p) for p in split_tagged_paragraphs_balanced(fn.tagged_text)]
    indented_paras = ["\n    ".join(p.splitlines()) for p in paras if p.strip()] or [render_inline_markdown(fn.tagged_text)]
    first = f"[^{fn.footnote_id}]: ({fn.label}) {orphan}{indented_paras[0]}"
    return "\n\n    ".join([first, *indented_paras[1:]])


def footnotes_by_block_map(
    footnotes: dict[str, FootnoteItem],
    allowed_ids: set[str] | None = None,
) -> dict[str, list[FootnoteItem]]:
    """Index pre-linked `FootnoteItem` objects by `referenced_in_block_ids`."""
    mapping: dict[str, list[FootnoteItem]] = {}
    for fn_id, fn in footnotes.items():
        if allowed_ids is not None and fn_id not in allowed_ids:
            continue
        for bid in fn.referenced_in_block_ids:
            mapping.setdefault(bid, []).append(fn)
    return mapping


def render_unavailable_page_banner(page_number: int, *, proof_href_prefix: str | None = None, epub: bool = False) -> str:
    """Render an explicit fail-closed banner (`[Page N unavailable — inspect source scan]`)."""
    msg = f"[Page {page_number} unavailable — inspect source scan]"
    if not epub and proof_href_prefix:
        href = html.escape(f"{proof_href_prefix}#page-{page_number}", quote=True)
        return (
            f'<div id="page-{page_number}" class="page-unavailable" role="alert">'
            f'<a href="{href}">{html.escape(msg)}</a></div>'
        )
    return f'<div id="page-{page_number}" class="page-unavailable" role="alert">{html.escape(msg)}</div>'


def render_inline_markdown(tagged_text: str, block_footnotes: list[FootnoteItem] | None = None) -> str:
    """Convert constrained inline tags + footnotes into Scholarly Markdown text."""
    fn_queue: dict[str, list[str]] = {}
    for fn in block_footnotes or []:
        fn_queue.setdefault(fn.label.strip().strip("[]()"), []).append(fn.footnote_id)
    tokens = tokenize_tagged_text(tagged_text)
    out: list[str] = []
    idx = 0
    while idx < len(tokens):
        kind, val = tokens[idx]
        is_fn = kind == "open" and val in {"fnref", "sup"} and idx + 2 < len(tokens) and tokens[idx + 1][0] == "text" and tokens[idx + 2] == ("close", val)
        bucket = fn_queue.get(tokens[idx + 1][1].strip().strip("[]()")) if is_fn else None
        if bucket:
            out.append(f"[^{bucket.pop(0)}]")
            idx += 3
            continue
        if kind == "text":
            cleaned = _MD_STRIP_FOLIO_RE.sub("", val)
            out.append(_MD_STRIP_NOTE_RE.sub(r"[^\1]", cleaned))
        else:
            out.append((MD_TAG_OPEN if kind == "open" else MD_TAG_CLOSE)[val])
        idx += 1
    return "".join(out)


def _count_leading_table_headers(row_blocks: list[BlockIR]) -> int:
    count = 0
    for r_blk in row_blocks:
        if r_blk.table_role != "header":
            return count
        count += 1
    return 0


def _render_table_group_html(
    caption_block: BlockIR | None,
    row_blocks: list[BlockIR],
    *,
    fn_by_block: dict[str, list[FootnoteItem]],
    available_footnotes: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    seen_page_ids: set[int],
    folio_to_page: dict[str, int],
    proof_href_prefix: str | None,
    epub: bool,
) -> str:
    cap_html = ""
    if caption_block is not None:
        inner = render_inline_html(
            caption_block.tagged_text,
            block_footnotes=fn_by_block.get(caption_block.block_id),
            available_footnotes=available_footnotes,
            ref_registry=ref_registry,
            seen_page_ids=seen_page_ids,
            folio_to_page=folio_to_page,
            default_page=caption_block.page_number,
            proof_href_prefix=proof_href_prefix,
            epub=epub,
        )
        cap_html = f"<caption>{inner}</caption>"

    lead_hdr_count = _count_leading_table_headers(row_blocks)
    head_rows: list[str] = []
    body_rows: list[str] = []
    for row_idx, r_blk in enumerate(row_blocks):
        is_hdr = r_blk.table_role == "header"
        cell_tag = (is_hdr and "th") or "td"
        row_fol = ""
        if r_blk.page_number not in seen_page_ids:
            row_fol = (
                _format_folio_html(
                    r_blk.page_number,
                    r_blk.printed_folio or str(r_blk.page_number),
                    seen_page_ids=seen_page_ids,
                    proof_href_prefix=proof_href_prefix,
                    epub=epub,
                )
                + " "
            )
        cells = r_blk.cells or [TableCell(text=p.strip()) for p in r_blk.tagged_text.split("|")]
        rendered_cells: list[str] = []
        for cell_idx, cell in enumerate(cells):
            cs = (cell.colspan > 1 and f' colspan="{cell.colspan}"') or ""
            rs = (cell.rowspan > 1 and f' rowspan="{cell.rowspan}"') or ""
            c_inner = render_inline_html(
                cell.text,
                block_footnotes=fn_by_block.get(r_blk.block_id),
                available_footnotes=available_footnotes,
                ref_registry=ref_registry,
                seen_page_ids=seen_page_ids,
                folio_to_page=folio_to_page,
                default_page=r_blk.page_number,
                proof_href_prefix=proof_href_prefix,
                epub=epub,
            )
            prefix = (cell_idx == 0 and row_fol) or ""
            rendered_cells.append(f"<{cell_tag}{cs}{rs}>{prefix}{c_inner}</{cell_tag}>")
        sub_cls = (is_hdr and row_idx >= lead_hdr_count and ' class="table-subheader"') or ""
        row_html = f"<tr{sub_cls}>{''.join(rendered_cells)}</tr>"
        (head_rows if row_idx < lead_hdr_count else body_rows).append(row_html)

    thead = (head_rows and f"<thead>{''.join(head_rows)}</thead>") or ""
    tbody = f"<tbody>{''.join(body_rows)}</tbody>"
    return f'<div class="archival-table-wrap"><table class="archival-table">{cap_html}{thead}{tbody}</table></div>'


def _compute_heading_level(block: BlockIR, heading_base: int, max_heading_level: int) -> int:
    raw = heading_base + (0 if block.type == "letter_header" else max(0, (block.heading_level or 1) - 1))
    return min(6, max(2, min(raw, max_heading_level)))


def render_single_block_html(
    block: BlockIR,
    *,
    fn_by_block: dict[str, list[FootnoteItem]],
    available_footnotes: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    seen_page_ids: set[int],
    folio_to_page: dict[str, int],
    proof_href_prefix: str | None = None,
    epub: bool = False,
    heading_base: int = 2,
    max_heading_level: int = 6,
) -> str:
    """Render one non-table, non-list `BlockIR` to semantic HTML5/XHTML."""
    cls = block_css_classes(block)
    bid = html.escape(sanitize_xml_id(block.block_id, "blk"), quote=True)
    preserve_lb = block.line_breaks_preserved or block.type in {
        "verse",
        "dateline",
        "salutation",
        "valediction",
        "signature",
        "address_block",
    }
    inner = render_inline_html(
        block.tagged_text,
        block_footnotes=fn_by_block.get(block.block_id),
        available_footnotes=available_footnotes,
        ref_registry=ref_registry,
        seen_page_ids=seen_page_ids,
        folio_to_page=folio_to_page,
        default_page=block.page_number,
        proof_href_prefix=proof_href_prefix,
        preserve_line_breaks=preserve_lb,
        cascading=(block.alignment == "cascading"),
        epub=epub,
    )
    if block.type == "ornamental_break":
        return f'<div id="{bid}" class="{cls}" role="separator">{inner.strip() or "* * *"}</div>'
    if block.type in {"heading", "letter_header"}:
        lvl = _compute_heading_level(block, heading_base, max_heading_level)
        return f'<h{lvl} id="{bid}" class="{cls}">{inner}</h{lvl}>'
    if block.type == "margin_note":
        side_cls = (block.bbox and block.bbox[0] >= 500 and "margin-right") or "margin-left"
        return f'<aside id="{bid}" class="{cls} {side_cls}" role="note">{inner}</aside>'
    if block.type in {"illustration", "facsimile_plate"}:
        label = (block.type == "facsimile_plate" and "Facsimile plate") or "Illustration"
        desc = inner or html.escape(f"[{label} on p. {block.printed_folio or block.page_number}]")
        return f'<figure id="{bid}" class="{cls}"><figcaption>{desc}</figcaption></figure>'
    tag = _HTML_INLINE_TAG_MAP.get(block.type, "p")
    return f'<{tag} id="{bid}" class="{cls}">{inner}</{tag}>'


_LEADING_BRACKET_FOLIO_RE = re.compile(r"^\s*\[p\.\s*([^\]]+?)\]\s*")
_COALESCE_TYPES: frozenset[str] = frozenset({"body", "blockquote", "editorial"})
_COMPAT_PROSE_TYPES: frozenset[str] = frozenset({"body", "editorial"})
_FLOAT_TYPES: frozenset[str] = frozenset({"illustration", "facsimile_plate", "caption", "margin_note"})
_SKIP_STREAM_TYPES: frozenset[str] = frozenset({"running_header", "folio", "scanner_residue", "noise", "footnote"})


def _upgrade_leading_folio_token(nxt: BlockIR) -> str:
    raw = nxt.tagged_text.lstrip()
    m = _LEADING_BRACKET_FOLIO_RE.match(raw)
    if m:
        rest = raw[m.end() :]
        tok = f"[[FOLIO:{nxt.page_number}:{m.group(1).strip()}]]"
        return f"{tok} {rest}".strip() if rest else tok
    if raw.startswith("[[FOLIO:"):
        return raw
    fol = nxt.printed_folio or str(nxt.page_number)
    return f"[[FOLIO:{nxt.page_number}:{fol}]] {raw}".strip()


def _find_continuation_index(
    visible: list[BlockIR],
    start_idx: int,
    acc_type: str,
) -> int | None:
    k = start_idx
    while k < len(visible) and visible[k].type in _FLOAT_TYPES:
        k += 1
    if k >= len(visible):
        return None
    cand = visible[k]
    if not cand.continues_from_prev or cand.epistolary_transition == "starts_new_letter":
        return None
    if cand.type == acc_type or {acc_type, cand.type} <= _COMPAT_PROSE_TYPES:
        return k
    return None


def _intermediate_float_folio_tokens(
    span_floats: list[BlockIR],
    cur_max_page: int,
    nxt_page: int,
) -> str:
    seen: set[int] = set()
    toks: list[str] = []
    for flt in span_floats:
        pnum = flt.page_number
        if cur_max_page < pnum < nxt_page and pnum not in seen:
            seen.add(pnum)
            toks.append(f"[[FOLIO:{pnum}:{flt.printed_folio or pnum}]]")
    return " ".join(toks)


def coalesce_continued_blocks(
    blocks: list[BlockIR],
    fn_by_block: dict[str, list[FootnoteItem]],
    *,
    include_folio_tokens: bool = True,
) -> tuple[list[BlockIR], dict[str, list[FootnoteItem]]]:
    """Merge cross-page continued `body`/`blockquote`/`editorial` blocks into single paragraphs for rendering."""
    visible = [b for b in blocks if b.type not in _SKIP_STREAM_TYPES]
    merged_blocks: list[BlockIR] = []
    merged_fns: dict[str, list[FootnoteItem]] = dict(fn_by_block)
    idx = 0
    while idx < len(visible):
        acc = visible[idx]
        cur_max_page = acc.page_number
        acc_fns = list(fn_by_block.get(acc.block_id, []))
        seen_fids = {fn.footnote_id for fn in acc_fns}
        deferred_floats: list[BlockIR] = []
        idx += 1
        while acc.type in _COALESCE_TYPES and acc.continues_to_next:
            k = _find_continuation_index(visible, idx, acc.type)
            if k is None:
                break
            span_floats = visible[idx:k]
            deferred_floats.extend(span_floats)
            nxt = visible[k]
            rhs = nxt.tagged_text.lstrip()
            if include_folio_tokens and nxt.page_number > cur_max_page:
                mid_toks = _intermediate_float_folio_tokens(span_floats, cur_max_page, nxt.page_number)
                rhs = f"{mid_toks} {_upgrade_leading_folio_token(nxt)}".strip()
            cur_max_page = max(cur_max_page, nxt.page_number, *(f.page_number for f in span_floats))
            joined_tag = f"{acc.tagged_text.rstrip()} {rhs}".strip()
            joined_pln = f"{acc.plain_text.rstrip()} {nxt.plain_text.lstrip()}".strip()
            for fn in fn_by_block.get(nxt.block_id, []):
                if fn.footnote_id not in seen_fids:
                    seen_fids.add(fn.footnote_id)
                    acc_fns.append(fn)
            acc = acc.model_copy(
                update={
                    "tagged_text": joined_tag,
                    "plain_text": joined_pln,
                    "continues_to_next": nxt.continues_to_next,
                }
            )
            idx = k + 1
        merged_fns[acc.block_id] = acc_fns
        merged_blocks.append(acc)
        merged_blocks.extend(deferred_floats)
    return merged_blocks, merged_fns


def count_redundant_leading_headings(blocks: list[BlockIR], section_title_plain: str) -> int:
    """Return count of leading `heading` blocks whose combined plain text matches `section_title_plain`."""
    norm_sec = " ".join(section_title_plain.split())
    if not norm_sec or not blocks or blocks[0].type != "heading":
        return 0
    first_page = blocks[0].page_number
    acc_parts: list[str] = []
    for count, blk in enumerate(blocks, start=1):
        if blk.type != "heading" or blk.page_number != first_page:
            break
        acc_parts.append(" ".join(blk.plain_text.split()))
        if " ".join(acc_parts) == norm_sec:
            return count
    return 0


def _resolve_child_spans_html(
    blocks: list[BlockIR],
    children: list[Any] | None,
) -> dict[int, int]:
    """Return non-overlapping `start_idx -> end_idx_exclusive` over `blocks` for `ChildSpan` enclosures."""
    if not children:
        return {}
    pos = {b.block_id: i for i, b in enumerate(blocks)}
    spans: dict[int, int] = {}
    last_end = 0
    for ch in children:
        s_i = pos.get(ch.start_anchor.block_id)
        e_i = pos.get(ch.end_anchor.block_id)
        if s_i is not None and e_i is not None and last_end <= s_i <= e_i:
            spans[s_i] = e_i + 1
            last_end = e_i + 1
    return spans


def _render_list_group_html(
    list_blocks: list[BlockIR],
    *,
    fn_by_block: dict[str, list[FootnoteItem]],
    available_footnotes: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    seen_page_ids: set[int],
    folio_to_page: dict[str, int],
    proof_href_prefix: str | None,
    epub: bool,
) -> str:
    items_html: list[str] = []
    for cur in list_blocks:
        fol_prefix = ""
        if cur.page_number not in seen_page_ids:
            fol_prefix = (
                _format_folio_html(
                    cur.page_number,
                    cur.printed_folio or str(cur.page_number),
                    seen_page_ids=seen_page_ids,
                    proof_href_prefix=proof_href_prefix,
                    epub=epub,
                )
                + " "
            )
        li_inner = render_inline_html(
            cur.tagged_text,
            block_footnotes=fn_by_block.get(cur.block_id),
            available_footnotes=available_footnotes,
            ref_registry=ref_registry,
            seen_page_ids=seen_page_ids,
            folio_to_page=folio_to_page,
            default_page=cur.page_number,
            proof_href_prefix=proof_href_prefix,
            preserve_line_breaks=cur.line_breaks_preserved,
            epub=epub,
        )
        items_html.append(f'<li id="{html.escape(sanitize_xml_id(cur.block_id, "blk"))}">{fol_prefix}{li_inner}</li>')
    return f'<ul class="archival-list">{"".join(items_html)}</ul>'


def render_block_stream_html(
    blocks: list[BlockIR],
    *,
    fn_by_block: dict[str, list[FootnoteItem]],
    available_footnotes: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    seen_page_ids: set[int],
    folio_to_page: dict[str, int],
    proof_href_prefix: str | None = None,
    epub: bool = False,
    heading_base: int = 2,
    leading_section_title: str = "",
    children: list[Any] | None = None,
) -> str:
    """Render an ordered sequence of `BlockIR`s, preserving page anchors, tables, lists, enclosures, and heading hierarchy."""
    blocks, fn_by_block = coalesce_continued_blocks(blocks, fn_by_block)
    child_spans = _resolve_child_spans_html(blocks, children)
    skip_n = count_redundant_leading_headings(blocks, leading_section_title)
    out: list[str] = []
    if skip_n > 0 and blocks[0].page_number not in seen_page_ids:
        out.append(
            '<div class="page-locus">'
            + _format_folio_html(
                blocks[0].page_number,
                blocks[0].printed_folio or str(blocks[0].page_number),
                seen_page_ids=seen_page_ids,
                proof_href_prefix=proof_href_prefix,
                epub=epub,
            )
            + "</div>"
        )
    max_next_h = max(2, heading_base)
    idx = skip_n
    while idx < len(blocks):
        if idx in child_spans:
            end_c = child_spans[idx]
            enc_html = render_block_stream_html(
                blocks[idx:end_c],
                fn_by_block=fn_by_block,
                available_footnotes=available_footnotes,
                ref_registry=ref_registry,
                seen_page_ids=seen_page_ids,
                folio_to_page=folio_to_page,
                proof_href_prefix=proof_href_prefix,
                epub=epub,
                heading_base=max_next_h,
            )
            out.append(f'<div class="enclosure-span">{enc_html}</div>')
            idx = end_c
            continue
        blk = blocks[idx]
        if blk.type in _SKIP_STREAM_TYPES:
            idx += 1
            continue
        if blk.page_number not in seen_page_ids:
            out.append(
                '<div class="page-locus">'
                + _format_folio_html(
                    blk.page_number,
                    blk.printed_folio or str(blk.page_number),
                    seen_page_ids=seen_page_ids,
                    proof_href_prefix=proof_href_prefix,
                    epub=epub,
                )
                + "</div>"
            )
        if blk.type == "list_item":
            end_li = idx + 1
            while end_li < len(blocks) and blocks[end_li].type == "list_item":
                end_li += 1
            out.append(
                _render_list_group_html(
                    blocks[idx:end_li],
                    fn_by_block=fn_by_block,
                    available_footnotes=available_footnotes,
                    ref_registry=ref_registry,
                    seen_page_ids=seen_page_ids,
                    folio_to_page=folio_to_page,
                    proof_href_prefix=proof_href_prefix,
                    epub=epub,
                )
            )
            idx = end_li
            continue
        if blk.type in {"table_caption", "table_row"}:
            cap_blk = blk if blk.type == "table_caption" else None
            idx += int(cap_blk is not None)
            row_blks: list[BlockIR] = []
            while idx < len(blocks) and blocks[idx].type == "table_row":
                row_blks.append(blocks[idx])
                idx += 1
            if row_blks:
                out.append(
                    _render_table_group_html(
                        cap_blk,
                        row_blks,
                        fn_by_block=fn_by_block,
                        available_footnotes=available_footnotes,
                        ref_registry=ref_registry,
                        seen_page_ids=seen_page_ids,
                        folio_to_page=folio_to_page,
                        proof_href_prefix=proof_href_prefix,
                        epub=epub,
                    )
                )
            elif cap_blk is not None:
                out.append(
                    render_single_block_html(
                        cap_blk,
                        fn_by_block=fn_by_block,
                        available_footnotes=available_footnotes,
                        ref_registry=ref_registry,
                        seen_page_ids=seen_page_ids,
                        folio_to_page=folio_to_page,
                        proof_href_prefix=proof_href_prefix,
                        epub=epub,
                        heading_base=heading_base,
                        max_heading_level=max_next_h,
                    )
                )
            continue
        if (
            blk.alignment == "two_column_left"
            and idx + 1 < len(blocks)
            and blocks[idx + 1].alignment == "two_column_right"
        ):
            left_html = render_single_block_html(
                blk,
                fn_by_block=fn_by_block,
                available_footnotes=available_footnotes,
                ref_registry=ref_registry,
                seen_page_ids=seen_page_ids,
                folio_to_page=folio_to_page,
                proof_href_prefix=proof_href_prefix,
                epub=epub,
                heading_base=heading_base,
                max_heading_level=max_next_h,
            )
            right_html = render_single_block_html(
                blocks[idx + 1],
                fn_by_block=fn_by_block,
                available_footnotes=available_footnotes,
                ref_registry=ref_registry,
                seen_page_ids=seen_page_ids,
                folio_to_page=folio_to_page,
                proof_href_prefix=proof_href_prefix,
                epub=epub,
                heading_base=heading_base,
                max_heading_level=max_next_h,
            )
            out.append(f'<div class="two-col-pair">{left_html}{right_html}</div>')
            idx += 2
            continue
        if blk.type in {"heading", "letter_header"}:
            used_lvl = _compute_heading_level(blk, heading_base, max_next_h)
            out.append(
                render_single_block_html(
                    blk,
                    fn_by_block=fn_by_block,
                    available_footnotes=available_footnotes,
                    ref_registry=ref_registry,
                    seen_page_ids=seen_page_ids,
                    folio_to_page=folio_to_page,
                    proof_href_prefix=proof_href_prefix,
                    epub=epub,
                    heading_base=heading_base,
                    max_heading_level=max_next_h,
                )
            )
            max_next_h = min(6, max(max_next_h, used_lvl + 1))
            idx += 1
            continue
        out.append(
            render_single_block_html(
                blk,
                fn_by_block=fn_by_block,
                available_footnotes=available_footnotes,
                ref_registry=ref_registry,
                seen_page_ids=seen_page_ids,
                folio_to_page=folio_to_page,
                proof_href_prefix=proof_href_prefix,
                epub=epub,
                heading_base=heading_base,
                max_heading_level=max_next_h,
            )
        )
        idx += 1
    return "\n".join(out)


def render_footnotes_apparatus_html(
    footnote_ids: list[str],
    footnotes_map: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    *,
    seen_page_ids: set[int] | None = None,
    folio_to_page: dict[str, int] | None = None,
    proof_href_prefix: str | None = None,
    epub: bool = False,
    heading_label: str = "Editorial & Authorial Notes",
) -> str:
    """Render DPUB-ARIA 1.1 compliant bidirectional `#fn-...` <-> `#fnref-...` multi-paragraph apparatus."""
    items_out: list[str] = []
    for fid in dict.fromkeys(footnote_ids):
        fn = footnotes_map.get(fid)
        if fn is None:
            continue
        dom_id = html.escape(fn_anchor_id(fn.footnote_id), quote=True)
        lbl_esc = html.escape(fn.label or "*", quote=False)
        orphan_badge = (
            fn.is_orphan
            and f'<span class="fn-orphan-tag">[Unlinked note, p. {html.escape(fn.origin_folio or str(fn.origin_page))}]</span> '
        ) or ""
        raw_paras = [p for p in split_tagged_paragraphs_balanced(fn.tagged_text) if p.strip()] or [fn.tagged_text]
        rendered_paras = [
            render_inline_html(
                p,
                available_footnotes=footnotes_map,
                ref_registry=ref_registry,
                seen_page_ids=seen_page_ids,
                folio_to_page=folio_to_page,
                default_page=fn.origin_page,
                proof_href_prefix=proof_href_prefix,
                epub=epub,
            )
            for p in raw_paras
        ]
        refs = ref_registry.get(fn.footnote_id) or []
        backlinks = "".join(
            f' <a class="fn-backlink" role="doc-backlink" href="#{html.escape(r, quote=True)}" '
            f'aria-label="Return to citation {i}">↩{(len(refs) > 1 and f"<sup>{i}</sup>") or ""}</a>'
            for i, r in enumerate(refs, start=1)
        )
        p_tags: list[str] = []
        for p_idx, p_html in enumerate(rendered_paras):
            lead = (p_idx == 0 and epub and f'<span class="fn-label">{lbl_esc}.</span> {orphan_badge}') or (
                p_idx == 0 and orphan_badge
            ) or ""
            tail = (p_idx == len(rendered_paras) - 1 and backlinks) or ""
            p_tags.append(f"<p>{lead}{p_html}{tail}</p>")
        joined_p = "".join(p_tags)
        items_out.append(
            f'<aside id="{dom_id}" epub:type="footnote" role="doc-footnote" class="fn-item">{joined_p}</aside>'
            if epub
            else f'<li id="{dom_id}" class="fn-item"><span class="fn-label">{lbl_esc}.</span> <div class="fn-body">{joined_p}</div></li>'
        )
    if not items_out:
        return ""
    h_esc = html.escape(heading_label)
    body = "".join(items_out)
    return (
        f'<section epub:type="footnotes" class="apparatus-notes"><h2>{h_esc}</h2>{body}</section>'
        if epub
        else f'<section class="apparatus-notes" role="doc-endnotes"><h2 class="apparatus-heading">{h_esc}</h2><ol class="fn-list">{body}</ol></section>'
    )


def resolve_has_local_scans(edition_dir: Path, override: bool | None) -> bool:
    """Return whether local scan images exist under `<edition_dir>/proofing/scans/`."""
    if override is not None:
        return override
    return any(
        p.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg"}
        for p in (edition_dir / "proofing" / "scans").glob("page_*.*")
    )


def render_optional_links(specs: tuple[tuple[bool, str], ...], sep: str = "") -> str:
    """Concatenate HTML fragments whose condition flag is true."""
    return sep.join(fragment for enabled, fragment in specs if enabled)


def render_pager_slot(href: str | None, label: str | None) -> str:
    """Render an optional pager link or spacer span."""
    if not href or not label:
        return "<span></span>"
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'


EDITORIAL_SHARED_CSS = """/* Paper Lanterns v2 — Scholarly Letterpress & Epistolary Edition Stylesheet */
:root {
  --paper-bg: #faf7f2;
  --paper-elevated: #f3ede3;
  --paper-card: #fffdf9;
  --ink-primary: #1a1816;
  --ink-secondary: #524b44;
  --ink-muted: #7b7268;
  --accent-oxblood: #6e242b;
  --rule-hairline: rgba(26, 24, 22, 0.15);
  --font-serif: "EB Garamond", "ETbb", "Hoefler Text", "Baskerville", Georgia, serif;
  --font-sans: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--paper-bg);
  color: var(--ink-primary);
  font-family: var(--font-serif);
  font-size: 1.18rem;
  line-height: 1.72;
  text-rendering: optimizeLegibility;
}

a { color: var(--accent-oxblood); text-decoration-thickness: 1px; text-underline-offset: 0.16em; }
a:hover { color: var(--ink-primary); }

.masthead {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 0.6rem 1.4rem;
  padding: 0.8rem 2.2rem;
  background: rgba(250, 247, 242, 0.92);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--rule-hairline);
  font-family: var(--font-sans);
  font-size: 0.78rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.masthead-brand { font-weight: 600; color: var(--ink-primary); text-decoration: none; }
.masthead-nav { display: flex; flex-wrap: wrap; gap: 0.75rem 1.15rem; align-items: center; }
.masthead-nav a { color: var(--ink-secondary); text-decoration: none; }
.masthead-nav a:hover { color: var(--accent-oxblood); }

.tier-badge {
  padding: 0.2rem 0.55rem;
  border: 1px solid var(--rule-hairline);
  color: var(--accent-oxblood);
  font-weight: 600;
  font-size: 0.7rem;
}

.edition-container { max-width: 960px; margin: 0 auto; padding: 3.2rem 2rem 5rem; }
.reading-column { max-width: 68ch; margin: 0 auto; position: relative; }

.small-caps { font-variant: small-caps; letter-spacing: 0.04em; }
.unclear { border-bottom: 1px dotted var(--accent-oxblood); cursor: help; }

/* Epistolary Block Semantics & Spatial Alignment Fidelity */
.archival-block { margin: 0 0 1.05rem; }
.block-heading { font-weight: 500; line-height: 1.25; margin: 2.4rem 0 1.1rem; }
.block-letter-header {
  font-weight: 600;
  font-size: 1.12rem;
  letter-spacing: 0.03em;
  margin: 2.2rem 0 0.75rem;
  padding-top: 1.2rem;
  border-top: 1px solid var(--rule-hairline);
}
.block-dateline {
  font-variant-numeric: oldstyle-nums;
  color: var(--ink-secondary);
  margin: 0.45rem 0 0.85rem;
}
.block-salutation { margin: 0.6rem 0 0.75rem; }
.block-body { text-align: justify; hyphens: auto; }
.block-blockquote, .block-epigraph {
  margin: 1.15rem 1.8rem;
  padding-left: 1rem;
  border-left: 2px solid var(--rule-hairline);
  font-size: 1.06rem;
}
.block-verse {
  margin: 1.1rem 2rem;
  white-space: pre-wrap;
  line-height: 1.62;
}
.block-valediction { margin: 0.9rem 0 0.35rem; }
.block-signature {
  letter-spacing: 0.03em;
  margin: 0.25rem 0 1.2rem;
}
.block-postscript {
  margin-top: 1rem;
  padding-top: 0.55rem;
  border-top: 1px dashed var(--rule-hairline);
  font-size: 1.08rem;
}
.block-address {
  margin: 0.85rem 0 1.25rem;
  padding: 0.65rem 1rem;
  border: 1px solid var(--rule-hairline);
  background: var(--paper-elevated);
  font-size: 1.02rem;
}
.block-editorial {
  margin: 0 0 1.05rem;
  text-align: justify;
  hyphens: auto;
}
.unit-letter-excerpt {
  margin: 1.15rem 1.6rem;
  padding-left: 0.95rem;
  border-left: 2px solid var(--rule-hairline);
  font-size: 1.05rem;
}
.enclosure-span {
  margin: 0.9rem 0 0.9rem 1.25rem;
  padding-left: 0.95rem;
  border-left: 2px solid var(--rule-hairline);
}
.block-ornamental-break {
  text-align: center;
  letter-spacing: 0.35em;
  color: var(--ink-muted);
  margin: 1.8rem 0;
}
.block-margin-note {
  font-family: var(--font-sans);
  font-size: 0.78rem;
  line-height: 1.4;
  color: var(--ink-muted);
  padding: 0.25rem 0.6rem;
  border-left: 2px solid var(--rule-hairline);
  margin: 0.4rem 0 0.7rem;
}
.block-margin-note.margin-right {
  border-left: none;
  border-right: 2px solid var(--rule-hairline);
  text-align: right;
}
@media (min-width: 1180px) {
  .reading-column .block-margin-note.margin-left {
    float: left;
    clear: left;
    width: 11rem;
    margin: 0.15rem 1.25rem 0.6rem -12.5rem;
  }
  .reading-column .block-margin-note.margin-right {
    float: right;
    clear: right;
    width: 11rem;
    margin: 0.15rem -12.5rem 0.6rem 1.25rem;
  }
}

/* Spatial Alignments */
.align-left { text-align: left; }
.align-center { text-align: center; }
.align-right { text-align: right; margin-left: auto; }
.align-indented { padding-left: 2.4rem; }
.align-inline { display: inline; margin-right: 0.6rem; }
.align-cascading { text-align: left; }
.align-cascading .cascade-line {
  display: inline-block;
  padding-left: calc(var(--cascade-step, 0) * 2.2rem);
}

.two-col-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1.8rem;
  align-items: start;
  margin: 1rem 0;
}
@media (max-width: 680px) {
  .two-col-pair { grid-template-columns: 1fr; gap: 0.75rem; }
}

/* Folio Locus & Unavailable Banner */
.page-locus {
  margin: 0.9rem 0 0.35rem;
  text-align: right;
  font-family: var(--font-sans);
  font-size: 0.72rem;
}
.folio-marker {
  font-family: var(--font-sans);
  font-size: 0.74rem;
  color: var(--ink-muted);
  text-decoration: none;
  padding: 0.05rem 0.3rem;
}
.folio-marker:hover { color: var(--accent-oxblood); text-decoration: underline; }

.page-unavailable {
  margin: 1.5rem 0;
  padding: 0.95rem 1.2rem;
  border: 1px solid var(--accent-oxblood);
  background: rgba(110, 36, 43, 0.06);
  font-family: var(--font-sans);
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--accent-oxblood);
  text-align: center;
}

/* Tables */
.archival-table-wrap { overflow-x: auto; margin: 1.4rem 0; }
.archival-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.98rem;
  font-variant-numeric: tabular-nums;
}
.archival-table th, .archival-table td {
  padding: 0.45rem 0.65rem;
  border-bottom: 1px solid var(--rule-hairline);
  text-align: left;
  vertical-align: top;
}
.archival-table thead th {
  border-top: 1px solid var(--ink-primary);
  border-bottom: 1px solid var(--ink-primary);
  font-weight: 600;
}
.archival-table tr.table-subheader th {
  padding-top: 0.85rem;
  font-weight: 600;
  border-bottom: 1px solid var(--ink-secondary);
}

/* Letter Unit Wrapper & Editorial Framing */
.letter-unit {
  margin: 2.5rem 0;
  padding-bottom: 1.5rem;
  border-bottom: 1px solid var(--rule-hairline);
}
.letter-meta-bar {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 0.5rem 1rem;
  font-family: var(--font-sans);
  font-size: 0.76rem;
  color: var(--ink-muted);
  margin-bottom: 0.8rem;
}
.editorial-frame-box {
  margin: 1.2rem 0;
  padding: 0.85rem 1.15rem;
  background: var(--paper-elevated);
  border-left: 3px solid var(--ink-muted);
}
.editorial-frame-label {
  display: block;
  font-family: var(--font-sans);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--ink-muted);
  margin-bottom: 0.4rem;
}

/* Bidirectional Footnote Apparatus */
.fn-ref a {
  font-family: var(--font-sans);
  font-size: 0.74em;
  font-weight: 600;
  text-decoration: none;
  padding: 0 0.12em;
}
.apparatus-notes {
  margin-top: 3rem;
  padding-top: 1.4rem;
  border-top: 1px solid var(--ink-primary);
  font-size: 0.98rem;
}
.apparatus-heading {
  font-family: var(--font-sans);
  font-size: 0.78rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ink-muted);
  margin: 0 0 1rem;
}
.fn-list { list-style: none; padding: 0; margin: 0; }
.fn-item { margin-bottom: 0.65rem; display: flex; gap: 0.55rem; align-items: baseline; }
.fn-body { flex: 1; }
.fn-body p { margin: 0 0 0.45rem; }
.fn-body p:last-child { margin-bottom: 0; }
.fn-label { font-family: var(--font-sans); font-weight: 600; font-size: 0.82rem; min-width: 1.6rem; }
.fn-orphan-tag { font-family: var(--font-sans); font-size: 0.75rem; color: var(--accent-oxblood); }
.fn-backlink { font-family: var(--font-sans); font-size: 0.8rem; text-decoration: none; margin-left: 0.3rem; }

/* Scan Facsimile Drawer (<dialog>) */
dialog.scan-drawer {
  width: min(92vw, 960px);
  max-height: 90vh;
  border: 1px solid var(--rule-hairline);
  background: var(--paper-bg);
  color: var(--ink-primary);
  padding: 0;
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.28);
}
dialog.scan-drawer::backdrop { background: rgba(26, 24, 22, 0.55); }
.scan-drawer-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.75rem 1.2rem;
  border-bottom: 1px solid var(--rule-hairline);
  font-family: var(--font-sans);
  font-size: 0.82rem;
}
.scan-drawer-body {
  padding: 1rem;
  overflow: auto;
  max-height: calc(90vh - 3.4rem);
  text-align: center;
}
.scan-drawer-body img { max-width: 100%; height: auto; border: 1px solid var(--rule-hairline); }

/* Interactive Catalog Controls */
.catalog-filters {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 0.85rem;
  padding: 1.1rem;
  background: var(--paper-elevated);
  border: 1px solid var(--rule-hairline);
  margin-bottom: 1.6rem;
}
.catalog-filters label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-family: var(--font-sans);
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--ink-secondary);
}
.catalog-filters input, .catalog-filters select {
  padding: 0.45rem 0.6rem;
  border: 1px solid var(--rule-hairline);
  background: var(--paper-card);
  color: var(--ink-primary);
  font-family: var(--font-sans);
  font-size: 0.88rem;
}
.catalog-table { width: 100%; border-collapse: collapse; font-size: 1rem; }
.catalog-table th, .catalog-table td {
  padding: 0.65rem 0.55rem;
  border-bottom: 1px solid var(--rule-hairline);
  text-align: left;
  vertical-align: top;
}
.catalog-table th {
  font-family: var(--font-sans);
  font-size: 0.73rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--ink-muted);
}
.pager-nav {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  margin-top: 3rem;
  padding-top: 1.2rem;
  border-top: 1px solid var(--rule-hairline);
  font-family: var(--font-sans);
  font-size: 0.85rem;
}
"""

EDITORIAL_EPUB_CSS = """/* Paper Lanterns v3 — Dark-Mode-Safe EPUB 3.3 Stylesheet */
body {
  line-height: 1.65;
  margin: 0;
  padding: 0;
}

.small-caps { font-variant: small-caps; letter-spacing: 0.04em; }
.unclear { border-bottom: 1px dotted currentColor; }

.archival-block { margin: 0 0 0.95em; }
.block-heading { font-weight: 600; line-height: 1.28; margin: 1.9em 0 0.85em; }
.block-letter-header {
  font-weight: 600;
  font-size: 1.08em;
  letter-spacing: 0.03em;
  margin: 1.8em 0 0.65em;
  padding-top: 0.95em;
  border-top: 1px solid rgba(128, 128, 128, 0.3);
}
.block-dateline { font-variant-numeric: oldstyle-nums; opacity: 0.88; margin: 0.4em 0 0.75em; }
.block-salutation { margin: 0.55em 0 0.7em; }
.block-body, .block-editorial { text-align: justify; hyphens: auto; }
.block-blockquote, .block-epigraph, .unit-letter-excerpt, .enclosure-span {
  margin: 1em 1.4em;
  padding-left: 0.9em;
  border-left: 2px solid rgba(128, 128, 128, 0.35);
}
.block-verse {
  margin: 1em 1.6em;
  white-space: pre-wrap;
  line-height: 1.58;
}
.block-valediction { margin: 0.85em 0 0.3em; }
.block-signature { letter-spacing: 0.03em; margin: 0.2em 0 1.1em; }
.block-postscript {
  margin-top: 0.9em;
  padding-top: 0.5em;
  border-top: 1px dashed rgba(128, 128, 128, 0.35);
}
.block-address {
  margin: 0.8em 0 1.15em;
  padding: 0.55em 0.9em;
  border: 1px solid rgba(128, 128, 128, 0.32);
}
.block-ornamental-break { text-align: center; letter-spacing: 0.35em; opacity: 0.75; margin: 1.5em 0; }
.block-margin-note {
  font-size: 0.82em;
  line-height: 1.4;
  opacity: 0.82;
  padding: 0.2em 0.55em;
  border-left: 2px solid rgba(128, 128, 128, 0.35);
  margin: 0.35em 0 0.65em;
}
.block-margin-note.margin-right {
  border-left: none;
  border-right: 2px solid rgba(128, 128, 128, 0.35);
  text-align: right;
}

.align-left { text-align: left; }
.align-center { text-align: center; }
.align-right { text-align: right; }
.align-indented { padding-left: 2em; }
.align-inline { display: inline; margin-right: 0.5em; }
.align-cascading { text-align: left; }
.align-cascading .cascade-line { display: inline-block; padding-left: 1.5em; }

.page-locus { margin: 0.75em 0 0.25em; text-align: right; font-size: 0.75em; }
.folio-marker { font-size: 0.78em; opacity: 0.72; }
.page-unavailable {
  margin: 1.2em 0;
  padding: 0.8em 1em;
  border: 1px solid currentColor;
  font-size: 0.9em;
  font-weight: 600;
  text-align: center;
}

.archival-table-wrap { overflow-x: auto; margin: 1.2em 0; }
.archival-table { width: 100%; border-collapse: collapse; font-size: 0.94em; }
.archival-table th, .archival-table td {
  padding: 0.4em 0.55em;
  border-bottom: 1px solid rgba(128, 128, 128, 0.28);
  text-align: left;
  vertical-align: top;
}
.archival-table thead th, .archival-table tr.table-subheader th {
  border-bottom: 1px solid currentColor;
  font-weight: 600;
}

.letter-unit { margin: 2em 0; padding-bottom: 1.2em; border-bottom: 1px solid rgba(128, 128, 128, 0.28); }
.fn-ref a { font-size: 0.78em; font-weight: 600; text-decoration: none; }
.apparatus-notes { margin-top: 2.4em; padding-top: 1.1em; border-top: 1px solid currentColor; font-size: 0.92em; }
.fn-item { margin-bottom: 0.6em; }
.fn-item p { margin: 0 0 0.4em; }
.fn-label { font-weight: 600; }
.fn-orphan-tag { font-size: 0.82em; opacity: 0.82; }
.fn-backlink { font-size: 0.85em; text-decoration: none; margin-left: 0.25em; }
"""
