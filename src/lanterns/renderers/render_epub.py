"""Semantic EPUB 3.3 renderer (`epub/<book_id>.epub`).

ROLE: Package CollectionAST into a standards-compliant EPUB 3.3 archive with pop-up footnotes and dual TOCs.
INPUTS: CollectionAST from lanterns.core.models.
OUTPUTS: epub/<book_id>.epub (mimetype stored first, META-INF/container.xml, OEBPS/{content.opf,nav.xhtml,styles.css,sec-*.xhtml}).
INVARIANT: Every XHTML and OPF payload passes strict XML parsing before archive creation; zero self-closing <span/>.
"""

from __future__ import annotations

import html
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lanterns.core.models import BlockIR, BookSection, CollectionAST
from lanterns.renderers.html_utils import (
    EDITORIAL_EPUB_CSS,
    collect_section_footnotes,
    footnotes_by_block_map,
    html_lang_dir_attrs,
    index_ast_blocks,
    iter_section_render_units,
    render_block_stream_html,
    render_footnotes_apparatus_html,
    render_inline_html,
    render_unavailable_page_banner,
    sanitize_xml_id,
)
from lanterns.renderers.render_book_web import (
    build_folio_to_page_map,
    letter_dom_anchor,
)

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_BODY_MATTER_BY_KIND: dict[str, str] = {
    "front_matter": "frontmatter",
    "toc": "frontmatter",
    "appendix": "backmatter",
    "endnotes": "backmatter",
    "index": "backmatter",
}
_SECTION_EPUB_TYPE_BY_KIND: dict[str, str] = {
    "epistolary_part": "part",
    "appendix": "appendix",
    "index": "index",
}


def epub_section_filename(section_id: str) -> str:
    """Return `sec-<sanitized_id>.xhtml` inside `OEBPS/`."""
    stem = sanitize_xml_id(section_id, "sec")
    return f"{stem}.xhtml" if stem.startswith("sec-") else f"sec-{stem}.xhtml"


_collect_section_footnotes = collect_section_footnotes


def _extract_dom_page_entries(
    root: ET.Element,
    before_pages: set[int],
    folio_by_page: dict[int, str],
) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    for elem in root.iter():
        eid = elem.attrib.get("id", "")
        if eid.startswith("page-") and eid[5:].isdigit():
            pnum = int(eid[5:])
            if pnum not in before_pages:
                entries.append((pnum, folio_by_page.get(pnum, str(pnum))))
    return entries


def _build_section_xhtml(
    ast: CollectionAST,
    section: BookSection,
    *,
    block_lookup: dict[str, BlockIR],
    global_seen_pages: set[int],
    folio_to_page: dict[str, int],
) -> tuple[str, list[tuple[int, str]]]:
    ordered_fn_ids, sec_footnotes = _collect_section_footnotes(section, ast)
    fn_by_block = footnotes_by_block_map(sec_footnotes)
    ref_registry: dict[str, list[str]] = {}
    before_pages = set(global_seen_pages)

    parts: list[str] = []
    unavail_sorted = sorted(p for p in section.unavailable_pages if p not in global_seen_pages)
    unavail_idx = 0
    render_units = iter_section_render_units(section, ast, block_lookup)

    for unit_idx, (u_kind, blks, ltr, u_obj) in enumerate(render_units):
        first_page = blks[0].page_number if blks else section.end_page + 1
        while unavail_idx < len(unavail_sorted) and unavail_sorted[unavail_idx] <= first_page:
            upage = unavail_sorted[unavail_idx]
            global_seen_pages.add(upage)
            parts.append(render_unavailable_page_banner(upage, epub=True))
            unavail_idx += 1

        if ltr is not None:
            dom_id = html.escape(letter_dom_anchor(ltr.letter_id), quote=True)
            inner = render_block_stream_html(
                blks,
                fn_by_block=fn_by_block,
                available_footnotes=sec_footnotes,
                ref_registry=ref_registry,
                seen_page_ids=global_seen_pages,
                folio_to_page=folio_to_page,
                epub=True,
                heading_base=2,
                children=ltr.children,
            )
            parts.append(f'<article id="{dom_id}" class="letter-unit">{inner}</article>')
        elif blks:
            inner = render_block_stream_html(
                blks,
                fn_by_block=fn_by_block,
                available_footnotes=sec_footnotes,
                ref_registry=ref_registry,
                seen_page_ids=global_seen_pages,
                folio_to_page=folio_to_page,
                epub=True,
                heading_base=2,
                leading_section_title=(unit_idx == 0 and section.title_plain) or "",
                children=(u_obj and u_obj.children) or None,
            )
            if u_obj is not None:
                u_id = html.escape(sanitize_xml_id(u_obj.unit_id, "unit"), quote=True)
                parts.append(f'<div id="{u_id}" class="structural-unit unit-{u_kind.replace("_", "-")}">{inner}</div>')
            else:
                parts.append(inner)

    while unavail_idx < len(unavail_sorted):
        upage = unavail_sorted[unavail_idx]
        global_seen_pages.add(upage)
        parts.append(render_unavailable_page_banner(upage, epub=True))
        unavail_idx += 1

    apparatus = render_footnotes_apparatus_html(
        ordered_fn_ids,
        sec_footnotes,
        ref_registry,
        seen_page_ids=global_seen_pages,
        folio_to_page=folio_to_page,
        epub=True,
        heading_label="Notes",
    )

    folio_by_page = {p.page_number: (p.printed_folio or str(p.page_number)) for p in ast.pages}
    sec_title = render_inline_html(section.title_tagged or section.title_plain, epub=True)
    sec_dom_id = html.escape(sanitize_xml_id(section.section_id, "sec"), quote=True)
    lang_attrs = html_lang_dir_attrs(ast, xhtml=True)
    body_matter = _BODY_MATTER_BY_KIND.get(section.kind, "bodymatter")
    sec_epub_type = _SECTION_EPUB_TYPE_BY_KIND.get(section.kind, "chapter")

    xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" {lang_attrs}>
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(section.title_plain)}</title>
  <link rel="stylesheet" type="text/css" href="styles.css"/>
</head>
<body epub:type="{body_matter}">
  <section id="{sec_dom_id}" epub:type="{sec_epub_type}">
    <h1>{sec_title}</h1>
    {"".join(parts)}
    {apparatus}
  </section>
</body>
</html>
"""
    root = ET.fromstring(xhtml.encode("utf-8"))
    sec_page_entries = _extract_dom_page_entries(root, before_pages, folio_by_page)
    return xhtml, sec_page_entries


def _build_nav_xhtml(
    ast: CollectionAST,
    sec_href_by_id: dict[str, str],
    page_list: list[tuple[str, int, str]],
) -> str:
    chap_items = [
        f'<li><a href="{html.escape(sec_href_by_id[sec.section_id], quote=True)}">'
        f"{html.escape(sec.title_plain)}</a></li>"
        for sec in ast.sections
    ]
    letter_items: list[str] = []
    for ltr in ast.letters:
        sec_href = sec_href_by_id.get(ltr.section_id)
        if not sec_href:
            continue
        dom_id = letter_dom_anchor(ltr.letter_id)
        corr = " → ".join(p for p in (ltr.metadata.sender_display, ltr.metadata.recipient_display) if p)
        label = f"{ltr.sequence_index}. {corr or ltr.slug}"
        if ltr.metadata.dateline_plain:
            label = f"{label} ({ltr.metadata.dateline_plain})"
        letter_items.append(
            f'<li><a href="{html.escape(f"{sec_href}#{dom_id}", quote=True)}">{html.escape(label)}</a></li>'
        )

    letters_nav_block = (
        letter_items
        and '<nav id="nav-letters" aria-label="Correspondence Index">'
        "<h2>Index of Letters</h2>"
        f'<ol>{"".join(letter_items)}</ol></nav>'
    ) or ""
    page_items = [
        f'<li><a href="{html.escape(f"{href}#page-{pnum}", quote=True)}">{html.escape(fol)}</a></li>'
        for href, pnum, fol in page_list
    ]
    page_nav_block = (
        page_items
        and '<nav epub:type="page-list" role="doc-pagelist" id="nav-pages" aria-label="Page List">'
        "<h2>Pages</h2>"
        f'<ol>{"".join(page_items)}</ol></nav>'
    ) or ""
    lang_attrs = html_lang_dir_attrs(ast, xhtml=True)

    nav_doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" {lang_attrs}>
<head>
  <meta charset="utf-8"/>
  <title>Contents — {html.escape(ast.title)}</title>
  <link rel="stylesheet" type="text/css" href="styles.css"/>
</head>
<body>
  <nav epub:type="toc" role="doc-toc" id="toc">
    <h1>Contents</h1>
    <ol>{"".join(chap_items)}</ol>
  </nav>
  {letters_nav_block}
  {page_nav_block}
</body>
</html>
"""
    ET.fromstring(nav_doc.encode("utf-8"))
    return nav_doc


def _build_series_opf_meta(ast: CollectionAST) -> str:
    if not ast.series_id:
        return ""
    vol_xml = (
        f'\n    <meta refines="#series-id" property="group-position">{ast.volume_number}</meta>'
        if ast.volume_number is not None
        else ""
    )
    return (
        f'\n    <meta property="belongs-to-collection" id="series-id">{html.escape(ast.series_id)}</meta>'
        '\n    <meta refines="#series-id" property="collection-type">series</meta>'
        f"{vol_xml}"
    )


def _build_content_opf(
    ast: CollectionAST,
    manifest_entries: list[tuple[str, str, str, str]],
    spine_ids: list[str],
    *,
    has_pages: bool = False,
) -> str:
    book_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://paperlanterns.org/books/{ast.book_id}"))
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lang_code = html.escape((ast.language or "en").strip() or "en")
    creator_xml = (ast.author_or_editor and f"<dc:creator>{html.escape(ast.author_or_editor)}</dc:creator>") or ""
    date_xml = (ast.year and f"<dc:date>{ast.year}</dc:date>") or ""
    series_xml = _build_series_opf_meta(ast)
    page_a11y_xml = (
        has_pages and '\n    <meta property="schema:accessibilityFeature">pageNavigation</meta>'
    ) or ""
    manifest_xml = "\n    ".join(
        f'<item id="{html.escape(iid, quote=True)}" href="{html.escape(href, quote=True)}" '
        f'media-type="{html.escape(mtype, quote=True)}"'
        + ((props and f' properties="{html.escape(props, quote=True)}"') or "")
        + "/>"
        for iid, href, mtype, props in manifest_entries
    )
    spine_dir = (ast.text_direction == "rtl" and ' page-progression-direction="rtl"') or ""
    spine_xml = "\n    ".join(f'<itemref idref="{html.escape(sid, quote=True)}"/>' for sid in spine_ids)

    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">urn:uuid:{book_uuid}</dc:identifier>
    <dc:title>{html.escape(ast.title)}</dc:title>
    <dc:language>{lang_code}</dc:language>
    {creator_xml}
    {date_xml}
    <dc:rights>Public Domain / CC0 1.0</dc:rights>
    <meta property="dcterms:modified">{modified}</meta>{series_xml}
    <meta property="schema:accessMode">textual</meta>
    <meta property="schema:accessModeSufficient">textual</meta>
    <meta property="schema:accessibilityFeature">structuralNavigation</meta>
    <meta property="schema:accessibilityFeature">readingOrder</meta>{page_a11y_xml}
    <meta property="schema:accessibilityHazard">none</meta>
    <meta property="schema:accessibilitySummary">Diplomatic archival transcription with structural navigation, print-equivalent page markers, and linked footnote apparatus.</meta>
  </metadata>
  <manifest>
    {manifest_xml}
  </manifest>
  <spine{spine_dir}>
    {spine_xml}
  </spine>
</package>
"""
    ET.fromstring(opf.encode("utf-8"))
    return opf


def render_epub_edition(ast: CollectionAST, edition_dir: str | Path) -> dict[str, Any]:
    """Render `CollectionAST` into `<edition_dir>/epub/<book_id>.epub`."""
    out_dir = Path(edition_dir) / "epub"
    out_dir.mkdir(parents=True, exist_ok=True)
    epub_path = out_dir / f"{ast.book_id}.epub"

    block_lookup = index_ast_blocks(ast)
    folio_to_page = build_folio_to_page_map(ast)
    global_seen_pages: set[int] = set()
    page_list: list[tuple[str, int, str]] = []
    sec_href_by_id: dict[str, str] = {}
    xhtml_files: dict[str, str] = {}
    manifest_entries: list[tuple[str, str, str, str]] = [
        ("styles", "styles.css", "text/css", ""),
        ("nav", "nav.xhtml", "application/xhtml+xml", "nav"),
    ]
    spine_ids: list[str] = []

    sections_to_emit = list(ast.sections) or [
        BookSection(
            section_id="sec-empty",
            kind="epistolary_part",
            title_tagged=ast.title,
            title_plain=ast.title,
            start_page=1,
            end_page=max(1, ast.total_pages),
        )
    ]

    for idx, sec in enumerate(sections_to_emit, start=1):
        fname = epub_section_filename(sec.section_id)
        item_id = f"spine-{idx:03d}"
        sec_href_by_id[sec.section_id] = fname
        xhtml_str, sec_pages = _build_section_xhtml(
            ast,
            sec,
            block_lookup=block_lookup,
            global_seen_pages=global_seen_pages,
            folio_to_page=folio_to_page,
        )
        xhtml_files[fname] = xhtml_str
        manifest_entries.append((item_id, fname, "application/xhtml+xml", ""))
        spine_ids.append(item_id)
        for pnum, fol in sec_pages:
            page_list.append((fname, pnum, fol))

    nav_xhtml = _build_nav_xhtml(ast, sec_href_by_id, page_list)
    opf_xml = _build_content_opf(ast, manifest_entries, spine_ids, has_pages=bool(page_list))

    tmp_path = epub_path.with_suffix(".tmp.epub")
    with zipfile.ZipFile(tmp_path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf_xml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/styles.css", EDITORIAL_EPUB_CSS.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        for fname, content in xhtml_files.items():
            zf.writestr(f"OEBPS/{fname}", content.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    tmp_path.replace(epub_path)

    return {
        "epub_path": str(epub_path),
        "spine_sections": len(spine_ids),
        "page_list_count": len(page_list),
    }
