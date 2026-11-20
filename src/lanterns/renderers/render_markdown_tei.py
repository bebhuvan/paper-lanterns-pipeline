"""Scholarly Markdown (`markdown/<book_id>.md`) and Archival TEI P5 XML (`tei/<book_id>.tei.xml`) renderers.

ROLE: Export CollectionAST into single-volume Markdown and correspDesc-compliant TEI P5 XML.
INPUTS: CollectionAST from lanterns.core.models.
OUTPUTS: markdown/<book_id>.md and tei/<book_id>.tei.xml (verified well-formed with unique xml:ids).
INVARIANT: Every LetterUnit emits <correspDesc xml:id="letter-..."> in <profileDesc> and <div type="letter" corresp="#letter-..."> in <body>.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from lanterns.core.models import BlockIR, CollectionAST, FootnoteItem, LetterUnit
from lanterns.core.tags import split_tagged_lines_balanced
from lanterns.renderers.html_utils import (
    TEI_TAG_CLOSE,
    TEI_TAG_OPEN,
    alloc_fnref_id,
    atomic_write_text,
    coalesce_continued_blocks,
    count_redundant_leading_headings,
    fn_anchor_id,
    footnotes_by_block_map,
    render_inline_markdown,
    sanitize_xml_id,
    tokenize_tagged_text,
)

_TEI_TEXT_MARKER_RE = re.compile(
    r"\[\[NOTE:([^:\]]+):(?:(\d+):)?([^\]]+)\]\]"
    r"|\[\[(?:FN)?FOLIO:(\d+):([^\]]*)\]\]"
    r"|\[p\.\s*([^\]]+?)\]"
)
_MD_LINE_PREFIX = {
    "heading": "### ",
    "letter_header": "### ",
    "blockquote": "> ",
    "epigraph": "> ",
}
_TEI_BLOCK_WRAPS: dict[str, tuple[str, str]] = {
    "heading": ("", "head"),
    "letter_header": ("", "head"),
    "salutation": ("opener", "salute"),
    "valediction": ("closer", "salute"),
    "signature": ("closer", "signed"),
    "postscript": ("postscript", "p"),
    "blockquote": ("quote", "p"),
    "epigraph": ("quote", "p"),
    "address_block": ("opener", "address><addrLine"),
    "verse": ("lg", "l"),
}


def corresp_xml_id(letter_id: str) -> str:
    """Return canonical `<correspDesc xml:id="letter-...">` identifier."""
    raw = letter_id.strip()
    return sanitize_xml_id(raw if raw.startswith("letter-") else f"letter-{raw}", "letter")


_md_inline = render_inline_markdown


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
        if blk.type in {"running_header", "folio", "scanner_residue", "noise", "footnote"}:
            idx += 1
            continue
        if blk.type in {"table_caption", "table_row"}:
            if blk.type == "table_caption":
                lines.append(f"**{_md_inline(blk.tagged_text, fn_by_block.get(blk.block_id))}**\n")
                idx += 1
            rows: list[list[str]] = []
            while idx < len(blocks) and blocks[idx].type == "table_row":
                rblk = blocks[idx]
                raw_parts = [c.text for c in (rblk.cells or ())] or rblk.tagged_text.split("|")
                rows.append([_md_inline(p, fn_by_block.get(rblk.block_id)).replace("\n", " ").strip() for p in raw_parts])
                idx += 1
            if rows:
                width = max(len(r) for r in rows)
                padded = [r + [""] * (width - len(r)) for r in rows]
                lines.extend(["| " + " | ".join(padded[0]) + " |", "| " + " | ".join("---" for _ in range(width)) + " |"])
                lines.extend("| " + " | ".join(r) + " |" for r in padded[1:])
                lines.append("")
            continue

        txt = _md_inline(blk.tagged_text, fn_by_block.get(blk.block_id))
        if blk.type == "list_item":
            lines.append(f"- {txt}")
        elif blk.type == "ornamental_break":
            lines.append("* * *\n")
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

    letters_by_id = {ltr.letter_id: ltr for ltr in ast.letters}
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
        sec_fn_ids: list[str] = list(sec.footnote_ids)
        for item_idx, item in enumerate(sec.items):
            if item.kind == "letter" and item.letter_id in letters_by_id:
                ltr = letters_by_id[item.letter_id]
                corr = " → ".join(p for p in (ltr.metadata.sender_display, ltr.metadata.recipient_display) if p)
                lines.append(f"### Letter {ltr.sequence_index}{': ' + corr if corr else ''}\n")
                lines.extend(_md_block_lines(ltr.blocks, fn_by_block))
                sec_fn_ids = list(dict.fromkeys([*sec_fn_ids, *ltr.footnote_ids]))
            elif item.blocks:
                lines.extend(
                    _md_block_lines(
                        item.blocks,
                        fn_by_block,
                        leading_section_title=sec.title_plain if item_idx == 0 else "",
                    )
                )

        if sec_fn_ids:
            lines.append("### Notes\n")
            for fid in sec_fn_ids:
                if fid in ast.footnotes:
                    fn = ast.footnotes[fid]
                    orphan_prefix = f"[Unlinked note, p. {fn.origin_folio or fn.origin_page}] " if fn.is_orphan else ""
                    lines.append(f"[^{fn.footnote_id}]: ({fn.label}) {orphan_prefix}{_md_inline(fn.tagged_text)}")
            lines.append("")

    atomic_write_text(out_path, "\n".join(lines).rstrip() + "\n")
    return {"markdown_path": str(out_path)}


def _tei_escape_chunk(
    chunk: str,
    *,
    available_footnotes: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    seen_pb_pages: set[int],
) -> str:
    out: list[str] = []
    last = 0
    for match in _TEI_TEXT_MARKER_RE.finditer(chunk):
        if match.start() > last:
            out.append(html.escape(chunk[last : match.start()], quote=False))
        last = match.end()
        if match.group(1) is not None:
            fn_id, lbl = match.group(1), match.group(3)
            if fn_id in available_footnotes:
                tid = html.escape(fn_anchor_id(fn_id), quote=True)
                rid = html.escape(alloc_fnref_id(fn_id, ref_registry), quote=True)
                out.append(f'<ref xml:id="{rid}" type="noteref" target="#{tid}">{html.escape(lbl, quote=False)}</ref>')
            else:
                out.append(f'<hi rend="sup">{html.escape(lbl, quote=False)}</hi>')
        elif match.group(4) is not None:
            pnum = int(match.group(4))
            fol = html.escape(match.group(5) or str(pnum), quote=True)
            id_attr = ""
            if pnum not in seen_pb_pages:
                seen_pb_pages.add(pnum)
                id_attr = f' xml:id="page-{pnum}"'
            out.append(f'<pb{id_attr} n="{fol}"/>')
        else:
            fol = html.escape(match.group(6).strip(), quote=True)
            out.append(f'<pb n="{fol}"/>')
    if last < len(chunk):
        out.append(html.escape(chunk[last:], quote=False))
    return "".join(out)


def _render_tei_inline(
    tagged_text: str,
    *,
    block_footnotes: list[FootnoteItem] | None,
    available_footnotes: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    seen_pb_pages: set[int],
    preserve_lb: bool = False,
) -> str:
    queue: dict[str, list[FootnoteItem]] = {}
    for fn in block_footnotes or []:
        queue.setdefault(fn.label.strip().strip("[]()"), []).append(fn)

    def _one_line(line_txt: str) -> str:
        tokens = tokenize_tagged_text(line_txt)
        parts: list[str] = []
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
                sup_raw = tokens[idx + 1][1]
                key = sup_raw.strip().strip("[]()")
                bucket = queue.get(key)
                if bucket:
                    fn = bucket.pop(0)
                    tid = html.escape(fn_anchor_id(fn.footnote_id), quote=True)
                    rid = html.escape(alloc_fnref_id(fn.footnote_id, ref_registry), quote=True)
                    parts.append(
                        f'<ref xml:id="{rid}" type="noteref" target="#{tid}">{html.escape(sup_raw, quote=False)}</ref>'
                    )
                    idx += 3
                    continue
            if kind == "open":
                parts.append(TEI_TAG_OPEN[val])
            elif kind == "close":
                parts.append(TEI_TAG_CLOSE[val])
            else:
                parts.append(
                    _tei_escape_chunk(
                        val,
                        available_footnotes=available_footnotes,
                        ref_registry=ref_registry,
                        seen_pb_pages=seen_pb_pages,
                    )
                )
            idx += 1
        return "".join(parts)

    if preserve_lb and "\n" in tagged_text:
        return "<lb/>".join(_one_line(ln) for ln in split_tagged_lines_balanced(tagged_text))
    return _one_line(tagged_text)


def _tei_pb_if_needed(blk: BlockIR, seen_pb_pages: set[int], used_ids: set[str]) -> str:
    if blk.page_number not in seen_pb_pages:
        seen_pb_pages.add(blk.page_number)
        page_id = f"page-{blk.page_number}"
        used_ids.add(page_id)
        fol_esc = html.escape(blk.printed_folio or str(blk.page_number), quote=True)
        return f'<pb xml:id="{page_id}" n="{fol_esc}"/>\n'
    return ""


def _render_tei_block(
    blk: BlockIR,
    *,
    fn_by_block: dict[str, list[FootnoteItem]],
    available_footnotes: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    seen_pb_pages: set[int],
    used_ids: set[str],
    seen_prose_before_dateline: bool = False,
) -> str:
    pb_prefix = _tei_pb_if_needed(blk, seen_pb_pages, used_ids)

    preserve_lb = blk.line_breaks_preserved or blk.type in {
        "verse",
        "dateline",
        "salutation",
        "valediction",
        "signature",
        "address_block",
    }
    inner = _render_tei_inline(
        blk.tagged_text,
        block_footnotes=fn_by_block.get(blk.block_id),
        available_footnotes=available_footnotes,
        ref_registry=ref_registry,
        seen_pb_pages=seen_pb_pages,
        preserve_lb=preserve_lb,
    )
    rend = f' rend="{html.escape(blk.alignment, quote=True)}"' if blk.alignment != "left" else ""
    if blk.type == "ornamental_break":
        return f'{pb_prefix}<milestone unit="section" rend="ornament"/>'
    if blk.type == "margin_note":
        return f'{pb_prefix}<note place="margin">{inner}</note>'
    if blk.type == "dateline":
        wrap = "closer" if seen_prose_before_dateline else "opener"
        return f"{pb_prefix}<{wrap}><dateline{rend}>{inner}</dateline></{wrap}>"
    outer, inner_tag = _TEI_BLOCK_WRAPS.get(blk.type, ("", "p"))
    core_el = f"<{inner_tag}{rend if blk.type in {'heading', 'letter_header', 'salutation', 'valediction', 'signature', 'postscript', 'blockquote', 'epigraph', 'body'} or not outer else ''}>{inner}</{inner_tag.split('><')[0]}>"
    if blk.type == "address_block":
        return f"{pb_prefix}<opener><address><addrLine>{inner}</addrLine></address></opener>"
    return f"{pb_prefix}<{outer}>{core_el}</{outer}>" if outer else f"{pb_prefix}{core_el}"


def _render_tei_block_stream(
    blocks: list[BlockIR],
    *,
    fn_by_block: dict[str, list[FootnoteItem]],
    available_footnotes: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    seen_pb_pages: set[int],
    used_ids: set[str],
) -> str:
    blocks, fn_by_block = coalesce_continued_blocks(blocks, fn_by_block)
    out: list[str] = []
    seen_prose = False
    idx = 0
    while idx < len(blocks):
        blk = blocks[idx]
        if blk.type in {"running_header", "folio", "scanner_residue", "noise", "footnote"}:
            idx += 1
            continue
        if blk.type == "list_item":
            list_pb = _tei_pb_if_needed(blk, seen_pb_pages, used_ids)
            items: list[str] = []
            while idx < len(blocks) and blocks[idx].type == "list_item":
                cur = blocks[idx]
                item_pb = _tei_pb_if_needed(cur, seen_pb_pages, used_ids)
                items.append(
                    item_pb
                    + "<item>"
                    + _render_tei_inline(
                        cur.tagged_text,
                        block_footnotes=fn_by_block.get(cur.block_id),
                        available_footnotes=available_footnotes,
                        ref_registry=ref_registry,
                        seen_pb_pages=seen_pb_pages,
                    )
                    + "</item>"
                )
                idx += 1
            out.append(list_pb + "<list>" + "".join(items) + "</list>")
            seen_prose = True
            continue
        if blk.type in {"table_caption", "table_row"}:
            tbl_pb = _tei_pb_if_needed(blk, seen_pb_pages, used_ids)
            head_xml = ""
            if blk.type == "table_caption":
                head_xml = "<head>" + _render_tei_inline(
                    blk.tagged_text,
                    block_footnotes=fn_by_block.get(blk.block_id),
                    available_footnotes=available_footnotes,
                    ref_registry=ref_registry,
                    seen_pb_pages=seen_pb_pages,
                ) + "</head>"
                idx += 1
            rows_xml: list[str] = []
            while idx < len(blocks) and blocks[idx].type == "table_row":
                rblk = blocks[idx]
                row_pb = _tei_pb_if_needed(rblk, seen_pb_pages, used_ids)
                cell_texts = [c.text for c in (rblk.cells or ())] or [p.strip() for p in rblk.tagged_text.split("|")]
                c_xml = [
                    "<cell>"
                    + _render_tei_inline(
                        cell_txt,
                        block_footnotes=fn_by_block.get(rblk.block_id),
                        available_footnotes=available_footnotes,
                        ref_registry=ref_registry,
                        seen_pb_pages=seen_pb_pages,
                    )
                    + "</cell>"
                    for cell_txt in cell_texts
                ]
                rows_xml.append(row_pb + "<row>" + "".join(c_xml) + "</row>")
                idx += 1
            out.append(f"{tbl_pb}<table>{head_xml}{''.join(rows_xml)}</table>")
            seen_prose = True
            continue

        out.append(
            _render_tei_block(
                blk,
                fn_by_block=fn_by_block,
                available_footnotes=available_footnotes,
                ref_registry=ref_registry,
                seen_pb_pages=seen_pb_pages,
                used_ids=used_ids,
                seen_prose_before_dateline=seen_prose,
            )
        )
        seen_prose = (seen_prose and blk.type != "letter_header") or blk.type in {
            "body",
            "valediction",
            "signature",
            "postscript",
        }
        idx += 1
    return "\n".join(out)


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


def _build_corresp_descriptions(letters: list[LetterUnit], used_ids: set[str]) -> tuple[str, dict[str, str]]:
    id_map: dict[str, str] = {}
    descs: list[str] = []
    for ltr in letters:
        cid = _unique_xml_id(corresp_xml_id(ltr.letter_id), used_ids)
        id_map[ltr.letter_id] = cid
        sender = html.escape(ltr.metadata.sender_display or "Unknown")
        recipient = html.escape(ltr.metadata.recipient_display or "Unknown")
        date_el = (
            f"<date>{html.escape(ltr.metadata.dateline_plain)}</date>" if ltr.metadata.dateline_plain else ""
        )
        descs.append(
            f'      <correspDesc xml:id="{html.escape(cid, quote=True)}">\n'
            f'        <correspAction type="sent"><persName>{sender}</persName>{date_el}</correspAction>\n'
            f'        <correspAction type="received"><persName>{recipient}</persName></correspAction>\n'
            f"      </correspDesc>"
        )
    return "\n".join(descs), id_map


def render_tei_edition(ast: CollectionAST, edition_dir: str | Path) -> dict[str, Any]:
    """Render `CollectionAST` into well-formed TEI P5 XML at `<edition_dir>/tei/<book_id>.tei.xml`."""
    tei_dir = Path(edition_dir) / "tei"
    tei_dir.mkdir(parents=True, exist_ok=True)
    out_path = tei_dir / f"{ast.book_id}.tei.xml"

    used_ids: set[str] = set()
    corresp_xml, corresp_id_by_letter = _build_corresp_descriptions(ast.letters, used_ids)

    letters_by_id = {ltr.letter_id: ltr for ltr in ast.letters}
    fn_by_block = footnotes_by_block_map(ast.footnotes)
    ref_registry: dict[str, list[str]] = {}
    seen_pb_pages: set[int] = set()
    emitted_fn_ids: set[str] = set()

    section_divs: list[str] = []
    for sec in ast.sections:
        sec_xml_id = _unique_xml_id(f"sec-{sec.section_id}", used_ids)
        sec_children: list[str] = [f"<head>{html.escape(sec.title_plain)}</head>"]

        for upage in sorted(sec.unavailable_pages):
            gap_id = _unique_xml_id(f"page-{upage}", used_ids)
            seen_pb_pages.add(upage)
            sec_children.append(
                f'<pb xml:id="{gap_id}" n="{upage}"/><gap reason="unavailable" unit="page" quantity="1"/>'
            )

        sec_fn_order: list[str] = list(sec.footnote_ids)
        for item in sec.items:
            if item.kind == "letter" and item.letter_id in letters_by_id:
                ltr = letters_by_id[item.letter_id]
                cid = corresp_id_by_letter[ltr.letter_id]
                div_id = _unique_xml_id(f"div-{cid}", used_ids)
                ltr_inner = _render_tei_block_stream(
                    ltr.blocks,
                    fn_by_block=fn_by_block,
                    available_footnotes=ast.footnotes,
                    ref_registry=ref_registry,
                    seen_pb_pages=seen_pb_pages,
                    used_ids=used_ids,
                )
                sec_children.append(
                    f'<div xml:id="{html.escape(div_id, quote=True)}" type="letter" corresp="#{html.escape(cid, quote=True)}">\n'
                    f"{ltr_inner}\n</div>"
                )
                sec_fn_order = list(dict.fromkeys([*sec_fn_order, *ltr.footnote_ids]))
            elif item.blocks:
                sec_children.append(
                    _render_tei_block_stream(
                        item.blocks,
                        fn_by_block=fn_by_block,
                        available_footnotes=ast.footnotes,
                        ref_registry=ref_registry,
                        seen_pb_pages=seen_pb_pages,
                        used_ids=used_ids,
                    )
                )

        for fid in sec_fn_order:
            if fid in emitted_fn_ids or fid not in ast.footnotes:
                continue
            emitted_fn_ids.add(fid)
            fn = ast.footnotes[fid]
            nid = _unique_xml_id(fn_anchor_id(fn.footnote_id), used_ids)
            fn_inner = _render_tei_inline(
                fn.tagged_text,
                block_footnotes=None,
                available_footnotes=ast.footnotes,
                ref_registry=ref_registry,
                seen_pb_pages=seen_pb_pages,
            )
            n_attr = html.escape(fn.label or "*", quote=True)
            sec_children.append(f'<note place="foot" xml:id="{html.escape(nid, quote=True)}" n="{n_attr}">{fn_inner}</note>')

        section_divs.append(
            f'<div xml:id="{html.escape(sec_xml_id, quote=True)}" type="{html.escape(sec.kind, quote=True)}">\n'
            + "\n".join(c for c in sec_children if c)
            + "\n</div>"
        )

    for fid, fn in ast.footnotes.items():
        if fid not in emitted_fn_ids:
            nid = _unique_xml_id(fn_anchor_id(fn.footnote_id), used_ids)
            fn_inner = _render_tei_inline(
                fn.tagged_text,
                block_footnotes=None,
                available_footnotes=ast.footnotes,
                ref_registry=ref_registry,
                seen_pb_pages=seen_pb_pages,
            )
            section_divs.append(
                f'<div type="apparatus"><note place="foot" xml:id="{html.escape(nid, quote=True)}" '
                f'n="{html.escape(fn.label or "*", quote=True)}">{fn_inner}</note></div>'
            )

    body_xml = "\n      ".join(section_divs) or '<div><p>Empty edition</p></div>'
    author_el = f"<author>{html.escape(ast.author_or_editor or 'Public Domain')}</author>"
    date_el = f"<date>{ast.year}</date>" if ast.year else "<date>Public Domain</date>"
    profile_block = f"\n    <profileDesc>\n{corresp_xml}\n    </profileDesc>" if corresp_xml else "\n    <profileDesc/>"

    tei_doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0" xml:lang="en">
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
