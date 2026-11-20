"""Individual Online Letters Edition renderer (`letters/index.{html,json}`, `letters/<slug>.{html,md,json}`).

ROLE: Project every LetterUnit from CollectionAST into standalone browseable HTML, Astro/Hugo Markdown, and JSON.
INPUTS: CollectionAST from lanterns.core.models.
OUTPUTS: letters/index.html, letters/index.json, letters/styles.css, letters/letters.js, letters/<slug>.{html,md,json}.
INVARIANT: Self-contained per-letter footnotes; safe DOM (createElement/textContent only); zero book_id branching.
"""

from __future__ import annotations

import html
import shutil
from pathlib import Path
from typing import Any

from lanterns.core.models import BlockIR, CollectionAST, FootnoteItem, LetterUnit
from lanterns.renderers.html_utils import (
    EDITORIAL_SHARED_CSS,
    READER_STRICT_CSP,
    atomic_write_json,
    atomic_write_text,
    coalesce_continued_blocks,
    fold_diacritics_for_search,
    footnotes_by_block_map,
    format_markdown_footnote,
    html_lang_dir_attrs,
    index_ast_blocks,
    render_block_stream_html,
    render_footnotes_apparatus_html,
    render_inline_markdown,
    render_optional_links,
    render_pager_slot,
    resolve_has_local_scans,
    sanitize_xml_id,
    strip_constrained_tags,
)
from lanterns.renderers.render_book_web import (
    build_folio_to_page_map,
    build_scan_dialog_html,
    letter_dom_anchor,
    section_filename_stem,
)

_index_blocks_by_id = index_ast_blocks


def _letter_header_label(letter: LetterUnit) -> str:
    raw = strip_constrained_tags(letter.metadata.header_tagged or "")
    parts = [" ".join(ln.split()) for ln in raw.splitlines() if ln.strip()]
    return " · ".join(parts)


def _letter_display_title(letter: LetterUnit) -> str:
    correspondents = " → ".join(
        p for p in (letter.metadata.sender_display, letter.metadata.recipient_display) if p
    )
    return correspondents or _letter_header_label(letter) or f"Letter #{letter.sequence_index}"


def _yaml_quote(value: str | None) -> str:
    if value is None:
        return "null"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


_inline_markdown = render_inline_markdown


def _render_letter_markdown(letter: LetterUnit, ast: CollectionAST, block_lookup: dict[str, BlockIR]) -> str:
    meta = letter.metadata
    langs_yaml = "[" + ", ".join(_yaml_quote(lang) for lang in meta.languages) + "]"
    lines: list[str] = [
        "---",
        f"letter_id: {_yaml_quote(letter.letter_id)}",
        f"slug: {_yaml_quote(letter.slug)}",
        f"sequence_index: {letter.sequence_index}",
        f"sender: {_yaml_quote(meta.sender_display)}",
        f"recipient: {_yaml_quote(meta.recipient_display)}",
        f"dateline: {_yaml_quote(meta.dateline_plain)}",
        f"start_page: {letter.start_page}",
        f"end_page: {letter.end_page}",
        f"start_folio: {_yaml_quote(letter.start_folio)}",
        f"languages: {langs_yaml}",
        f"boundary_confidence: {_yaml_quote(letter.boundary_confidence)}",
        "---",
        "",
    ]

    ltr_fns = {fid: ast.footnotes[fid] for fid in letter.footnote_ids if fid in ast.footnotes}
    fn_by_blk = footnotes_by_block_map(ltr_fns)

    prec_blocks = [block_lookup[bid] for bid in letter.preceding_editorial_block_ids if bid in block_lookup]
    if prec_blocks:
        lines.append("> **Editorial Context**")
        for pblk in prec_blocks:
            lines.append(f"> {_inline_markdown(pblk.tagged_text, fn_by_blk.get(pblk.block_id))}")
        lines.append("")

    merged_blocks, fn_by_blk = coalesce_continued_blocks(letter.blocks, fn_by_blk, include_folio_tokens=False)
    md_prefix = {"letter_header": "# ", "heading": "## ", "blockquote": "> ", "epigraph": "> "}
    for blk in merged_blocks:
        text_md = _inline_markdown(blk.tagged_text, fn_by_blk.get(blk.block_id))
        if blk.type == "ornamental_break":
            lines.append("* * *\n")
        elif blk.type == "margin_note":
            lines.append(f"> *[Margin: {text_md.replace(chr(10), ' ')}]*\n")
        else:
            pref = md_prefix.get(blk.type, "")
            body = "  \n".join(text_md.split("\n")) if (not pref and (blk.type == "verse" or blk.line_breaks_preserved)) else text_md
            lines.append(f"{pref}{body}\n")

    foll_blocks = [block_lookup[bid] for bid in letter.following_editorial_block_ids if bid in block_lookup]
    if foll_blocks:
        lines.append("> **Editorial Note**")
        for fblk in foll_blocks:
            lines.append(f"> {_inline_markdown(fblk.tagged_text, fn_by_blk.get(fblk.block_id))}")
        lines.append("")

    if ltr_fns:
        lines.append("## Notes\n")
        for fid in letter.footnote_ids:
            if fid in ltr_fns:
                lines.append(format_markdown_footnote(ltr_fns[fid], include_orphan_prefix=False))
        lines.append("")
    return "\n".join(lines)


def _render_editorial_frame_html(
    label: str,
    blocks: list[BlockIR],
    *,
    fn_by_block: dict[str, list[FootnoteItem]],
    ltr_footnotes: dict[str, FootnoteItem],
    ref_registry: dict[str, list[str]],
    seen_page_ids: set[int],
    folio_to_page: dict[str, int],
    proof_href: str | None,
) -> str:
    if not blocks:
        return ""
    inner = render_block_stream_html(
        blocks,
        fn_by_block=fn_by_block,
        available_footnotes=ltr_footnotes,
        ref_registry=ref_registry,
        seen_page_ids=seen_page_ids,
        folio_to_page=folio_to_page,
        proof_href_prefix=proof_href,
        heading_base=2,
    )
    return f'<aside class="editorial-frame-box"><span class="editorial-frame-label">{html.escape(label)}</span>{inner}</aside>'


def _render_standalone_letter_html(
    ast: CollectionAST,
    letter: LetterUnit,
    *,
    prev_letter: LetterUnit | None,
    next_letter: LetterUnit | None,
    sec_stem: str,
    block_lookup: dict[str, BlockIR],
    folio_to_page: dict[str, int],
    has_book_web: bool,
    has_proofing: bool,
    has_local_scans: bool = False,
) -> str:
    ltr_footnotes = {fid: ast.footnotes[fid] for fid in letter.footnote_ids if fid in ast.footnotes}
    fn_by_block = footnotes_by_block_map(ltr_footnotes)
    ref_registry: dict[str, list[str]] = {}
    seen_page_ids: set[int] = set()
    proof_href = (has_proofing and "../proofing/index.html") or None

    prec_blocks = [block_lookup[bid] for bid in letter.preceding_editorial_block_ids if bid in block_lookup]
    foll_blocks = [block_lookup[bid] for bid in letter.following_editorial_block_ids if bid in block_lookup]

    prec_html = _render_editorial_frame_html(
        "Preceding Editorial Context",
        prec_blocks,
        fn_by_block=fn_by_block,
        ltr_footnotes=ltr_footnotes,
        ref_registry=ref_registry,
        seen_page_ids=seen_page_ids,
        folio_to_page=folio_to_page,
        proof_href=proof_href,
    )
    letter_body_html = render_block_stream_html(
        letter.blocks,
        fn_by_block=fn_by_block,
        available_footnotes=ltr_footnotes,
        ref_registry=ref_registry,
        seen_page_ids=seen_page_ids,
        folio_to_page=folio_to_page,
        proof_href_prefix=proof_href,
        heading_base=2,
        children=letter.children,
    )
    foll_html = _render_editorial_frame_html(
        "Following Editorial Note",
        foll_blocks,
        fn_by_block=fn_by_block,
        ltr_footnotes=ltr_footnotes,
        ref_registry=ref_registry,
        seen_page_ids=seen_page_ids,
        folio_to_page=folio_to_page,
        proof_href=proof_href,
    )
    apparatus_html = render_footnotes_apparatus_html(
        letter.footnote_ids,
        ltr_footnotes,
        ref_registry,
        seen_page_ids=seen_page_ids,
        folio_to_page=folio_to_page,
        proof_href_prefix=proof_href,
        heading_label="Notes to This Letter",
    )

    display_heading = _letter_display_title(letter)
    book_deep_link = render_optional_links(
        (
            (
                has_book_web,
                f'<a href="../web/sections/{html.escape(sec_stem, quote=True)}.html#{html.escape(letter_dom_anchor(letter.letter_id), quote=True)}">'
                "Read in Complete Book</a>",
            ),
        )
    )
    proof_deep_link = render_optional_links(
        (
            (
                has_proofing,
                f'<a href="../proofing/index.html#page-{letter.start_page}">Verify Source Scan (p. {html.escape(letter.start_folio or str(letter.start_page))})</a>',
            ),
        )
    )
    prev_nav = render_pager_slot(
        prev_letter and f"{prev_letter.slug}.html",
        prev_letter and f"← Letter #{prev_letter.sequence_index}",
    )
    next_nav = render_pager_slot(
        next_letter and f"{next_letter.slug}.html",
        next_letter and f"Letter #{next_letter.sequence_index} →",
    )

    archive_attr = html.escape(ast.archive_id or "", quote=True)
    csp_escaped = html.escape(READER_STRICT_CSP, quote=True)
    drawer_html = render_optional_links(((has_book_web, build_scan_dialog_html()),))
    drawer_js = render_optional_links(((has_book_web, '<script src="../web/reader.js" defer></script>'),))
    lang_attrs = html_lang_dir_attrs(ast)

    return f"""<!DOCTYPE html>
<html {lang_attrs}>
<head>
  <meta charset="utf-8" />
  <meta http-equiv="Content-Security-Policy" content="{csp_escaped}" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(display_heading)} — {html.escape(ast.title)}</title>
  <link rel="stylesheet" href="styles.css" />
</head>
<body data-archive-id="{archive_attr}" data-scan-prefix="../proofing/scans" data-has-local-scans="{int(bool(has_local_scans))}">
  <header class="masthead">
    <a class="masthead-brand" href="index.html">{html.escape(ast.title)} · Correspondence</a>
    <nav class="masthead-nav">
      <a href="index.html">All Letters</a>
      {book_deep_link}
      {proof_deep_link}
    </nav>
  </header>
  <main class="edition-container">
    <article class="reading-column" id="{html.escape(sanitize_xml_id(letter.letter_id, 'letter'), quote=True)}">
      <div class="letter-meta-bar">
        <span>Letter #{letter.sequence_index} · {html.escape(letter.metadata.dateline_plain or f"p. {letter.start_folio or letter.start_page}")}</span>
        <span>Confidence: {html.escape(letter.boundary_confidence)}</span>
      </div>
      <h1 class="block-heading">{html.escape(display_heading)}</h1>
      {prec_html}
      <div class="letter-stream">
        {letter_body_html}
      </div>
      {foll_html}
      {apparatus_html}
      <nav class="pager-nav" aria-label="Letter sequence navigation">
        {prev_nav}
        {next_nav}
      </nav>
    </article>
  </main>
  {drawer_html}
  {drawer_js}
</body>
</html>
"""


LETTERS_CATALOG_JS = """/* Paper Lanterns v2 — Safe-DOM Interactive Correspondence Catalog */
(function () {
  "use strict";

  function fold(str) {
    return String(str || "")
      .replace(/[æÆ]/g, "ae")
      .replace(/[œŒ]/g, "oe")
      .replace(/ſ/g, "s")
      .replace(/ß/g, "ss")
      .normalize("NFD")
      .replace(/[\\u0300-\\u036f]/g, "")
      .toLowerCase();
  }

  function clearChildren(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function populateSelect(selectEl, values, allLabel) {
    clearChildren(selectEl);
    var optAll = document.createElement("option");
    optAll.value = "";
    optAll.textContent = allLabel;
    selectEl.appendChild(optAll);
    values.forEach(function (val) {
      var opt = document.createElement("option");
      opt.value = val;
      opt.textContent = val;
      selectEl.appendChild(opt);
    });
  }

  function uniqueSorted(arr) {
    var seen = {};
    var out = [];
    arr.forEach(function (v) {
      if (v && !seen[v]) {
        seen[v] = true;
        out.push(v);
      }
    });
    return out.sort();
  }

  function initCatalog() {
    var tbody = document.getElementById("letters-catalog-tbody");
    var countEl = document.getElementById("letters-count-summary");
    if (!tbody) return;

    var qInput = document.getElementById("filter-query");
    var senderSel = document.getElementById("filter-sender");
    var recSel = document.getElementById("filter-recipient");
    var dateInput = document.getElementById("filter-date");
    var langSel = document.getElementById("filter-language");
    var secSel = document.getElementById("filter-section");

    var letters = [];

    function renderRows() {
      var q = fold(qInput.value.trim());
      var sVal = senderSel.value;
      var rVal = recSel.value;
      var dVal = fold(dateInput.value.trim());
      var lVal = langSel.value;
      var secVal = secSel.value;

      var filtered = letters.filter(function (item) {
        if (sVal && item.sender !== sVal) return false;
        if (rVal && item.recipient !== rVal) return false;
        if (secVal && item.section_title !== secVal) return false;
        if (lVal && (item.languages || []).indexOf(lVal) === -1) return false;
        if (dVal && fold(item.dateline || "").indexOf(dVal) === -1) return false;
        if (q && item.search_folded.indexOf(q) === -1) return false;
        return true;
      });

      clearChildren(tbody);
      if (countEl) {
        countEl.textContent = "Showing " + filtered.length + " of " + letters.length + " letters";
      }

      filtered.forEach(function (item) {
        var tr = document.createElement("tr");

        var tdSeq = document.createElement("td");
        tdSeq.textContent = String(item.sequence_index);
        tr.appendChild(tdSeq);

        var tdCorr = document.createElement("td");
        var link = document.createElement("a");
        link.setAttribute("href", item.slug + ".html");
        link.textContent = (item.sender || item.recipient)
          ? ((item.sender || "—") + " → " + (item.recipient || "—"))
          : (item.header_label || ("Letter #" + item.sequence_index));
        tdCorr.appendChild(link);
        tr.appendChild(tdCorr);

        var tdDate = document.createElement("td");
        tdDate.textContent = item.dateline || "—";
        tr.appendChild(tdDate);

        var tdLang = document.createElement("td");
        tdLang.textContent = (item.languages || []).join(", ") || "—";
        tr.appendChild(tdLang);

        var tdIncipit = document.createElement("td");
        tdIncipit.textContent = item.incipit || "";
        tr.appendChild(tdIncipit);

        var tdPage = document.createElement("td");
        tdPage.textContent = "p. " + (item.start_folio || String(item.start_page));
        tr.appendChild(tdPage);

        tbody.appendChild(tr);
      });
    }

    fetch("index.json")
      .then(function (res) { return res.json(); })
      .then(function (doc) {
        letters = doc.letters || [];
        populateSelect(senderSel, uniqueSorted(letters.map(function (x) { return x.sender; })), "All Senders");
        populateSelect(recSel, uniqueSorted(letters.map(function (x) { return x.recipient; })), "All Recipients");
        var allLangs = [];
        letters.forEach(function (x) {
          (x.languages || []).forEach(function (lg) { allLangs.push(lg); });
        });
        populateSelect(langSel, uniqueSorted(allLangs), "All Languages");
        populateSelect(secSel, uniqueSorted(letters.map(function (x) { return x.section_title; })), "All Sections");
        renderRows();
      })
      .catch(function () {});

    [qInput, dateInput].forEach(function (el) {
      if (el) el.addEventListener("input", renderRows);
    });
    [senderSel, recSel, langSel, secSel].forEach(function (el) {
      if (el) el.addEventListener("change", renderRows);
    });
  }

  document.addEventListener("DOMContentLoaded", initCatalog);
})();
"""


def _render_letters_index_html(ast: CollectionAST, initial_rows_html: str, *, has_book_web: bool, has_proofing: bool) -> str:
    web_link = render_optional_links(((has_book_web, '<a href="../web/index.html">Complete Book Edition</a>'),))
    proof_link = render_optional_links(((has_proofing, '<a href="../proofing/index.html">Proofing Studio</a>'),))
    lang_attrs = html_lang_dir_attrs(ast)
    return f"""<!DOCTYPE html>
<html {lang_attrs}>
<head>
  <meta charset="utf-8" />
  <meta http-equiv="Content-Security-Policy" content="{html.escape(READER_STRICT_CSP, quote=True)}" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Correspondence Catalog — {html.escape(ast.title)}</title>
  <link rel="stylesheet" href="styles.css" />
</head>
<body>
  <header class="masthead">
    <span class="masthead-brand">{html.escape(ast.title)} · Individual Letters</span>
    <nav class="masthead-nav">
      {web_link}
      {proof_link}
    </nav>
  </header>
  <main class="edition-container">
    <h1>Interactive Correspondence Catalog</h1>
    <p id="letters-count-summary" style="font-family:var(--font-sans);font-size:0.84rem;color:var(--ink-muted);">
      {len(ast.letters)} letters indexed
    </p>
    <div class="catalog-filters" role="search">
      <label>Full-Text / Incipit Search
        <input type="search" id="filter-query" placeholder="Diacritic-insensitive (e.g. Rajput, depot)..." />
      </label>
      <label>Sender
        <select id="filter-sender"><option value="">All Senders</option></select>
      </label>
      <label>Recipient
        <select id="filter-recipient"><option value="">All Recipients</option></select>
      </label>
      <label>Dateline / Date
        <input type="search" id="filter-date" placeholder="Year, place, or date..." />
      </label>
      <label>Language
        <select id="filter-language"><option value="">All Languages</option></select>
      </label>
      <label>Section
        <select id="filter-section"><option value="">All Sections</option></select>
      </label>
    </div>
    <table class="catalog-table">
      <thead>
        <tr>
          <th>#</th>
          <th>Sender → Recipient</th>
          <th>Dateline</th>
          <th>Languages</th>
          <th>Incipit</th>
          <th>Folio</th>
        </tr>
      </thead>
      <tbody id="letters-catalog-tbody">
        {initial_rows_html}
      </tbody>
    </table>
  </main>
  <script src="letters.js" defer></script>
</body>
</html>
"""


def render_letters_edition(
    ast: CollectionAST,
    edition_dir: str | Path,
    *,
    has_book_web: bool = True,
    has_proofing: bool = True,
    has_local_scans: bool | None = None,
) -> dict[str, Any]:
    """Render Individual Online Letters Edition into `<edition_dir>/letters/`."""
    ed_root = Path(edition_dir)
    letters_dir = ed_root / "letters"
    shutil.rmtree(letters_dir, ignore_errors=True)
    letters_dir.mkdir(parents=True, exist_ok=True)
    local_scans_flag = resolve_has_local_scans(ed_root, has_local_scans)

    atomic_write_text(letters_dir / "styles.css", EDITORIAL_SHARED_CSS)
    atomic_write_text(letters_dir / "letters.js", LETTERS_CATALOG_JS)

    block_lookup = _index_blocks_by_id(ast)
    folio_to_page = build_folio_to_page_map(ast)
    sec_by_id = {s.section_id: s for s in ast.sections}

    catalog_records: list[dict[str, Any]] = []
    static_rows: list[str] = []

    for idx, ltr in enumerate(ast.letters):
        prev_ltr = (idx > 0 and ast.letters[idx - 1]) or None
        next_ltr = (idx + 1 < len(ast.letters) and ast.letters[idx + 1]) or None
        sec = sec_by_id.get(ltr.section_id)
        sec_stem = (sec and section_filename_stem(sec.section_id)) or "sec"
        sec_title = (sec and sec.title_plain) or ltr.section_id

        ltr_html = _render_standalone_letter_html(
            ast,
            ltr,
            prev_letter=prev_ltr,
            next_letter=next_ltr,
            sec_stem=sec_stem,
            block_lookup=block_lookup,
            folio_to_page=folio_to_page,
            has_book_web=has_book_web,
            has_proofing=has_proofing,
            has_local_scans=local_scans_flag,
        )
        atomic_write_text(letters_dir / f"{ltr.slug}.html", ltr_html)

        ltr_md = _render_letter_markdown(ltr, ast, block_lookup)
        atomic_write_text(letters_dir / f"{ltr.slug}.md", ltr_md)

        ltr_fns = [ast.footnotes[fid].model_dump(mode="json") for fid in ltr.footnote_ids if fid in ast.footnotes]
        ltr_json = {
            **ltr.model_dump(mode="json"),
            "footnotes_resolved": ltr_fns,
        }
        atomic_write_json(letters_dir / f"{ltr.slug}.json", ltr_json)

        full_plain = " ".join(strip_constrained_tags(b.tagged_text) for b in ltr.blocks)
        search_haystack = " ".join(
            p
            for p in (
                ltr.metadata.sender_display,
                ltr.metadata.recipient_display,
                ltr.metadata.dateline_plain,
                ltr.metadata.incipit,
                full_plain,
            )
            if p
        )
        hdr_lbl = _letter_header_label(ltr)
        corr_cell = (
            f"{ltr.metadata.sender_display or '—'} → {ltr.metadata.recipient_display or '—'}"
            if (ltr.metadata.sender_display or ltr.metadata.recipient_display)
            else (hdr_lbl or f"Letter #{ltr.sequence_index}")
        )
        catalog_records.append(
            {
                "letter_id": ltr.letter_id,
                "slug": ltr.slug,
                "sequence_index": ltr.sequence_index,
                "section_id": ltr.section_id,
                "section_title": sec_title,
                "sender": ltr.metadata.sender_display,
                "recipient": ltr.metadata.recipient_display,
                "header_label": hdr_lbl,
                "dateline": ltr.metadata.dateline_plain,
                "languages": ltr.metadata.languages,
                "incipit": ltr.metadata.incipit,
                "start_page": ltr.start_page,
                "end_page": ltr.end_page,
                "start_folio": ltr.start_folio,
                "boundary_confidence": ltr.boundary_confidence,
                "footnote_count": len(ltr_fns),
                "search_folded": fold_diacritics_for_search(search_haystack),
            }
        )
        static_rows.append(
            f"<tr><td>{ltr.sequence_index}</td>"
            f'<td><a href="{html.escape(ltr.slug, quote=True)}.html">'
            f"{html.escape(corr_cell)}</a></td>"
            f'<td>{html.escape(ltr.metadata.dateline_plain or "—")}</td>'
            f'<td>{html.escape(", ".join(ltr.metadata.languages) or "—")}</td>'
            f"<td>{html.escape(ltr.metadata.incipit[:90])}</td>"
            f"<td>p. {html.escape(ltr.start_folio or str(ltr.start_page))}</td></tr>"
        )

    atomic_write_json(
        letters_dir / "index.json",
        {"book_id": ast.book_id, "title": ast.title, "letter_count": len(catalog_records), "letters": catalog_records},
    )
    atomic_write_text(
        letters_dir / "index.html",
        _render_letters_index_html(ast, "".join(static_rows), has_book_web=has_book_web, has_proofing=has_proofing),
    )
    return {
        "letters_index": str(letters_dir / "index.html"),
        "letters_json": str(letters_dir / "index.json"),
        "letter_count": len(catalog_records),
    }
