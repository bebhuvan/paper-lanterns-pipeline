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

from lanterns.core.models import BookSection, CollectionAST, LetterUnit
from lanterns.renderers.html_utils import (
    EDITORIAL_SHARED_CSS,
    collect_section_footnotes,
    footnotes_by_block_map,
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


def epub_section_filename(section_id: str) -> str:
    """Return `sec-<sanitized_id>.xhtml` inside `OEBPS/`."""
    stem = sanitize_xml_id(section_id, "sec")
    return f"{stem}.xhtml" if stem.startswith("sec-") else f"sec-{stem}.xhtml"


_collect_section_footnotes = collect_section_footnotes


def _build_section_xhtml(
    ast: CollectionAST,
    section: BookSection,
    *,
    global_seen_pages: set[int],
    folio_to_page: dict[str, int],
) -> tuple[str, list[tuple[int, str]]]:
    letters_by_id: dict[str, LetterUnit] = {ltr.letter_id: ltr for ltr in ast.letters}
    ordered_fn_ids, sec_footnotes = _collect_section_footnotes(section, ast)
    fn_by_block = footnotes_by_block_map(sec_footnotes)
    ref_registry: dict[str, list[str]] = {}
    before_pages = set(global_seen_pages)

    parts: list[str] = []
    for upage in sorted(p for p in section.unavailable_pages if p not in global_seen_pages):
        global_seen_pages.add(upage)
        parts.append(render_unavailable_page_banner(upage, epub=True))

    for item_idx, item in enumerate(section.items):
        if item.kind == "letter" and item.letter_id in letters_by_id:
            ltr = letters_by_id[item.letter_id]
            dom_id = html.escape(letter_dom_anchor(ltr.letter_id), quote=True)
            inner = render_block_stream_html(
                ltr.blocks,
                fn_by_block=fn_by_block,
                available_footnotes=sec_footnotes,
                ref_registry=ref_registry,
                seen_page_ids=global_seen_pages,
                folio_to_page=folio_to_page,
                epub=True,
                heading_base=2,
            )
            parts.append(f'<article id="{dom_id}" class="letter-unit">{inner}</article>')
        elif item.blocks:
            parts.append(
                render_block_stream_html(
                    item.blocks,
                    fn_by_block=fn_by_block,
                    available_footnotes=sec_footnotes,
                    ref_registry=ref_registry,
                    seen_page_ids=global_seen_pages,
                    folio_to_page=folio_to_page,
                    epub=True,
                    heading_base=2,
                    leading_section_title=section.title_plain if item_idx == 0 else "",
                )
            )

    apparatus = render_footnotes_apparatus_html(
        ordered_fn_ids,
        sec_footnotes,
        ref_registry,
        seen_page_ids=global_seen_pages,
        folio_to_page=folio_to_page,
        epub=True,
        heading_label="Notes",
    )

    new_pages = sorted(global_seen_pages - before_pages)
    folio_by_page = {p.page_number: (p.printed_folio or str(p.page_number)) for p in ast.pages}
    sec_page_entries = [(pnum, folio_by_page.get(pnum, str(pnum))) for pnum in new_pages]
    sec_title = render_inline_html(section.title_tagged or section.title_plain, epub=True)
    sec_dom_id = html.escape(sanitize_xml_id(section.section_id, "sec"), quote=True)

    xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(section.title_plain)}</title>
  <link rel="stylesheet" type="text/css" href="styles.css"/>
</head>
<body>
  <section id="{sec_dom_id}" epub:type="chapter">
    <h1>{sec_title}</h1>
    {"".join(parts)}
    {apparatus}
  </section>
</body>
</html>
"""
    ET.fromstring(xhtml.encode("utf-8"))
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
        '<nav id="nav-letters" aria-label="Correspondence Index">'
        "<h2>Index of Letters</h2>"
        f'<ol>{"".join(letter_items)}</ol></nav>'
        if letter_items
        else ""
    )
    page_items = [
        f'<li><a href="{html.escape(f"{href}#page-{pnum}", quote=True)}">{html.escape(fol)}</a></li>'
        for href, pnum, fol in page_list
    ]
    page_nav_block = (
        '<nav epub:type="page-list" id="nav-pages" aria-label="Page List">'
        "<h2>Pages</h2>"
        f'<ol>{"".join(page_items)}</ol></nav>'
        if page_items
        else ""
    )

    nav_doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Contents — {html.escape(ast.title)}</title>
  <link rel="stylesheet" type="text/css" href="styles.css"/>
</head>
<body>
  <nav epub:type="toc" id="toc">
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


def _build_content_opf(
    ast: CollectionAST,
    manifest_entries: list[tuple[str, str, str, str]],
    spine_ids: list[str],
) -> str:
    book_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://paperlanterns.org/books/{ast.book_id}"))
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    creator_xml = (
        f"<dc:creator>{html.escape(ast.author_or_editor)}</dc:creator>" if ast.author_or_editor else ""
    )
    date_xml = f"<dc:date>{ast.year}</dc:date>" if ast.year else ""
    manifest_xml = "\n    ".join(
        f'<item id="{html.escape(iid, quote=True)}" href="{html.escape(href, quote=True)}" '
        f'media-type="{html.escape(mtype, quote=True)}"'
        + (f' properties="{html.escape(props, quote=True)}"' if props else "")
        + "/>"
        for iid, href, mtype, props in manifest_entries
    )
    spine_xml = "\n    ".join(f'<itemref idref="{html.escape(sid, quote=True)}"/>' for sid in spine_ids)

    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">urn:uuid:{book_uuid}</dc:identifier>
    <dc:title>{html.escape(ast.title)}</dc:title>
    <dc:language>en</dc:language>
    {creator_xml}
    {date_xml}
    <dc:rights>Public Domain / CC0 1.0</dc:rights>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
    {manifest_xml}
  </manifest>
  <spine>
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

    sections_to_emit = list(ast.sections)
    if not sections_to_emit:
        sections_to_emit = [
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
            global_seen_pages=global_seen_pages,
            folio_to_page=folio_to_page,
        )
        xhtml_files[fname] = xhtml_str
        manifest_entries.append((item_id, fname, "application/xhtml+xml", ""))
        spine_ids.append(item_id)
        for pnum, fol in sec_pages:
            page_list.append((fname, pnum, fol))

    nav_xhtml = _build_nav_xhtml(ast, sec_href_by_id, page_list)
    opf_xml = _build_content_opf(ast, manifest_entries, spine_ids)

    tmp_path = epub_path.with_suffix(".tmp.epub")
    with zipfile.ZipFile(tmp_path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf_xml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/styles.css", EDITORIAL_SHARED_CSS.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        for fname, content in xhtml_files.items():
            zf.writestr(f"OEBPS/{fname}", content.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    tmp_path.replace(epub_path)

    return {
        "epub_path": str(epub_path),
        "spine_sections": len(spine_ids),
        "page_list_count": len(page_list),
    }
