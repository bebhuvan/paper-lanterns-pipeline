"""Scholarly Markdown (`markdown/<book_id>.md`) and TEI P5 XML (`tei/<book_id>.tei.xml`) renderers.

ROLE: Export CollectionAST into single-volume Scholarly Markdown and `tei_all.rng`/CMIF-compliant TEI P5 XML.
INPUTS: CollectionAST from lanterns.core.models.
OUTPUTS: markdown/<book_id>.md and tei/<book_id>.tei.xml (tessellated `<div>`s, unified `<opener>`/`<closer>`, CMIF `<correspDesc>`).
INVARIANT: Pure structural projection; zero prose-word regexes; zero `book_id` branching; 100% block & footnote conservation.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lanterns.core.models import (
    BlockIR,
    ChildSpan,
    CollectionAST,
    CorrespondentRef,
    FootnoteItem,
    LetterUnit,
)
from lanterns.core.tags import split_tagged_lines_balanced, split_tagged_paragraphs_balanced
from lanterns.renderers.html_utils import (
    TEI_TAG_CLOSE,
    TEI_TAG_OPEN,
    alloc_fnref_id,
    atomic_write_text,
    coalesce_continued_blocks,
    collect_section_footnotes,
    count_redundant_leading_headings,
    fn_anchor_id,
    footnotes_by_block_map,
    format_markdown_footnote,
    index_ast_blocks,
    iter_section_render_units,
    render_inline_markdown,
    sanitize_xml_id,
    tokenize_tagged_text,
)

_TEI_TEXT_MARKER_RE = re.compile(
    r"\[\[NOTE:([^:\]]+):(?:(\d+):)?([^\]]+)\]\]"
    r"|\[\[(?:FN)?FOLIO:(\d+):([^\]]*)\]\]"
    r"|\[p\.\s*([^\]]+?)\]"
)
_W3C_DATE_RE = re.compile(r"^\d{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?$")

_MD_LINE_PREFIX = {
    "heading": "### ",
    "letter_header": "### ",
    "blockquote": "> ",
    "epigraph": "> ",
}
_SKIP_TYPES: frozenset[str] = frozenset({"running_header", "folio", "scanner_residue", "noise", "footnote"})
_HEAD_TYPES: frozenset[str] = frozenset({"heading", "letter_header"})
_OPENER_TYPES: frozenset[str] = frozenset({"epigraph", "dateline", "salutation", "address_block"})
_CLOSER_TYPES: frozenset[str] = frozenset({"valediction", "signature", "dateline", "address_block"})
_PS_TRAIL_TYPES: frozenset[str] = frozenset({"postscript", "body", "blockquote", "verse", "editorial"}) | _CLOSER_TYPES
_COMMON_TYPES: frozenset[str] = frozenset(
    {
        "body",
        "editorial",
        "blockquote",
        "epigraph",
        "verse",
        "list_item",
        "table_caption",
        "table_row",
        "illustration",
        "facsimile_plate",
        "caption",
        "imprint",
        "toc_entry",
        "index_entry",
    }
)
_OPENER_CLOSER_TAG: dict[str, str] = {
    "dateline": "dateline",
    "salutation": "salute",
    "valediction": "salute",
    "signature": "signed",
}
_TEI_INLINE_MAP: dict[str, dict[str, str]] = {"open": TEI_TAG_OPEN, "close": TEI_TAG_CLOSE}
_PLATE_LABELS: dict[str, str] = {"facsimile_plate": "Facsimile plate", "illustration": "Illustration"}
_CORR_TAG_BY_KIND: dict[str, str] = {"org": "orgName", "person": "persName"}
_UNIT_DIV_TYPE: dict[str, str] = {
    "letter": "letter",
    "letter_excerpt": "letter_excerpt",
    "narrative": "narrative",
    "editorial_bridge": "editorial",
    "document": "document",
    "apparatus": "apparatus",
}


@dataclass
class _TeiCtx:
    fn_by_block: dict[str, list[FootnoteItem]]
    available_footnotes: dict[str, FootnoteItem]
    ref_registry: dict[str, list[str]]
    seen_pb_pages: set[int]
    used_ids: set[str]


@dataclass(frozen=True)
class _FloatingItem:
    xml: str


_DivItem = BlockIR | _FloatingItem


def corresp_xml_id(letter_id: str) -> str:
    """Return canonical `<correspDesc xml:id="letter-...">` identifier."""
    raw = letter_id.strip()
    return sanitize_xml_id(raw if raw.startswith("letter-") else f"letter-{raw}", "letter")


def _md_render_table_group(
    blocks: list[BlockIR],
    start_idx: int,
    fn_by_block: dict[str, list[FootnoteItem]],
) -> tuple[list[str], int]:
    out: list[str] = []
    idx = start_idx
    if blocks[idx].type == "table_caption":
        cap = blocks[idx]
        out.append(f"**{render_inline_markdown(cap.tagged_text, fn_by_block.get(cap.block_id))}**\n")
        idx += 1
    rows: list[list[str]] = []
    while idx < len(blocks) and blocks[idx].type == "table_row":
        rblk = blocks[idx]
        raw_parts = [c.text for c in (rblk.cells or ())] or rblk.tagged_text.split("|")
        rows.append([render_inline_markdown(p, fn_by_block.get(rblk.block_id)).replace("\n", " ").strip() for p in raw_parts])
        idx += 1
    if rows:
        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        out.extend(["| " + " | ".join(padded[0]) + " |", "| " + " | ".join("---" for _ in range(width)) + " |"])
        out.extend("| " + " | ".join(r) + " |" for r in padded[1:])
        out.append("")
    return out, idx


def _md_block_lines(
    blocks: list[BlockIR],
    fn_by_block: dict[str, list[FootnoteItem]],
    *,
    leading_section_title: str = "",
) -> list[str]:
    blocks, fn_by_block = coalesce_continued_blocks(blocks, fn_by_block, include_folio_tokens=False)
    lines: list[str] = []
    idx = count_redundant_leading_headings(blocks, leading_section_title)
    while idx < len(blocks):
        blk = blocks[idx]
        if blk.type in _SKIP_TYPES:
            idx += 1
            continue
        if blk.type in {"table_caption", "table_row"}:
            tbl_lines, idx = _md_render_table_group(blocks, idx, fn_by_block)
            lines.extend(tbl_lines)
            continue

        txt = render_inline_markdown(blk.tagged_text, fn_by_block.get(blk.block_id))
        if blk.type == "list_item":
            lines.append(f"- {txt}")
        elif blk.type == "ornamental_break":
            lines.append("* * *\n")
        elif blk.type == "margin_note":
            lines.append(f"> *[Margin: {txt.replace(chr(10), ' ')}]*\n")
        elif blk.type in _PLATE_LABELS and not txt.strip():
            lines.append(f"*[{_PLATE_LABELS[blk.type]}, p. {blk.printed_folio or blk.page_number}]*\n")
        else:
            prefix = _MD_LINE_PREFIX.get(blk.type, "")
            body = "  \n".join(txt.split("\n")) if (not prefix and (blk.line_breaks_preserved or blk.type == "verse")) else txt
            lines.append(f"{prefix}{body}\n")
        idx += 1
    return lines


def render_scholarly_markdown(ast: CollectionAST, edition_dir: str | Path) -> dict[str, Any]:
    """Render single-volume Scholarly Markdown into `<edition_dir>/markdown/<book_id>.md`."""
    md_dir = Path(edition_dir) / "markdown"
    md_dir.mkdir(parents=True, exist_ok=True)
    out_path = md_dir / f"{ast.book_id}.md"

    block_lookup = index_ast_blocks(ast)
    fn_by_block = footnotes_by_block_map(ast.footnotes)

    lines: list[str] = [
        f"# {ast.title}",
        "",
        f"- **Author / Editor**: {ast.author_or_editor or 'Public Domain'}",
        f"- **Year**: {ast.year or 'n.d.'}",
        f"- **Letters**: {len(ast.letters)}",
        f"- **Pages**: {ast.total_pages}",
        "",
    ]

    for sec in ast.sections:
        lines.append(f"## {sec.title_plain}\n")
        for upage in sorted(sec.unavailable_pages):
            lines.append(f"> **[Page {upage} unavailable — inspect source scan]**\n")
        sec_fn_ids, sec_fns = collect_section_footnotes(sec, ast)
        for u_idx, (ukind, ublks, lu, _) in enumerate(iter_section_render_units(sec, ast, block_lookup)):
            if ukind == "letter" and lu is not None:
                corr = " → ".join(p for p in (lu.metadata.sender_display, lu.metadata.recipient_display) if p)
                lines.append(f"### Letter {lu.sequence_index}{(corr and ': ' + corr) or ''}\n")
                lines.extend(_md_block_lines(ublks, fn_by_block))
            elif ublks:
                lines.extend(
                    _md_block_lines(
                        ublks,
                        fn_by_block,
                        leading_section_title=(u_idx == 0 and sec.title_plain) or "",
                    )
                )

        if sec_fn_ids:
            lines.append("### Notes\n")
            for fid in sec_fn_ids:
                if fid in sec_fns:
                    lines.append(format_markdown_footnote(sec_fns[fid]))
            lines.append("")

    atomic_write_text(out_path, "\n".join(lines).rstrip() + "\n")
    return {"markdown_path": str(out_path)}


def _unique_xml_id(candidate: str, used_ids: set[str]) -> str:
    clean = sanitize_xml_id(candidate, "id")
    if clean not in used_ids:
        used_ids.add(clean)
        return clean
    seq = 2
    while f"{clean}-{seq}" in used_ids:
        seq += 1
    final = f"{clean}-{seq}"
    used_ids.add(final)
    return final


def _tei_pb_if_needed(blk: BlockIR, seen_pb_pages: set[int], used_ids: set[str]) -> str:
    if blk.page_number in seen_pb_pages:
        return ""
    seen_pb_pages.add(blk.page_number)
    page_id = _unique_xml_id(f"page-{blk.page_number}", used_ids)
    fol_esc = html.escape(blk.printed_folio or str(blk.page_number), quote=True)
    return f'<pb xml:id="{page_id}" n="{fol_esc}"/>'


def _tei_escape_chunk(chunk: str, ctx: _TeiCtx) -> str:
    out: list[str] = []
    last = 0
    for match in _TEI_TEXT_MARKER_RE.finditer(chunk):
        if match.start() > last:
            out.append(html.escape(chunk[last : match.start()], quote=False))
        last = match.end()
        if match.group(1) is not None:
            fn_id, lbl = match.group(1), match.group(3)
            if fn_id in ctx.available_footnotes:
                tid = html.escape(fn_anchor_id(fn_id), quote=True)
                rid = html.escape(alloc_fnref_id(fn_id, ctx.ref_registry), quote=True)
                out.append(f'<ref xml:id="{rid}" type="noteref" target="#{tid}">{html.escape(lbl, quote=False)}</ref>')
            else:
                out.append(f'<hi rend="sup">{html.escape(lbl, quote=False)}</hi>')
        elif match.group(4) is not None:
            pnum = int(match.group(4))
            fol = html.escape(match.group(5) or str(pnum), quote=True)
            id_attr = ""
            if pnum not in ctx.seen_pb_pages:
                ctx.seen_pb_pages.add(pnum)
                pid = _unique_xml_id(f"page-{pnum}", ctx.used_ids)
                id_attr = f' xml:id="{pid}"'
            out.append(f'<pb{id_attr} n="{fol}"/>')
        else:
            fol = html.escape(match.group(6).strip(), quote=True)
            out.append(f'<pb n="{fol}"/>')
    if last < len(chunk):
        out.append(html.escape(chunk[last:], quote=False))
    return "".join(out)


def _render_tei_inline(
    tagged_text: str,
    block_footnotes: list[FootnoteItem] | None = None,
    ctx: _TeiCtx | None = None,
    *,
    available_footnotes: dict[str, FootnoteItem] | None = None,
    ref_registry: dict[str, list[str]] | None = None,
    seen_pb_pages: set[int] | None = None,
    preserve_lb: bool = False,
) -> str:
    active_ctx = ctx or _TeiCtx(
        fn_by_block={},
        available_footnotes=available_footnotes or {},
        ref_registry={} if ref_registry is None else ref_registry,
        seen_pb_pages=set() if seen_pb_pages is None else seen_pb_pages,
        used_ids=set(),
    )
    queue: dict[str, list[FootnoteItem]] = {}
    for fn in block_footnotes or []:
        queue.setdefault(fn.label.strip().strip("[]()"), []).append(fn)

    def _one_line(line_txt: str) -> str:
        tokens = tokenize_tagged_text(line_txt)
        parts: list[str] = []
        idx = 0
        while idx < len(tokens):
            kind, val = tokens[idx]
            is_fn = (
                kind == "open"
                and val in {"fnref", "sup"}
                and idx + 2 < len(tokens)
                and tokens[idx + 1][0] == "text"
                and tokens[idx + 2] == ("close", val)
            )
            bucket = queue.get(tokens[idx + 1][1].strip().strip("[]()")) if is_fn else None
            if bucket:
                fn = bucket.pop(0)
                tid = html.escape(fn_anchor_id(fn.footnote_id), quote=True)
                rid = html.escape(alloc_fnref_id(fn.footnote_id, active_ctx.ref_registry), quote=True)
                parts.append(f'<ref xml:id="{rid}" type="noteref" target="#{tid}">{html.escape(tokens[idx + 1][1], quote=False)}</ref>')
                idx += 3
                continue
            parts.append(_TEI_INLINE_MAP[kind][val] if kind in _TEI_INLINE_MAP else _tei_escape_chunk(val, active_ctx))
            idx += 1
        return "".join(parts)

    if preserve_lb and "\n" in tagged_text:
        return "<lb/>".join(_one_line(ln) for ln in split_tagged_lines_balanced(tagged_text))
    return _one_line(tagged_text)


def _rend_attr(val: str) -> str:
    return "" if val in {"", "left"} else f' rend="{html.escape(val, quote=True)}"'


def _render_tei_address(blk: BlockIR, ctx: _TeiCtx) -> str:
    fns = ctx.fn_by_block.get(blk.block_id)
    raw_lines = [ln for ln in split_tagged_lines_balanced(blk.tagged_text) if ln.strip()] or [blk.tagged_text]
    addr_lines = "".join(f"<addrLine>{_render_tei_inline(ln, fns, ctx)}</addrLine>" for ln in raw_lines)
    return f"<address>{addr_lines}</address>"


def _render_opener_or_closer_item(blk: BlockIR, ctx: _TeiCtx) -> str:
    pb = _tei_pb_if_needed(blk, ctx.seen_pb_pages, ctx.used_ids)
    rend = _rend_attr(blk.alignment)
    if blk.type == "address_block":
        return f"{pb}{_render_tei_address(blk, ctx)}"
    if blk.type == "epigraph":
        inner = _render_tei_inline(blk.tagged_text, ctx.fn_by_block.get(blk.block_id), ctx, preserve_lb=blk.line_breaks_preserved)
        return f"{pb}<epigraph><p{rend}>{inner}</p></epigraph>"
    tag = _OPENER_CLOSER_TAG[blk.type]
    inner = _render_tei_inline(blk.tagged_text, ctx.fn_by_block.get(blk.block_id), ctx, preserve_lb=True)
    return f"{pb}<{tag}{rend}>{inner}</{tag}>"


def _render_tei_verse(blk: BlockIR, pb: str, rend: str, ctx: _TeiCtx) -> str:
    fns = ctx.fn_by_block.get(blk.block_id)
    v_lines = [ln for ln in split_tagged_lines_balanced(blk.tagged_text) if ln.strip()] or [blk.tagged_text]
    l_xml = "".join(f"<l>{_render_tei_inline(ln, fns, ctx)}</l>" for ln in v_lines)
    return f"{pb}<lg{rend}>{l_xml}</lg>"


def _render_tei_common_single(blk: BlockIR, ctx: _TeiCtx) -> str:
    pb = _tei_pb_if_needed(blk, ctx.seen_pb_pages, ctx.used_ids)
    rend = _rend_attr(blk.alignment)
    fns = ctx.fn_by_block.get(blk.block_id)
    if blk.type == "ornamental_break":
        return f'{pb}<milestone unit="section" rend="ornament"/>'
    if blk.type == "margin_note":
        return f'{pb}<note place="margin">{_render_tei_inline(blk.tagged_text, fns, ctx)}</note>'
    if blk.type == "verse":
        return _render_tei_verse(blk, pb, rend, ctx)
    if blk.type in _PLATE_LABELS:
        inner = _render_tei_inline(blk.tagged_text, fns, ctx)
        fallback = html.escape(f"{blk.type} on p. {blk.printed_folio or blk.page_number}")
        return f'{pb}<figure type="{blk.type}"><figDesc>{inner or fallback}</figDesc></figure>'
    preserve_lb = blk.line_breaks_preserved or blk.type in {"dateline", "salutation", "valediction", "signature", "address_block"}
    inner = _render_tei_inline(blk.tagged_text, fns, ctx, preserve_lb=preserve_lb)
    if blk.type in {"blockquote", "epigraph"}:
        return f"{pb}<quote><p{rend}>{inner}</p></quote>"
    p_rend = _rend_attr(blk.alignment if blk.type in {"body", "editorial"} else blk.type)
    return f"{pb}<p{p_rend}>{inner}</p>"


def _render_tei_list(items: list[_DivItem], start: int, ctx: _TeiCtx) -> tuple[str, int]:
    first = items[start]
    assert isinstance(first, BlockIR)
    list_pb = _tei_pb_if_needed(first, ctx.seen_pb_pages, ctx.used_ids)
    li_xml: list[str] = []
    idx = start
    while idx < len(items) and isinstance(items[idx], BlockIR) and items[idx].type == "list_item":  # type: ignore[union-attr]
        cur = items[idx]
        assert isinstance(cur, BlockIR)
        ipb = _tei_pb_if_needed(cur, ctx.seen_pb_pages, ctx.used_ids)
        li_xml.append(f"{ipb}<item>{_render_tei_inline(cur.tagged_text, ctx.fn_by_block.get(cur.block_id), ctx)}</item>")
        idx += 1
    return f"{list_pb}<list>{''.join(li_xml)}</list>", idx


def _render_tei_table(items: list[_DivItem], start: int, ctx: _TeiCtx) -> tuple[str, int]:
    first = items[start]
    assert isinstance(first, BlockIR)
    tbl_pb = _tei_pb_if_needed(first, ctx.seen_pb_pages, ctx.used_ids)
    head_xml = ""
    idx = start
    if first.type == "table_caption":
        head_xml = f"<head>{_render_tei_inline(first.tagged_text, ctx.fn_by_block.get(first.block_id), ctx)}</head>"
        idx += 1
    rows_xml: list[str] = []
    while idx < len(items) and isinstance(items[idx], BlockIR) and items[idx].type == "table_row":  # type: ignore[union-attr]
        rblk = items[idx]
        assert isinstance(rblk, BlockIR)
        rpb = _tei_pb_if_needed(rblk, ctx.seen_pb_pages, ctx.used_ids)
        cells = [c.text for c in (rblk.cells or ())] or [p.strip() for p in rblk.tagged_text.split("|")]
        rfns = ctx.fn_by_block.get(rblk.block_id)
        c_xml = "".join(f"<cell>{_render_tei_inline(ct, rfns, ctx)}</cell>" for ct in cells)
        rows_xml.append(f"{rpb}<row>{c_xml}</row>")
        idx += 1
    if not rows_xml:
        return _render_tei_common_single(first, ctx), start + 1
    return f"{tbl_pb}<table>{head_xml}{''.join(rows_xml)}</table>", idx


def _render_tei_common_stream(items: list[_DivItem], ctx: _TeiCtx) -> list[str]:
    out: list[str] = []
    idx = 0
    while idx < len(items):
        cur = items[idx]
        if isinstance(cur, _FloatingItem):
            out.append(cur.xml)
            idx += 1
        elif cur.type == "list_item":
            xml, idx = _render_tei_list(items, idx, ctx)
            out.append(xml)
        elif cur.type in {"table_caption", "table_row"}:
            xml, idx = _render_tei_table(items, idx, ctx)
            out.append(xml)
        else:
            out.append(_render_tei_common_single(cur, ctx))
            idx += 1
    return out


def _item_is_common(item: _DivItem) -> bool:
    return isinstance(item, _FloatingItem) or item.type in _COMMON_TYPES


def _find_postscript_start(items: list[_DivItem], top_end: int) -> int:
    for idx in range(top_end, len(items)):
        cur = items[idx]
        if isinstance(cur, BlockIR) and cur.type == "postscript":
            tail_ok = all(isinstance(it, BlockIR) and it.type in _PS_TRAIL_TYPES for it in items[idx:])
            if tail_ok and any(_item_is_common(it) for it in items[top_end:idx]):
                return idx
    return len(items)


def _partition_div_indices(items: list[_DivItem]) -> tuple[int, int, int, int]:
    h_end = 0
    while h_end < len(items) and isinstance(items[h_end], BlockIR) and items[h_end].type in _HEAD_TYPES:  # type: ignore[union-attr]
        h_end += 1
    top_end = h_end
    while top_end < len(items) and isinstance(items[top_end], BlockIR) and items[top_end].type in _OPENER_TYPES:  # type: ignore[union-attr]
        top_end += 1
    if not any(_item_is_common(it) for it in items[top_end:]):
        start_m = h_end * int(h_end < len(items))
        return start_m, start_m, len(items), len(items)

    ps_start = _find_postscript_start(items, top_end)
    bot_start = ps_start
    while (
        bot_start > top_end
        and isinstance(items[bot_start - 1], BlockIR)
        and items[bot_start - 1].type in _CLOSER_TYPES  # type: ignore[union-attr]
        and any(_item_is_common(it) for it in items[top_end : bot_start - 1])
    ):
        bot_start -= 1
    return h_end, top_end, bot_start, ps_start


def _render_tei_postscript(ps_items: list[_DivItem], ctx: _TeiCtx) -> str:
    c_start = len(ps_items)
    while c_start > 1 and isinstance(ps_items[c_start - 1], BlockIR) and ps_items[c_start - 1].type in _CLOSER_TYPES:  # type: ignore[union-attr]
        c_start -= 1
    body_parts = _render_tei_common_stream(ps_items[:c_start], ctx)
    closer_xml = ""
    if c_start < len(ps_items):
        c_inner = "".join(_render_opener_or_closer_item(it, ctx) for it in ps_items[c_start:] if isinstance(it, BlockIR))
        closer_xml = f"<closer>{c_inner}</closer>"
    return f"<postscript>{''.join(body_parts)}{closer_xml}</postscript>"


def _splice_child_spans(
    visible: list[BlockIR],
    children: list[ChildSpan],
    ctx: _TeiCtx,
) -> list[_DivItem]:
    if not children:
        return list(visible)
    span_by_start: dict[str, tuple[str, set[str]]] = {}
    claimed: set[str] = set()
    vis_ids = {b.block_id for b in visible}
    for ch in children:
        c_ids = [bid for bid in ch.block_ids if bid in vis_ids and bid not in claimed]
        if c_ids and len(c_ids) < len(visible):
            span_by_start[c_ids[0]] = (ch.kind, set(c_ids))
            claimed.update(c_ids)

    items: list[_DivItem] = []
    idx = 0
    while idx < len(visible):
        spec = span_by_start.get(visible[idx].block_id)
        if spec is None:
            items.append(visible[idx])
            idx += 1
            continue
        ckind, cset = spec
        c_blks: list[BlockIR] = []
        while idx < len(visible) and visible[idx].block_id in cset:
            c_blks.append(visible[idx])
            idx += 1
        inner = _render_tei_div_blocks(c_blks, [], ctx)
        items.append(_FloatingItem(f'<floatingText type="{html.escape(ckind, quote=True)}"><body><div>{inner}</div></body></floatingText>'))
    return items


def _render_tei_div_blocks(blocks: list[BlockIR], children: list[ChildSpan], ctx: _TeiCtx) -> str:
    saved_fns = ctx.fn_by_block
    coalesced, merged_fns = coalesce_continued_blocks(blocks, saved_fns)
    ctx.fn_by_block = merged_fns
    visible = [b for b in coalesced if b.type not in _SKIP_TYPES]
    if not visible:
        ctx.fn_by_block = saved_fns
        return ""

    items = _splice_child_spans(visible, children, ctx)
    h_end, top_end, bot_start, ps_start = _partition_div_indices(items)

    out: list[str] = []
    for h_it in items[:h_end]:
        assert isinstance(h_it, BlockIR)
        pb = _tei_pb_if_needed(h_it, ctx.seen_pb_pages, ctx.used_ids)
        rend = _rend_attr(h_it.alignment)
        out.append(f"{pb}<head{rend}>{_render_tei_inline(h_it.tagged_text, ctx.fn_by_block.get(h_it.block_id), ctx)}</head>")

    if top_end > h_end:
        op_inner = "".join(_render_opener_or_closer_item(it, ctx) for it in items[h_end:top_end] if isinstance(it, BlockIR))
        out.append(f"<opener>{op_inner}</opener>")

    out.extend(_render_tei_common_stream(items[top_end:bot_start], ctx))

    if ps_start > bot_start:
        cl_inner = "".join(_render_opener_or_closer_item(it, ctx) for it in items[bot_start:ps_start] if isinstance(it, BlockIR))
        out.append(f"<closer>{cl_inner}</closer>")

    if ps_start < len(items):
        out.append(_render_tei_postscript(items[ps_start:], ctx))

    ctx.fn_by_block = saved_fns
    return "\n".join(out)


def _format_corresp_refs(refs: list[CorrespondentRef], fallback_display: str | None) -> list[str]:
    valid = [r for r in refs if r.name.strip() and r.name.strip().casefold() != "unknown"]
    if valid:
        return [
            f"<{_CORR_TAG_BY_KIND.get(r.kind, 'persName')}>{html.escape(r.name.strip())}</{_CORR_TAG_BY_KIND.get(r.kind, 'persName')}>"
            for r in valid
        ]
    disp = (fallback_display or "").strip()
    return [f"<persName>{html.escape(disp)}</persName>"] if (disp and disp.casefold() != "unknown") else []


def _format_corresp_sent_action(ltr: LetterUnit) -> str:
    meta = ltr.metadata
    parts = _format_corresp_refs(meta.senders, meta.sender_display)
    place = (meta.place or "").strip()
    if place:
        parts.append(f"<placeName>{html.escape(place)}</placeName>")
    iso = (meta.date_iso or "").strip()
    when_attr = (bool(_W3C_DATE_RE.match(iso)) and f' when="{iso}"') or ""
    date_txt = (meta.dateline_plain or iso).strip()
    if place and date_txt.startswith(place):
        date_txt = date_txt[len(place) :].lstrip(" ,.;—-") or date_txt
    if date_txt:
        parts.append(f"<date{when_attr}>{html.escape(date_txt)}</date>")
    return (parts and f'        <correspAction type="sent">{"".join(parts)}</correspAction>') or ""


def _build_corresp_descriptions(letters: list[LetterUnit], used_ids: set[str]) -> tuple[str, dict[str, str]]:
    id_map: dict[str, str] = {}
    descs: list[str] = []
    for ltr in letters:
        cid = _unique_xml_id(corresp_xml_id(ltr.letter_id), used_ids)
        id_map[ltr.letter_id] = cid
        sent_xml = _format_corresp_sent_action(ltr)
        rec_refs = _format_corresp_refs(ltr.metadata.recipients, ltr.metadata.recipient_display)
        rec_xml = (rec_refs and f'        <correspAction type="received">{"".join(rec_refs)}</correspAction>') or ""
        body_lines = [ln for ln in (sent_xml, rec_xml) if ln] or ["        <note>Correspondents unrecorded in source</note>"]
        descs.append(f'      <correspDesc xml:id="{html.escape(cid, quote=True)}">\n' + "\n".join(body_lines) + "\n      </correspDesc>")
    return "\n".join(descs), id_map


def _render_tei_footnote(fn: FootnoteItem, nid: str, ctx: _TeiCtx) -> str:
    n_attr = html.escape(fn.label or "*", quote=True)
    paras = split_tagged_paragraphs_balanced(fn.tagged_text)
    orphan = (fn.is_orphan and f"[Unlinked note, p. {html.escape(fn.origin_folio or str(fn.origin_page))}] ") or ""
    p_xml = "".join(
        f"<p>{((i == 0 and orphan) or '')}{_render_tei_inline(p_txt, None, ctx)}</p>"
        for i, p_txt in enumerate(paras)
    )
    return f'<note place="foot" xml:id="{html.escape(nid, quote=True)}" n="{n_attr}">{p_xml}</note>'


def _render_tei_section(
    sec: Any,
    ast: CollectionAST,
    block_lookup: dict[str, BlockIR],
    corresp_id_by_letter: dict[str, str],
    emitted_fn_ids: set[str],
    ctx: _TeiCtx,
) -> str:
    sec_xml_id = _unique_xml_id(f"sec-{sec.section_id}", ctx.used_ids)
    sec_children: list[str] = [f"<head>{html.escape(sec.title_plain)}</head>"]

    for upage in sorted(sec.unavailable_pages):
        if upage not in ctx.seen_pb_pages:
            ctx.seen_pb_pages.add(upage)
            gap_id = _unique_xml_id(f"page-{upage}", ctx.used_ids)
            sec_children.append(f'<pb xml:id="{gap_id}" n="{upage}"/><gap reason="unavailable" unit="page" quantity="1"/>')

    for u_idx, (ukind, raw_blks, lu, su) in enumerate(iter_section_render_units(sec, ast, block_lookup)):
        skip_n = (u_idx == 0 and ukind != "letter" and count_redundant_leading_headings(raw_blks, sec.title_plain)) or 0
        if skip_n > 0 and raw_blks:
            pb0 = _tei_pb_if_needed(raw_blks[0], ctx.seen_pb_pages, ctx.used_ids)
            if pb0:
                sec_children.append(pb0)
        ublks = raw_blks[skip_n:]
        u_children = (lu and lu.children) or (su and su.children) or []
        inner = _render_tei_div_blocks(ublks, list(u_children), ctx)
        if not inner:
            continue
        if lu is not None and lu.letter_id in corresp_id_by_letter:
            cid = corresp_id_by_letter[lu.letter_id]
            div_id = _unique_xml_id(f"div-{cid}", ctx.used_ids)
            sec_children.append(f'<div xml:id="{html.escape(div_id, quote=True)}" type="letter" corresp="#{html.escape(cid, quote=True)}">\n{inner}\n</div>')
        else:
            div_type = _UNIT_DIV_TYPE.get(ukind, "narrative")
            base_id = (su and su.unit_id) or f"{sec.section_id}-u{u_idx + 1}"
            div_id = _unique_xml_id(f"div-{base_id}", ctx.used_ids)
            sec_children.append(f'<div xml:id="{html.escape(div_id, quote=True)}" type="{div_type}">\n{inner}\n</div>')

    sec_fn_order, _ = collect_section_footnotes(sec, ast)
    notes_xml: list[str] = []
    for fid in sec_fn_order:
        if fid not in emitted_fn_ids and fid in ast.footnotes:
            emitted_fn_ids.add(fid)
            fn = ast.footnotes[fid]
            nid = _unique_xml_id(fn_anchor_id(fn.footnote_id), ctx.used_ids)
            notes_xml.append(_render_tei_footnote(fn, nid, ctx))
    if notes_xml:
        notes_div_id = _unique_xml_id(f"notes-{sec.section_id}", ctx.used_ids)
        sec_children.append(f'<div xml:id="{html.escape(notes_div_id, quote=True)}" type="notes"><head>Notes</head>\n' + "\n".join(notes_xml) + "\n</div>")

    return f'<div xml:id="{html.escape(sec_xml_id, quote=True)}" type="{html.escape(sec.kind, quote=True)}">\n' + "\n".join(sec_children) + "\n</div>"


def render_tei_edition(ast: CollectionAST, edition_dir: str | Path) -> dict[str, Any]:
    """Render `CollectionAST` into `tei_all.rng`-valid TEI P5 XML at `<edition_dir>/tei/<book_id>.tei.xml`."""
    tei_dir = Path(edition_dir) / "tei"
    tei_dir.mkdir(parents=True, exist_ok=True)
    out_path = tei_dir / f"{ast.book_id}.tei.xml"

    used_ids: set[str] = set()
    corresp_xml, corresp_id_by_letter = _build_corresp_descriptions(ast.letters, used_ids)
    block_lookup = index_ast_blocks(ast)
    ctx = _TeiCtx(
        fn_by_block=footnotes_by_block_map(ast.footnotes),
        available_footnotes=ast.footnotes,
        ref_registry={},
        seen_pb_pages=set(),
        used_ids=used_ids,
    )
    emitted_fn_ids: set[str] = set()

    section_divs = [
        _render_tei_section(sec, ast, block_lookup, corresp_id_by_letter, emitted_fn_ids, ctx)
        for sec in ast.sections
    ]
    orphan_notes = [
        _render_tei_footnote(fn, _unique_xml_id(fn_anchor_id(fid), used_ids), ctx)
        for fid, fn in ast.footnotes.items()
        if fid not in emitted_fn_ids
    ]
    if orphan_notes:
        app_id = _unique_xml_id("notes-unlinked", used_ids)
        section_divs.append(f'<div xml:id="{app_id}" type="notes"><head>Unlinked Notes</head>\n' + "\n".join(orphan_notes) + "\n</div>")

    body_xml = "\n      ".join(section_divs) or '<div><p>Empty edition</p></div>'
    lang_esc = html.escape(ast.language or "en", quote=True)
    author_el = f"<author>{html.escape(ast.author_or_editor or 'Public Domain')}</author>"
    date_el = f"<date>{ast.year or 'Public Domain'}</date>"
    profile_block = (corresp_xml and f"\n    <profileDesc>\n{corresp_xml}\n    </profileDesc>") or ""

    tei_doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0" xml:lang="{lang_esc}">
  <teiHeader>
    <fileDesc>
      <titleStmt>
        <title>{html.escape(ast.title)}</title>
        {author_el}
      </titleStmt>
      <publicationStmt>
        <publisher>Paper Lanterns Archival Correspondence Library</publisher>
        <idno type="book_id">{html.escape(ast.book_id)}</idno>
        <availability status="free"><p>CC0 1.0 Public Domain</p></availability>
      </publicationStmt>
      <sourceDesc>
        <bibl>
          <title>{html.escape(ast.title)}</title>
          {date_el}
        </bibl>
      </sourceDesc>
    </fileDesc>{profile_block}
  </teiHeader>
  <text>
    <body>
      {body_xml}
    </body>
  </text>
</TEI>
"""
    ET.fromstring(tei_doc.encode("utf-8"))
    atomic_write_text(out_path, tei_doc)
    return {"tei_path": str(out_path), "corresp_count": len(ast.letters)}


def render_markdown_and_tei(ast: CollectionAST, edition_dir: str | Path) -> dict[str, Any]:
    """Render both Scholarly Markdown and Archival TEI P5 XML."""
    md_res = render_scholarly_markdown(ast, edition_dir)
    tei_res = render_tei_edition(ast, edition_dir)
    return {**md_res, **tei_res}
