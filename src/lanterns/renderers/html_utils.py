"""Safe XHTML/HTML5, Markdown, and TEI inline & block rendering primitives for Paper Lanterns v2.

ROLE: Pure structural projection of CollectionAST blocks, constrained inline tags, folio turns, and pre-linked footnotes.
INPUTS: BlockIR, FootnoteItem, and CollectionAST data structures from lanterns.core.models.
OUTPUTS: Strictly escaped, stack-balanced HTML5/XHTML/TEI/Markdown fragments and shared editorial CSS.
INVARIANT: Never inspects prose content words, never branches on book_id, never re-implements segmentation or footnote matching.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from lanterns.core.models import BlockIR, FootnoteItem
from lanterns.core.tags import split_tagged_lines_balanced

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

_INLINE_TOKEN_RE = re.compile(r"(</?(?:i|b|sc|fnref|sup|sub|unclear)>)")
_PLACEHOLDER_NOTE_RE = re.compile(r"\[\[NOTE:([^:\]]+):(?:(\d+):)?([^\]]+)\]\]")
_PLACEHOLDER_FOLIO_RE = re.compile(r"\[\[(?:FN)?FOLIO:(\d+):([^\]]*)\]\]")
_BRACKET_FOLIO_RE = re.compile(r"\[p\.\s*([^\]]+?)\]")
_NCNAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9_.-]+")

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

_LIGATURE_SEARCH_MAP: dict[str, str] = {
    "æ": "ae",
    "Æ": "ae",
    "œ": "oe",
    "Œ": "oe",
    "ſ": "s",
    "ß": "ss",
    "ø": "o",
    "Ø": "o",
    "ł": "l",
    "Ł": "l",
    "đ": "d",
    "Đ": "d",
}


def atomic_write_text(path: str | Path, content: str) -> Path:
    """Atomically write UTF-8 text via temporary file + os.replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp_path, target)
    except OSError:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return target


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    """Atomically write pretty-printed UTF-8 JSON."""
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(path, serialized)


def sanitize_xml_id(raw_id: str, prefix: str = "id") -> str:
    """Normalize an identifier into a valid XML/HTML NCName."""
    cleaned = _NCNAME_CLEAN_RE.sub("-", raw_id.strip()).strip("-.")
    if not cleaned:
        return prefix
    if not (cleaned[0].isalpha() or cleaned[0] == "_"):
        return f"{prefix}-{cleaned}"
    return cleaned


def fn_anchor_id(footnote_id: str) -> str:
    """Canonical DOM/XML target id (`#fn-...`) for a FootnoteItem."""
    raw = footnote_id.strip()
    if not raw.startswith("fn-"):
        raw = f"fn-{raw}"
    return sanitize_xml_id(raw, "fn")


def alloc_fnref_id(footnote_id: str, ref_registry: dict[str, list[str]]) -> str:
    """Allocate a globally unique `#fnref-...` callout anchor id and record it."""
    target = fn_anchor_id(footnote_id)
    base = target[3:] if target.startswith("fn-") else target
    existing = ref_registry.setdefault(footnote_id, [])
    occ = len(existing) + 1
    ref_id = sanitize_xml_id(f"fnref-{base}" if occ == 1 else f"fnref-{base}-{occ}", "fnref")
    existing.append(ref_id)
    return ref_id


def fold_diacritics_for_search(text: str) -> str:
    """Fold diacritics and historical ligatures for accent-insensitive search (`Rájput` -> `rajput`)."""
    expanded = "".join(_LIGATURE_SEARCH_MAP.get(ch, ch) for ch in text)
    decomposed = unicodedata.normalize("NFD", expanded)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped).casefold()


def tokenize_tagged_text(tagged_text: str) -> list[tuple[str, str]]:
    """Tokenize constrained inline tags with strict LIFO stack balancing."""
    tokens: list[tuple[str, str]] = []
    stack: list[str] = []
    for part in _INLINE_TOKEN_RE.split(tagged_text):
        if not part:
            continue
        if part.startswith("</") and part.endswith(">"):
            tag = part[2:-1]
            if tag in stack:
                idx = len(stack) - 1 - stack[::-1].index(tag)
                to_reopen = stack[idx + 1 :]
                for open_t in reversed(stack[idx:]):
                    tokens.append(("close", open_t))
                del stack[idx:]
                for reopen_t in to_reopen:
                    stack.append(reopen_t)
                    tokens.append(("open", reopen_t))
            else:
                tokens.append(("text", part))
        elif part.startswith("<") and part.endswith(">"):
            tag = part[1:-1]
            stack.append(tag)
            tokens.append(("open", tag))
        else:
            tokens.append(("text", part))
    while stack:
        tokens.append(("close", stack.pop()))
    return tokens


def strip_constrained_tags(tagged_text: str) -> str:
    """Return plain diplomatic text with constrained inline tags removed."""
    return "".join(val for kind, val in tokenize_tagged_text(tagged_text) if kind == "text")


def _normalize_note_label(raw: str) -> str:
    cleaned = raw.strip().strip("[]()")
    if cleaned.endswith(".") and len(cleaned) > 1 and cleaned[:-1].isalnum():
        cleaned = cleaned[:-1]
    return cleaned.strip()


def _build_callout_queue(block_footnotes: list[FootnoteItem] | None) -> dict[str, list[FootnoteItem]]:
    queue: dict[str, list[FootnoteItem]] = {}
    for fn in block_footnotes or []:
        key = _normalize_note_label(fn.label)
        queue.setdefault(key, []).append(fn)
    return queue


def _format_fnref_html(fn_id: str, label_text: str, ref_registry: dict[str, list[str]], *, epub: bool) -> str:
    target_id = html.escape(fn_anchor_id(fn_id), quote=True)
    ref_id = html.escape(alloc_fnref_id(fn_id, ref_registry), quote=True)
    lbl_esc = html.escape(label_text, quote=True)
    if epub:
        return f'<sup class="fn-ref"><a id="{ref_id}" epub:type="noteref" href="#{target_id}">{lbl_esc}</a></sup>'
    return f'<sup class="fn-ref"><a id="{ref_id}" href="#{target_id}" role="doc-noteref">{lbl_esc}</a></sup>'


def _format_folio_html(
    page_number: int | None,
    folio_label: str,
    *,
    seen_page_ids: set[int] | None,
    proof_href_prefix: str | None,
    epub: bool,
) -> str:
    fol_esc = html.escape(folio_label.strip() or str(page_number or ""), quote=True)
    id_attr = ""
    if page_number is not None and seen_page_ids is not None and page_number not in seen_page_ids:
        seen_page_ids.add(page_number)
        id_attr = f' id="page-{page_number}"'
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
    if cascading and "\n" in tagged_text:
        lines = [ln for ln in split_tagged_lines_balanced(tagged_text) if ln.strip()]
        rendered_lines = [
            f'<span class="cascade-line" style="--cascade-step: {step};">'
            + _render_single_line_html(
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
            + "</span>"
            for step, ln in enumerate(lines)
        ]
        return "<br />\n".join(rendered_lines)

    if preserve_line_breaks and "\n" in tagged_text:
        lines = split_tagged_lines_balanced(tagged_text)
        return "<br />\n".join(
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
        )

    return _render_single_line_html(
        tagged_text,
        callout_queue=queue,
        available_footnotes=available_footnotes,
        ref_registry=registry,
        seen_page_ids=seen_page_ids,
        folio_to_page=folio_to_page,
        default_page=default_page,
        proof_href_prefix=proof_href_prefix,
        epub=epub,
    )


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

    head_rows: list[str] = []
    body_rows: list[str] = []
    for r_blk in row_blocks:
        is_hdr = r_blk.table_role == "header"
        cell_tag = "th" if is_hdr else "td"
        cells = r_blk.cells or []
        if not cells:
            raw_parts = [p.strip() for p in r_blk.tagged_text.split("|")]
            rendered_cells = [
                f"<{cell_tag}>"
                + render_inline_html(
                    part,
                    block_footnotes=fn_by_block.get(r_blk.block_id),
                    available_footnotes=available_footnotes,
                    ref_registry=ref_registry,
                    seen_page_ids=seen_page_ids,
                    folio_to_page=folio_to_page,
                    default_page=r_blk.page_number,
                    proof_href_prefix=proof_href_prefix,
                    epub=epub,
                )
                + f"</{cell_tag}>"
                for part in raw_parts
            ]
        else:
            rendered_cells = []
            for cell in cells:
                cs = f' colspan="{cell.colspan}"' if cell.colspan > 1 else ""
                rs = f' rowspan="{cell.rowspan}"' if cell.rowspan > 1 else ""
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
                rendered_cells.append(f"<{cell_tag}{cs}{rs}>{c_inner}</{cell_tag}>")
        row_html = f"<tr>{''.join(rendered_cells)}</tr>"
        if is_hdr:
            head_rows.append(row_html)
        else:
            body_rows.append(row_html)

    thead = f"<thead>{''.join(head_rows)}</thead>" if head_rows else ""
    tbody = f"<tbody>{''.join(body_rows or head_rows)}</tbody>" if (body_rows or not head_rows) else "<tbody></tbody>"
    return f'<div class="archival-table-wrap"><table class="archival-table">{cap_html}{thead}{tbody}</table></div>'


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
    heading_base: int = 3,
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
        glyph = inner.strip() or "* * *"
        return f'<div id="{bid}" class="{cls}" role="separator">{glyph}</div>'
    if block.type == "heading":
        lvl = min(6, max(2, heading_base + max(0, (block.heading_level or 2) - 1)))
        return f'<h{lvl} id="{bid}" class="{cls}">{inner}</h{lvl}>'
    if block.type == "letter_header":
        return f'<h{heading_base} id="{bid}" class="{cls}">{inner}</h{heading_base}>'
    if block.type in {"blockquote", "epigraph"}:
        return f'<blockquote id="{bid}" class="{cls}">{inner}</blockquote>'
    if block.type == "margin_note":
        side_cls = "margin-right" if (block.bbox and block.bbox[0] >= 500) else "margin-left"
        return f'<aside id="{bid}" class="{cls} {side_cls}" role="note">{inner}</aside>'
    if block.type in {"illustration", "facsimile_plate"}:
        label = "Facsimile plate" if block.type == "facsimile_plate" else "Illustration"
        desc = inner or html.escape(f"[{label} on p. {block.printed_folio or block.page_number}]")
        return f'<figure id="{bid}" class="{cls}"><figcaption>{desc}</figcaption></figure>'
    return f'<p id="{bid}" class="{cls}">{inner}</p>'


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
        acc_fns = list(fn_by_block.get(acc.block_id, []))
        seen_fids = {fn.footnote_id for fn in acc_fns}
        deferred_floats: list[BlockIR] = []
        idx += 1
        while acc.type in _COALESCE_TYPES and acc.continues_to_next:
            k = _find_continuation_index(visible, idx, acc.type)
            if k is None:
                break
            deferred_floats.extend(visible[idx:k])
            nxt = visible[k]
            rhs = (
                _upgrade_leading_folio_token(nxt)
                if (include_folio_tokens and nxt.page_number > acc.page_number)
                else nxt.tagged_text.lstrip()
            )
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
    heading_base: int = 3,
    leading_section_title: str = "",
) -> str:
    """Render an ordered sequence of `BlockIR`s, preserving page anchors, tables, lists, and two-column pairs."""
    blocks, fn_by_block = coalesce_continued_blocks(blocks, fn_by_block)
    skip_n = count_redundant_leading_headings(blocks, leading_section_title)
    out: list[str] = []
    if skip_n > 0 and blocks[0].page_number not in seen_page_ids:
        fol_label = blocks[0].printed_folio or str(blocks[0].page_number)
        out.append(
            '<div class="page-locus">'
            + _format_folio_html(
                blocks[0].page_number,
                fol_label,
                seen_page_ids=seen_page_ids,
                proof_href_prefix=proof_href_prefix,
                epub=epub,
            )
            + "</div>"
        )
    idx = skip_n
    while idx < len(blocks):
        blk = blocks[idx]
        if blk.type in {"running_header", "folio", "scanner_residue", "noise", "footnote"}:
            idx += 1
            continue
        if blk.page_number not in seen_page_ids:
            fol_label = blk.printed_folio or str(blk.page_number)
            out.append(
                '<div class="page-locus">'
                + _format_folio_html(
                    blk.page_number,
                    fol_label,
                    seen_page_ids=seen_page_ids,
                    proof_href_prefix=proof_href_prefix,
                    epub=epub,
                )
                + "</div>"
            )
        if blk.type == "list_item":
            items_html: list[str] = []
            while idx < len(blocks) and blocks[idx].type == "list_item":
                cur = blocks[idx]
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
                items_html.append(f'<li id="{html.escape(sanitize_xml_id(cur.block_id, "blk"))}">{li_inner}</li>')
                idx += 1
            out.append(f'<ul class="archival-list">{"".join(items_html)}</ul>')
            continue

        if blk.type in {"table_caption", "table_row"}:
            cap_blk = blk if blk.type == "table_caption" else None
            if cap_blk is not None:
                idx += 1
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
            )
            out.append(f'<div class="two-col-pair">{left_html}{right_html}</div>')
            idx += 2
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
    """Render bidirectional `#fn-...` <-> `#fnref-...` footnote apparatus."""
    items_out: list[str] = []
    seen_fn: set[str] = set()
    for fid in footnote_ids:
        if fid in seen_fn or fid not in footnotes_map:
            continue
        seen_fn.add(fid)
        fn = footnotes_map[fid]
        dom_id = html.escape(fn_anchor_id(fn.footnote_id), quote=True)
        lbl_esc = html.escape(fn.label or "*", quote=False)
        orphan_badge = ""
        if fn.is_orphan:
            fol = html.escape(fn.origin_folio or str(fn.origin_page))
            orphan_badge = f'<span class="fn-orphan-tag">[Unlinked note, p. {fol}]</span> '
        body_html = render_inline_html(
            fn.tagged_text,
            available_footnotes=footnotes_map,
            ref_registry=ref_registry,
            seen_page_ids=seen_page_ids,
            folio_to_page=folio_to_page,
            default_page=fn.origin_page,
            proof_href_prefix=proof_href_prefix,
            epub=epub,
        )
        refs = ref_registry.get(fn.footnote_id) or []
        backlink = ""
        if refs:
            first_ref = html.escape(refs[0], quote=True)
            backlink = f' <a class="fn-backlink" href="#{first_ref}" aria-label="Return to text">↩</a>'
        if epub:
            items_out.append(
                f'<aside id="{dom_id}" epub:type="footnote" class="fn-item">'
                f'<p><span class="fn-label">{lbl_esc}.</span> {orphan_badge}{body_html}{backlink}</p></aside>'
            )
        else:
            items_out.append(
                f'<li id="{dom_id}" class="fn-item" role="doc-footnote">'
                f'<span class="fn-label">{lbl_esc}.</span> '
                f'<span class="fn-body">{orphan_badge}{body_html}{backlink}</span></li>'
            )
    if not items_out:
        return ""
    h_esc = html.escape(heading_label)
    if epub:
        return f'<section epub:type="footnotes" class="apparatus-notes"><h3>{h_esc}</h3>{"".join(items_out)}</section>'
    return (
        f'<section class="apparatus-notes" role="doc-endnotes">'
        f'<h3 class="apparatus-heading">{h_esc}</h3>'
        f'<ol class="fn-list">{"".join(items_out)}</ol></section>'
    )


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
.block-salutation {
  font-style: italic;
  margin: 0.6rem 0 0.75rem;
}
.block-body { text-align: justify; hyphens: auto; }
.block-blockquote, .block-epigraph {
  margin: 1.15rem 1.8rem;
  padding-left: 1rem;
  border-left: 2px solid var(--rule-hairline);
  font-size: 1.06rem;
}
.block-verse {
  margin: 1.1rem 2rem;
  font-style: italic;
  line-height: 1.62;
}
.block-valediction { margin: 0.9rem 0 0.35rem; }
.block-signature {
  font-variant: small-caps;
  letter-spacing: 0.05em;
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
  margin: 1.25rem 0;
  padding: 0.75rem 1.1rem;
  background: var(--paper-elevated);
  border-left: 3px solid var(--accent-oxblood);
  color: var(--ink-secondary);
  font-size: 1.05rem;
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
