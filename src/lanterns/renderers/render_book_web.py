"""Whole-Book Web Edition renderer (`web/index.html`, `web/sections/<section_id>.html`).

ROLE: Project CollectionAST into a navigable scholarly Whole-Book Web Edition with chapter-end apparatus.
INPUTS: CollectionAST from lanterns.core.models.
OUTPUTS: web/index.html, web/styles.css, web/reader.js, web/search_index.json, web/sections/<section_id>.html.
INVARIANT: Safe DOM (createElement/textContent only); zero book_id branching; zero content heuristics.
"""

from __future__ import annotations

import html
import shutil
from pathlib import Path
from typing import Any

from lanterns.core.models import BlockIR, BookSection, CollectionAST, FootnoteItem
from lanterns.renderers.html_utils import (
    EDITORIAL_SHARED_CSS,
    READER_STRICT_CSP,
    atomic_write_json,
    atomic_write_text,
    collect_section_footnotes,
    fold_diacritics_for_search,
    footnotes_by_block_map,
    html_lang_dir_attrs,
    index_ast_blocks,
    iter_section_render_units,
    render_block_stream_html,
    render_footnotes_apparatus_html,
    render_inline_html,
    render_optional_links,
    render_pager_slot,
    render_unavailable_page_banner,
    resolve_has_local_scans,
    sanitize_xml_id,
    strip_constrained_tags,
)

TIER_LABELS: dict[str, str] = {
    "source_certified": "Source-Certified Edition",
    "machine_checked": "Machine-Checked Edition",
    "community": "Community Edition · Open for Proofing",
    "blocked": "Verification Pending · Blocked",
}


def section_filename_stem(section_id: str) -> str:
    """Deterministic file stem for `web/sections/<section_id>.html`."""
    return sanitize_xml_id(section_id, "sec")


def letter_dom_anchor(letter_id: str) -> str:
    """Deterministic HTML element id for a letter inside a section page."""
    raw = letter_id.strip()
    return sanitize_xml_id(raw if raw.startswith("letter-") else f"letter-{raw}", "letter")


def build_folio_to_page_map(ast: CollectionAST) -> dict[str, int]:
    """Map printed folio labels (`'xii'`, `'14'`) to 1-based page numbers."""
    mapping: dict[str, int] = {}
    for page in ast.pages:
        if page.printed_folio:
            fol = page.printed_folio.strip()
            if fol and fol not in mapping:
                mapping[fol] = page.page_number
        mapping.setdefault(str(page.page_number), page.page_number)
    return mapping


def build_scan_dialog_html() -> str:
    return (
        '<dialog id="scan-drawer" class="scan-drawer" aria-label="Source scan facsimile">'
        '<div class="scan-drawer-bar">'
        '<span id="scan-drawer-title">Source Scan</span>'
        '<div><a id="scan-drawer-proof-link" href="#">Open in Proofing Studio</a> '
        '<button type="button" id="scan-drawer-close">Close</button></div>'
        "</div>"
        '<div class="scan-drawer-body"><img id="scan-drawer-img" alt="Archival page scan" />'
        '<p id="scan-drawer-fallback"></p></div>'
        "</dialog>"
    )


_build_scan_dialog_html = build_scan_dialog_html


_collect_section_footnotes = collect_section_footnotes


def _render_section_item_html(
    section: BookSection,
    ast: CollectionAST,
    block_lookup: dict[str, BlockIR],
    *,
    fn_by_block: dict[str, list[FootnoteItem]],
    sec_footnotes: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    seen_page_ids: set[int],
    folio_to_page: dict[str, int],
    proof_href: str | None,
    has_letters_edition: bool,
) -> str:
    parts: list[str] = []
    unavail_sorted = sorted(p for p in section.unavailable_pages if p not in seen_page_ids)
    unavail_idx = 0
    render_units = iter_section_render_units(section, ast, block_lookup)

    for unit_idx, (u_kind, blks, ltr, u_obj) in enumerate(render_units):
        first_page = blks[0].page_number if blks else section.end_page + 1
        while unavail_idx < len(unavail_sorted) and unavail_sorted[unavail_idx] <= first_page:
            upage = unavail_sorted[unavail_idx]
            seen_page_ids.add(upage)
            parts.append(render_unavailable_page_banner(upage, proof_href_prefix=proof_href))
            unavail_idx += 1

        if ltr is not None:
            dom_id = html.escape(letter_dom_anchor(ltr.letter_id), quote=True)
            standalone_link = render_optional_links(
                ((has_letters_edition, f'<a href="../../letters/{html.escape(ltr.slug, quote=True)}.html">Standalone Letter View →</a>'),)
            )
            sender_rec = " → ".join(
                p for p in (ltr.metadata.sender_display, ltr.metadata.recipient_display) if p
            )
            meta_txt = html.escape(sender_rec or f"Letter #{ltr.sequence_index}")
            inner_html = render_block_stream_html(
                blks,
                fn_by_block=fn_by_block,
                available_footnotes=sec_footnotes,
                ref_registry=ref_registry,
                seen_page_ids=seen_page_ids,
                folio_to_page=folio_to_page,
                proof_href_prefix=proof_href,
                heading_base=2,
                children=ltr.children,
            )
            parts.append(
                f'<article id="{dom_id}" class="letter-unit">'
                f'<div class="letter-meta-bar"><span>{meta_txt}</span>{standalone_link}</div>'
                f"{inner_html}</article>"
            )
        elif blks:
            inner_html = render_block_stream_html(
                blks,
                fn_by_block=fn_by_block,
                available_footnotes=sec_footnotes,
                ref_registry=ref_registry,
                seen_page_ids=seen_page_ids,
                folio_to_page=folio_to_page,
                proof_href_prefix=proof_href,
                heading_base=2,
                leading_section_title=(unit_idx == 0 and section.title_plain) or "",
                children=(u_obj and u_obj.children) or None,
            )
            if u_obj is not None:
                u_dom = html.escape(sanitize_xml_id(u_obj.unit_id, "unit"), quote=True)
                parts.append(f'<div id="{u_dom}" class="structural-unit unit-{u_kind.replace("_", "-")}">{inner_html}</div>')
            else:
                parts.append(inner_html)

    while unavail_idx < len(unavail_sorted):
        upage = unavail_sorted[unavail_idx]
        seen_page_ids.add(upage)
        parts.append(render_unavailable_page_banner(upage, proof_href_prefix=proof_href))
        unavail_idx += 1
    return "\n".join(parts)


def _render_section_page(
    ast: CollectionAST,
    section: BookSection,
    *,
    block_lookup: dict[str, BlockIR],
    prev_sec: BookSection | None,
    next_sec: BookSection | None,
    folio_to_page: dict[str, int],
    has_proofing: bool,
    has_letters_edition: bool,
    has_local_scans: bool = False,
) -> str:
    ordered_fn_ids, sec_footnotes = _collect_section_footnotes(section, ast)
    fn_by_block = footnotes_by_block_map(sec_footnotes)
    ref_registry: dict[str, list[str]] = {}
    seen_page_ids: set[int] = set()
    proof_href = (has_proofing and "../../proofing/index.html") or None

    body_html = _render_section_item_html(
        section,
        ast,
        block_lookup,
        fn_by_block=fn_by_block,
        sec_footnotes=sec_footnotes,
        ref_registry=ref_registry,
        seen_page_ids=seen_page_ids,
        folio_to_page=folio_to_page,
        proof_href=proof_href,
        has_letters_edition=has_letters_edition,
    )
    apparatus_html = render_footnotes_apparatus_html(
        ordered_fn_ids,
        sec_footnotes,
        ref_registry,
        seen_page_ids=seen_page_ids,
        folio_to_page=folio_to_page,
        proof_href_prefix=proof_href,
        heading_label="Section Apparatus & Footnotes",
    )

    sec_title_html = render_inline_html(section.title_tagged or section.title_plain)
    prev_link = render_pager_slot(
        prev_sec and f"{section_filename_stem(prev_sec.section_id)}.html",
        prev_sec and f"← {prev_sec.title_plain}",
    )
    next_link = render_pager_slot(
        next_sec and f"{section_filename_stem(next_sec.section_id)}.html",
        next_sec and f"{next_sec.title_plain} →",
    )
    letters_nav = render_optional_links(((has_letters_edition, '<a href="../../letters/index.html">Letters Catalog</a>'),))
    proof_nav = render_optional_links(((has_proofing, '<a href="../../proofing/index.html">Proofing Studio</a>'),))
    tier = (ast.rubric and ast.rubric.provenance_tier) or "community"
    archive_attr = html.escape(ast.archive_id or "", quote=True)
    csp_escaped = html.escape(READER_STRICT_CSP, quote=True)
    lang_attrs = html_lang_dir_attrs(ast)

    return f"""<!DOCTYPE html>
<html {lang_attrs}>
<head>
  <meta charset="utf-8" />
  <meta http-equiv="Content-Security-Policy" content="{csp_escaped}" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(section.title_plain)} — {html.escape(ast.title)}</title>
  <link rel="stylesheet" href="../styles.css" />
</head>
<body data-archive-id="{archive_attr}" data-scan-prefix="../../proofing/scans" data-has-local-scans="{int(bool(has_local_scans))}">
  <header class="masthead">
    <a class="masthead-brand" href="../index.html">{html.escape(ast.title)}</a>
    <nav class="masthead-nav">
      <a href="../index.html">Contents</a>
      {letters_nav}
      {proof_nav}
      <span class="tier-badge">{html.escape(TIER_LABELS.get(tier, tier))}</span>
    </nav>
  </header>
  <main class="edition-container">
    <div class="reading-column">
      <h1 class="block-heading">{sec_title_html}</h1>
      {body_html}
      {apparatus_html}
      <nav class="pager-nav" aria-label="Section navigation">
        {prev_link}
        {next_link}
      </nav>
    </div>
  </main>
  {_build_scan_dialog_html()}
  <script src="../reader.js" defer></script>
</body>
</html>
"""


def _build_search_entries(ast: CollectionAST, block_lookup: dict[str, BlockIR] | None = None) -> list[dict[str, Any]]:
    lookup = block_lookup or index_ast_blocks(ast)
    entries: list[dict[str, Any]] = []
    seen_letter_ids: set[str] = set()
    for sec in ast.sections:
        sec_stem = section_filename_stem(sec.section_id)
        for u_kind, blks, ltr, u_obj in iter_section_render_units(sec, ast, lookup):
            if ltr is not None:
                seen_letter_ids.add(ltr.letter_id)
                plain_body = " ".join(strip_constrained_tags(b.tagged_text) for b in blks).strip()
                corr = " → ".join(p for p in (ltr.metadata.sender_display, ltr.metadata.recipient_display) if p)
                title = (corr and f"Letter #{ltr.sequence_index}: {corr}") or f"Letter #{ltr.sequence_index}"
                entries.append(
                    {
                        "kind": "letter",
                        "title": title,
                        "href": f"sections/{sec_stem}.html#{letter_dom_anchor(ltr.letter_id)}",
                        "page": ltr.start_page,
                        "folio": ltr.start_folio or str(ltr.start_page),
                        "snippet": plain_body[:400],
                        "folded": fold_diacritics_for_search(plain_body),
                    }
                )
            elif blks:
                plain_body = " ".join(strip_constrained_tags(b.tagged_text) for b in blks).strip()
                if not plain_body:
                    continue
                frag = (u_obj and f"#{sanitize_xml_id(u_obj.unit_id, 'unit')}") or ""
                entries.append(
                    {
                        "kind": u_kind,
                        "title": (u_obj and u_obj.title) or sec.title_plain,
                        "href": f"sections/{sec_stem}.html{frag}",
                        "page": blks[0].page_number,
                        "folio": blks[0].printed_folio or str(blks[0].page_number),
                        "snippet": plain_body[:400],
                        "folded": fold_diacritics_for_search(plain_body),
                    }
                )
    sec_stem_by_id = {s.section_id: section_filename_stem(s.section_id) for s in ast.sections}
    for ltr in ast.letters:
        if ltr.letter_id in seen_letter_ids:
            continue
        plain_body = " ".join(strip_constrained_tags(b.tagged_text) for b in ltr.blocks).strip()
        corr = " → ".join(p for p in (ltr.metadata.sender_display, ltr.metadata.recipient_display) if p)
        entries.append(
            {
                "kind": "letter",
                "title": (corr and f"Letter #{ltr.sequence_index}: {corr}") or f"Letter #{ltr.sequence_index}",
                "href": f"sections/{sec_stem_by_id.get(ltr.section_id, 'sec')}.html#{letter_dom_anchor(ltr.letter_id)}",
                "page": ltr.start_page,
                "folio": ltr.start_folio or str(ltr.start_page),
                "snippet": plain_body[:400],
                "folded": fold_diacritics_for_search(plain_body),
            }
        )
    return entries


def _render_landing_page(
    ast: CollectionAST,
    *,
    has_letters_edition: bool,
    has_epub: bool,
    has_markdown: bool,
    has_tei: bool,
    has_proofing: bool,
) -> str:
    tier = (ast.rubric and ast.rubric.provenance_tier) or "community"
    score_str = (ast.rubric and f"{ast.rubric.total_score:.1f} / 100") or "Pending"
    sec_rows: list[str] = []
    for sec in ast.sections:
        stem = html.escape(section_filename_stem(sec.section_id), quote=True)
        sec_rows.append(
            f'<tr><td>{html.escape(sec.kind)}</td>'
            f'<td><a href="sections/{stem}.html">{html.escape(sec.title_plain)}</a></td>'
            f"<td>pp. {sec.start_page}–{sec.end_page}</td></tr>"
        )

    sec_stem_by_id = {s.section_id: section_filename_stem(s.section_id) for s in ast.sections}
    letter_rows: list[str] = []
    for ltr in ast.letters:
        stem = html.escape(sec_stem_by_id.get(ltr.section_id, "sec"), quote=True)
        dom_id = html.escape(letter_dom_anchor(ltr.letter_id), quote=True)
        in_book_href = f"sections/{stem}.html#{dom_id}"
        standalone_cell = render_optional_links(
            ((has_letters_edition, f'<a href="../letters/{html.escape(ltr.slug, quote=True)}.html">Letter Page</a>'),)
        )
        fol = html.escape(ltr.start_folio or str(ltr.start_page))
        sender = html.escape(ltr.metadata.sender_display or "—")
        recipient = html.escape(ltr.metadata.recipient_display or "—")
        dateline = html.escape(ltr.metadata.dateline_plain or "—")
        incipit = html.escape(ltr.metadata.incipit[:90])
        letter_rows.append(
            f"<tr><td>{ltr.sequence_index}</td>"
            f'<td><a href="{in_book_href}">{sender} → {recipient}</a></td>'
            f"<td>{dateline}</td><td>p. {fol}</td><td>{incipit}</td><td>{standalone_cell}</td></tr>"
        )

    download_specs = (
        (has_letters_edition, '<a href="../letters/index.html">Individual Online Letters</a>'),
        (has_epub, f'<a href="../epub/{html.escape(ast.book_id, quote=True)}.epub">EPUB 3</a>'),
        (has_markdown, f'<a href="../markdown/{html.escape(ast.book_id, quote=True)}.md">Scholarly Markdown</a>'),
        (has_tei, f'<a href="../tei/{html.escape(ast.book_id, quote=True)}.tei.xml">TEI P5 XML</a>'),
        (has_proofing, '<a href="../proofing/index.html">Community Proofing Studio</a>'),
    )
    downloads_html = " · ".join(link for enabled, link in download_specs if enabled)

    year_txt = (ast.year and f" ({ast.year})") or ""
    byline = html.escape(f"{ast.author_or_editor}{year_txt}".strip() or "Public Domain Archival Correspondence")
    letters_section = render_optional_links(
        (
            (
                bool(letter_rows),
                '<section style="margin-top:2.8rem;">'
                "<h2>Sequential Table of Letters</h2>"
                '<table class="catalog-table">'
                "<thead><tr><th>#</th><th>Correspondents</th><th>Dateline</th><th>Folio</th><th>Incipit</th><th>Standalone</th></tr></thead>"
                f'<tbody>{"".join(letter_rows)}</tbody></table></section>',
            ),
        )
    )
    lang_attrs = html_lang_dir_attrs(ast)

    return f"""<!DOCTYPE html>
<html {lang_attrs}>
<head>
  <meta charset="utf-8" />
  <meta http-equiv="Content-Security-Policy" content="{html.escape(READER_STRICT_CSP, quote=True)}" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(ast.title)} — Complete Archival Edition</title>
  <link rel="stylesheet" href="styles.css" />
</head>
<body>
  <header class="masthead">
    <span class="masthead-brand">{html.escape(ast.title)}</span>
    <nav class="masthead-nav">
      <span class="tier-badge">{html.escape(TIER_LABELS.get(tier, tier))}</span>
    </nav>
  </header>
  <main class="edition-container">
    <section>
      <h1>{html.escape(ast.title)}</h1>
      <p style="font-size:1.25rem;color:var(--ink-secondary);">{byline}</p>
      <p style="font-family:var(--font-sans);font-size:0.84rem;color:var(--ink-muted);">
        Pages: {ast.total_pages} · Letters: {len(ast.letters)} · Footnotes: {len(ast.footnotes)} · Rubric: {html.escape(score_str)}
      </p>
      <p style="font-family:var(--font-sans);font-size:0.86rem;">{downloads_html}</p>
    </section>
    <section style="margin-top:2.4rem;">
      <h2>Search Edition (Diacritic-Insensitive)</h2>
      <div class="catalog-filters">
        <label>Find word, correspondent, or passage
          <input type="search" id="book-search-input" placeholder="e.g. Rajput matches Rájput, depot matches depôt..." />
        </label>
      </div>
      <ul id="book-search-results" class="archival-list"></ul>
    </section>
    <section style="margin-top:2.4rem;">
      <h2>Table of Contents</h2>
      <table class="catalog-table">
        <thead><tr><th>Kind</th><th>Section</th><th>Leaves</th></tr></thead>
        <tbody>{"".join(sec_rows)}</tbody>
      </table>
    </section>
    {letters_section}
  </main>
  <script src="reader.js" defer></script>
</body>
</html>
"""


READER_JS = """/* Paper Lanterns v2 — Safe-DOM Facsimile Drawer & Diacritic-Insensitive Search */
(function () {
  "use strict";

  function foldSearch(str) {
    return String(str || "")
      .replace(/[æÆ]/g, "ae")
      .replace(/[œŒ]/g, "oe")
      .replace(/ſ/g, "s")
      .replace(/ß/g, "ss")
      .normalize("NFD")
      .replace(/[\\u0300-\\u036f]/g, "")
      .toLowerCase();
  }

  function padPage(num) {
    return String(num).padStart(4, "0");
  }

  function wireScanDrawer() {
    var dialog = document.getElementById("scan-drawer");
    if (!dialog || typeof dialog.showModal !== "function") return;
    var titleEl = document.getElementById("scan-drawer-title");
    var imgEl = document.getElementById("scan-drawer-img");
    var fallbackEl = document.getElementById("scan-drawer-fallback");
    var proofLink = document.getElementById("scan-drawer-proof-link");
    var closeBtn = document.getElementById("scan-drawer-close");
    var archiveId = document.body.getAttribute("data-archive-id") || "";
    var scanPrefix = document.body.getAttribute("data-scan-prefix") || "../proofing/scans";
    var hasLocalScans = document.body.getAttribute("data-has-local-scans") === "1";

    closeBtn.addEventListener("click", function () {
      dialog.close();
    });

    var markers = document.querySelectorAll("a.folio-marker[data-page]");
    markers.forEach(function (link) {
      link.addEventListener("click", function (ev) {
        if (ev.metaKey || ev.ctrlKey || ev.shiftKey) return;
        ev.preventDefault();
        var page = parseInt(link.getAttribute("data-page") || "1", 10);
        var folio = link.getAttribute("data-folio") || String(page);
        titleEl.textContent = "Source Leaf " + page + " (Printed p. " + folio + ")";
        proofLink.setAttribute("href", link.getAttribute("href") || "#");
        fallbackEl.textContent = "";
        var localSrc = scanPrefix + "/page_" + padPage(page) + ".png";
        var iaSrc = archiveId
          ? "https://archive.org/download/" + encodeURIComponent(archiveId) + "/page/n" + Math.max(0, page - 1) + "_w1400.jpg"
          : "";
        imgEl.onerror = function () {
          if (iaSrc && imgEl.src !== iaSrc) {
            imgEl.src = iaSrc;
          } else {
            fallbackEl.textContent = "Local scan preview not bundled; open in Proofing Studio to inspect.";
          }
        };
        if (hasLocalScans) {
          imgEl.src = localSrc;
        } else if (iaSrc) {
          imgEl.src = iaSrc;
        } else {
          imgEl.removeAttribute("src");
          fallbackEl.textContent = "Local scan preview not bundled; open in Proofing Studio to inspect.";
        }
        dialog.showModal();
      });
    });
  }

  function wireLandingSearch() {
    var input = document.getElementById("book-search-input");
    var listEl = document.getElementById("book-search-results");
    if (!input || !listEl) return;
    var indexEntries = null;

    function clearNode(el) {
      while (el.firstChild) el.removeChild(el.firstChild);
    }

    function renderHits(query) {
      clearNode(listEl);
      var q = foldSearch(query.trim());
      if (!q || !indexEntries) return;
      var matches = indexEntries.filter(function (item) {
        return item.folded.indexOf(q) !== -1 || foldSearch(item.title).indexOf(q) !== -1;
      }).slice(0, 25);
      matches.forEach(function (hit) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.setAttribute("href", hit.href);
        a.textContent = hit.title + " (p. " + hit.folio + ")";
        var span = document.createElement("span");
        span.textContent = " — " + hit.snippet.slice(0, 140);
        li.appendChild(a);
        li.appendChild(span);
        listEl.appendChild(li);
      });
    }

    input.addEventListener("input", function () {
      if (indexEntries !== null) {
        renderHits(input.value);
        return;
      }
      fetch("search_index.json")
        .then(function (r) { return r.json(); })
        .then(function (doc) {
          indexEntries = doc.entries || [];
          renderHits(input.value);
        })
        .catch(function () {
          indexEntries = [];
        });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireScanDrawer();
    wireLandingSearch();
  });
})();
"""


def render_book_web(
    ast: CollectionAST,
    edition_dir: str | Path,
    *,
    has_letters_edition: bool = True,
    has_epub: bool = True,
    has_markdown: bool = True,
    has_tei: bool = True,
    has_proofing: bool = True,
    has_local_scans: bool | None = None,
) -> dict[str, Any]:
    """Render the Complete Book Web Edition into `<edition_dir>/web/`."""
    ed_root = Path(edition_dir)
    web_dir = ed_root / "web"
    sec_dir = web_dir / "sections"
    shutil.rmtree(sec_dir, ignore_errors=True)
    sec_dir.mkdir(parents=True, exist_ok=True)
    local_scans_flag = resolve_has_local_scans(ed_root, has_local_scans)

    atomic_write_text(web_dir / "styles.css", EDITORIAL_SHARED_CSS)
    atomic_write_text(web_dir / "reader.js", READER_JS)

    block_lookup = index_ast_blocks(ast)
    folio_to_page = build_folio_to_page_map(ast)
    search_entries = _build_search_entries(ast, block_lookup)
    atomic_write_json(web_dir / "search_index.json", {"book_id": ast.book_id, "entries": search_entries})

    for idx, sec in enumerate(ast.sections):
        prev_sec = (idx > 0 and ast.sections[idx - 1]) or None
        next_sec = (idx + 1 < len(ast.sections) and ast.sections[idx + 1]) or None
        sec_html = _render_section_page(
            ast,
            sec,
            block_lookup=block_lookup,
            prev_sec=prev_sec,
            next_sec=next_sec,
            folio_to_page=folio_to_page,
            has_proofing=has_proofing,
            has_letters_edition=has_letters_edition,
            has_local_scans=local_scans_flag,
        )
        stem = section_filename_stem(sec.section_id)
        atomic_write_text(sec_dir / f"{stem}.html", sec_html)

    landing_html = _render_landing_page(
        ast,
        has_letters_edition=has_letters_edition,
        has_epub=has_epub,
        has_markdown=has_markdown,
        has_tei=has_tei,
        has_proofing=has_proofing,
    )
    atomic_write_text(web_dir / "index.html", landing_html)
    return {
        "web_index": str(web_dir / "index.html"),
        "section_count": len(ast.sections),
    }
