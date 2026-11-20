"""Phase 2 v3 compliance tests: TEI P5 (`tei_all.rng` + CMIF), EPUB 3.3, DPUB-ARIA, WCAG, and Markdown."""

from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from lanterns.core.models import (
    ChildSpan,
    CorrespondentRef,
    TableCell,
    make_boundary_anchor,
)
from lanterns.epistolary.compiler import compile_collection_ast
from lanterns.renderers import render_complete_edition
from lanterns.renderers.render_letters import render_letters_edition
from lanterns.renderers.render_markdown_tei import render_markdown_and_tei
from lanterns.validation.compiled_gate import validate_compiled_edition
from tests.conftest import make_block, make_page

REPO_ROOT = Path(__file__).resolve().parents[1]
TEI_RNG_PATH = REPO_ROOT / "workspace" / "tools" / "tei_all.rng"
EPUBCHECK_JAR = REPO_ROOT / "workspace" / "tools" / "epubcheck-5.1.0" / "epubcheck.jar"
TEI_NS = "{http://www.tei-c.org/ns/1.0}"


def test_tei_p5_tessellation_unified_opener_closer_cmif_and_relaxng(tmp_path: Path) -> None:
    """Verify H5 TEI P5 rewrite: div tessellation, unified opener/closer, post-body closer/address,
    multi-paragraph postscript, per-line <lg><l>, CMIF <correspDesc>, dynamic xml:lang, and tei_all.rng."""
    b_sec = make_block("p0001-b0001", 1, "heading", "CHAPTER I. LIFE AND LETTERS", heading_level=1)
    b_bio1 = make_block("p0001-b0002", 1, "editorial", "Biographical narrative opening the chapter.<fnref>1</fnref>")
    b_lhdr = make_block("p0001-b0003", 1, "letter_header", "LETTER I.", epistolary_transition="starts_new_letter")
    b_date_top = make_block("p0001-b0004", 1, "dateline", "Calcutta, 18 Mars 1877.", alignment="right")
    b_sal = make_block("p0001-b0005", 1, "salutation", "Chère Mademoiselle,")
    b_addr_top = make_block("p0001-b0006", 1, "address_block", "Rue de Babylone 62\nParis")
    b_body = make_block("p0001-b0007", 1, "body", "Main letter prose before verse and enclosure.")
    b_verse = make_block("p0001-b0008", 1, "verse", "<i>First line of verse,</i>\n<i>Second line of verse.</i>")
    b_enc = make_block("p0001-b0009", 1, "blockquote", "Enclosed dispatch body.", epistolary_transition="starts_enclosure")
    b_body2 = make_block("p0001-b0010", 1, "body", "Outer letter resumes.", epistolary_transition="ends_enclosure")
    b_val = make_block("p0001-b0011", 1, "valediction", "Croyez-moi,\nVotre dévouée,", alignment="cascading", line_breaks_preserved=True)
    b_sig = make_block("p0001-b0012", 1, "signature", "<sc>Toru Dutt</sc>.", alignment="right")
    b_date_bot = make_block("p0001-b0013", 1, "dateline", "19 Mars.", alignment="left")
    b_addr_bot = make_block("p0001-b0014", 1, "address_block", "À Mlle Clarisse Bader")
    b_ps1 = make_block("p0001-b0015", 1, "postscript", "P.S. First paragraph of postscript.")
    b_ps2 = make_block("p0001-b0016", 1, "body", "Second paragraph continuing the postscript.")
    b_ps_sig = make_block("p0001-b0017", 1, "signature", "T. D.", alignment="right")
    b_bio2 = make_block("p0001-b0018", 1, "editorial", "Inter-letter biographer commentary after Letter I.")
    b_margin = make_block("p0001-b0019", 1, "margin_note", "1877. Æt. 21.", bbox=[40, 200, 150, 240])
    b_fn1 = make_block("p0001-b0020", 1, "footnote", "First paragraph of note.", note_label="1")
    b_fn1_cont = make_block("p0001-b0021", 1, "footnote", "Second paragraph of the same note.", note_label=None)

    b_l2 = make_block("p0002-b0001", 2, "letter_header", "LETTER II.", epistolary_transition="starts_new_letter")
    b_l2_body = make_block("p0002-b0002", 2, "body", "Anonymous dispatch with unrecorded correspondents.")

    pages = [
        make_page(
            1,
            [
                b_sec,
                b_bio1,
                b_lhdr,
                b_date_top,
                b_sal,
                b_addr_top,
                b_body,
                b_verse,
                b_enc,
                b_body2,
                b_val,
                b_sig,
                b_date_bot,
                b_addr_bot,
                b_ps1,
                b_ps2,
                b_ps_sig,
                b_bio2,
                b_margin,
                b_fn1,
                b_fn1_cont,
            ],
            printed_folio="1",
        ),
        make_page(2, [b_l2, b_l2_body], printed_folio="2"),
    ]

    ast = compile_collection_ast(pages, book_id="tei-v3-test", title="TEI V3 Test")
    ast.language = "fr"
    ltr1 = ast.letters[0]
    ltr1.metadata.senders = [CorrespondentRef(name="Toru Dutt", kind="person")]
    ltr1.metadata.recipients = [CorrespondentRef(name="Académie Française", kind="org")]
    ltr1.metadata.place = "Calcutta"
    ltr1.metadata.date_iso = "1877-03-18"
    ltr1.children = [
        ChildSpan(
            child_id="child-0001",
            kind="enclosure",
            start_anchor=make_boundary_anchor(b_enc),
            end_anchor=make_boundary_anchor(b_enc),
            block_ids=["p0001-b0009"],
        )
    ]

    ed_dir = tmp_path / "edition"
    render_markdown_and_tei(ast, ed_dir)
    render_letters_edition(ast, ed_dir, has_book_web=False, has_proofing=False)

    tei_path = ed_dir / "tei" / "tei-v3-test.tei.xml"
    tei_xml = tei_path.read_text(encoding="utf-8")
    root = ET.fromstring(tei_xml)

    # 1. Dynamic xml:lang and zero literal "Unknown" in <correspDesc>
    assert root.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") == "fr"
    assert "<persName>Unknown</persName>" not in tei_xml
    assert "<orgName>Académie Française</orgName>" in tei_xml
    assert "<placeName>Calcutta</placeName>" in tei_xml
    assert 'when="1877-03-18"' in tei_xml

    # 2. Section <div> tessellation: no <p>/<quote>/<table> direct children alongside child <div>s
    sec_div = root.find(f".//{TEI_NS}body/{TEI_NS}div")
    assert sec_div is not None
    direct_tags = [ch.tag.removeprefix(TEI_NS) for ch in list(sec_div)]
    assert direct_tags[0] == "head"
    assert set(direct_tags[1:]) <= {"div", "pb", "gap"}, f"Tessellation violated: {direct_tags}"
    # No duplicate section <head>
    assert tei_xml.count("CHAPTER I. LIFE AND LETTERS") == 1

    # 3. Unified <opener>, unified <closer> (with post-body <address>), and <postscript> absorbing b_ps2 + b_ps_sig
    letter_div = sec_div.find(f"./{TEI_NS}div[@type='letter']")
    assert letter_div is not None
    openers = letter_div.findall(f"./{TEI_NS}opener")
    closers = letter_div.findall(f"./{TEI_NS}closer")
    postscripts = letter_div.findall(f"./{TEI_NS}postscript")
    assert len(openers) == 1, "Multiple fragmented <opener> elements emitted"
    assert len(closers) == 1, "Multiple fragmented <closer> elements emitted"
    assert len(postscripts) == 1
    assert closers[0].find(f"./{TEI_NS}dateline") is not None
    assert closers[0].find(f"./{TEI_NS}address/{TEI_NS}addrLine") is not None
    assert len(postscripts[0].findall(f"./{TEI_NS}p")) == 2
    assert postscripts[0].find(f"./{TEI_NS}closer/{TEI_NS}signed") is not None

    # 4. Verse emitted as one <l> per metrical line (no <lb/> stuffed inside single <l>)
    lg = letter_div.find(f"./{TEI_NS}lg")
    assert lg is not None
    l_elems = lg.findall(f"./{TEI_NS}l")
    assert len(l_elems) == 2
    assert "<lb/>" not in ET.tostring(lg, encoding="unicode")

    # 5. Enclosure wrapped in <floatingText type="enclosure">
    ft = letter_div.find(f"./{TEI_NS}floatingText[@type='enclosure']")
    assert ft is not None

    # 6. Multi-paragraph footnotes in TEI (<note><p>..<p>..) and Markdown (4-space indent)
    note_el = sec_div.find(f".//{TEI_NS}div[@type='notes']/{TEI_NS}note")
    assert note_el is not None
    assert len(note_el.findall(f"./{TEI_NS}p")) == 2

    md_txt = (ed_dir / "markdown" / "tei-v3-test.md").read_text(encoding="utf-8")
    assert "First paragraph of note.\n\n    Second paragraph of the same note." in md_txt
    assert "> *[Margin: 1877. Æt. 21.]*" in md_txt

    # 7. Official W3C/TEI Consortium RelaxNG schema validation via xmllint
    if shutil.which("xmllint") and TEI_RNG_PATH.is_file():
        proc = subprocess.run(
            ["xmllint", "--noout", "--relaxng", str(TEI_RNG_PATH), str(tei_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"tei_all.rng validation failed:\n{proc.stderr}"
    else:
        pytest.skip("xmllint or tei_all.rng not present")


def _resolve_epubcheck_cmd(epub_path: Path) -> list[str] | None:
    if not EPUBCHECK_JAR.is_file():
        return None
    if shutil.which("java"):
        probe = subprocess.run(["java", "-version"], capture_output=True, check=False)
        if probe.returncode == 0:
            return ["java", "-jar", str(EPUBCHECK_JAR), str(epub_path)]
    if shutil.which("mise"):
        return ["mise", "x", "java@temurin-21.0.13+9.0.LTS", "--", "java", "-jar", str(EPUBCHECK_JAR), str(epub_path)]
    return None


def test_epub33_dpub_aria_wcag_nav011_tables_dark_css_and_epubcheck(tmp_path: Path) -> None:
    """Verify H6/H7/H8 EPUB 3.3, Web, and Letters: NAV-011 float/list/table page order, mid-table subheaders,
    DPUB-ARIA 1.1 multi-citation multi-paragraph footnotes, WCAG heading clamping, dark-mode CSS, RTL/series, and EPUBCheck."""
    # Page 1: Chapter heading, narrative with jumped H4 heading (must clamp to H2), letter opening + body continuing across p.2 plate to p.3
    p1_blocks = [
        make_block("p0001-b0001", 1, "heading", "PART I. PERSIAN DISPATCHES", heading_level=1),
        make_block("p0001-b0002", 1, "heading", "Historical Introduction", heading_level=4),
        make_block("p0001-b0003", 1, "editorial", "Biographical introduction citing note one.<fnref>1</fnref>"),
        make_block("p0001-b0004", 1, "letter_header", "DISPATCH I.", epistolary_transition="starts_new_letter"),
        make_block("p0001-b0005", 1, "salutation", "To the Commander,"),
        make_block(
            "p0001-b0006",
            1,
            "body",
            "This paragraph starts on leaf one and continues past the facsimile plate",
            continues_to_next=True,
        ),
        make_block("p0001-b0007", 1, "footnote", "First paragraph of shared note.", note_label="1"),
        make_block("p0001-b0008", 1, "footnote", "Second paragraph of shared note.", note_label=None),
    ]
    # Page 2: Full-page facsimile plate interrupting continued prose
    p2_blocks = [
        make_block("p0002-b0001", 2, "facsimile_plate", "Facsimile of Persian seal"),
    ]
    # Page 3: Continued prose citing same footnote 1 a second time + verse + signature + start of multi-page list
    p3_blocks = [
        make_block(
            "p0003-b0001",
            3,
            "body",
            "onto leaf three with second citation.<fnref>1</fnref>",
            continues_from_prev=True,
        ),
        make_block("p0003-b0002", 3, "verse", "Line one in roman type\nLine two in roman type"),
        make_block("p0003-b0003", 3, "margin_note", "Right shoulder gloss", bbox=[640, 120, 760, 180]),
        make_block("p0003-b0004", 3, "list_item", "First supply item on leaf three"),
    ]
    # Page 4: Continued list item + table with leading header, body row, mid-table subheader, and body row on p.5
    p4_blocks = [
        make_block("p0004-b0001", 4, "list_item", "Second supply item turning onto leaf four"),
        make_block(
            "p0004-b0002",
            4,
            "table_row",
            "District | Revenue",
            table_role="header",
            cells=[TableCell(text="District"), TableCell(text="Revenue")],
        ),
        make_block(
            "p0004-b0003",
            4,
            "table_row",
            "Mysore | 12,000",
            table_role="body",
            cells=[TableCell(text="Mysore"), TableCell(text="12,000")],
        ),
        make_block(
            "p0004-b0004",
            4,
            "table_row",
            "Northern Provinces | Subtotal",
            table_role="header",
            cells=[TableCell(text="Northern Provinces", colspan=2)],
        ),
    ]
    p5_blocks = [
        make_block(
            "p0005-b0001",
            5,
            "table_row",
            "Bednore | 8,500",
            table_role="body",
            cells=[TableCell(text="Bednore"), TableCell(text="8,500")],
        ),
        make_block("p0005-b0002", 5, "signature", "Tipu Sultan", alignment="right"),
    ]

    pages = [
        make_page(1, p1_blocks, printed_folio="1"),
        make_page(2, p2_blocks, printed_folio="2"),
        make_page(3, p3_blocks, printed_folio="3"),
        make_page(4, p4_blocks, printed_folio="4"),
        make_page(5, p5_blocks, printed_folio="5"),
    ]
    ast = compile_collection_ast(pages, book_id="epub33-v3-test", title="Persian Dispatches Vol. II")
    ast.language = "fa"
    ast.text_direction = "rtl"
    ast.series_id = "persian-dispatches"
    ast.volume_number = 2
    # Link second citation on p0003-b0001 to the same footnote so multi-citation backlinks are exercised
    fn_only = next(iter(ast.footnotes.values()))
    if "p0003-b0001" not in fn_only.referenced_in_block_ids:
        fn_only.referenced_in_block_ids.append("p0003-b0001")

    ed_dir = tmp_path / "edition"
    res = render_complete_edition(ast, ed_dir, include_proofing=False)
    assert res["manifest"]["renderer_version"] == "3.0.0"

    epub_path = Path(res["epub"]["epub_path"])
    assert epub_path.is_file()

    with zipfile.ZipFile(epub_path, "r") as zf:
        opf_txt = zf.read("OEBPS/content.opf").decode("utf-8")
        nav_txt = zf.read("OEBPS/nav.xhtml").decode("utf-8")
        css_txt = zf.read("OEBPS/styles.css").decode("utf-8")
        sec_names = [n for n in zf.namelist() if n.startswith("OEBPS/sec-") and n.endswith(".xhtml")]
        assert len(sec_names) == 1
        sec_xhtml = zf.read(sec_names[0]).decode("utf-8")

    # 1. Dark-mode-safe EPUB CSS & diplomatic typography
    assert "#faf7f2" not in css_txt
    assert "#1a1816" not in css_txt
    assert "backdrop-filter" not in css_txt
    web_css = (ed_dir / "web" / "styles.css").read_text(encoding="utf-8")
    assert ".block-salutation { margin:" in web_css
    assert "white-space: pre-wrap;" in web_css

    # 2. RTL + Series + Accessibility OPF & Nav roles
    assert '<dc:language>fa</dc:language>' in opf_txt
    assert 'page-progression-direction="rtl"' in opf_txt
    assert 'property="belongs-to-collection"' in opf_txt
    assert 'property="group-position">2</meta>' in opf_txt
    assert 'property="schema:accessMode">textual</meta>' in opf_txt
    assert 'role="doc-toc"' in nav_txt
    assert 'role="doc-pagelist"' in nav_txt
    assert 'dir="rtl"' in sec_xhtml

    # 3. Strictly monotone #page-1..#page-5 order in XHTML DOM (NAV-011 regression check across float/list/table)
    sec_root = ET.fromstring(sec_xhtml.encode("utf-8"))
    dom_page_ids = [
        int(el.attrib["id"][5:])
        for el in sec_root.iter()
        if el.attrib.get("id", "").startswith("page-") and el.attrib["id"][5:].isdigit()
    ]
    assert dom_page_ids == [1, 2, 3, 4, 5], f"Non-monotone page anchors: {dom_page_ids}"

    # 4. Mid-table subheader preserved in <tbody> (not hoisted to <thead>)
    XHTML_NS = "{http://www.w3.org/1999/xhtml}"
    thead_rows = sec_root.findall(f".//{XHTML_NS}thead/{XHTML_NS}tr")
    tbody_rows = sec_root.findall(f".//{XHTML_NS}tbody/{XHTML_NS}tr")
    assert len(thead_rows) == 1
    assert len(tbody_rows) == 3
    assert tbody_rows[1].attrib.get("class") == "table-subheader"

    # 5. WCAG heading hierarchy (H4 clamped to H2 after H1) & DPUB-ARIA multi-citation multi-paragraph footnote
    heading_levels = [
        int(el.tag.removeprefix(XHTML_NS)[1:])
        for el in sec_root.iter()
        if el.tag.removeprefix(XHTML_NS) in {"h1", "h2", "h3", "h4", "h5", "h6"}
    ]
    for prev_h, cur_h in zip(heading_levels, heading_levels[1:], strict=False):
        assert cur_h <= prev_h + 1, f"WCAG heading skip detected: {heading_levels}"
    assert 'role="doc-noteref"' in sec_xhtml
    assert 'role="doc-footnote"' in sec_xhtml
    assert sec_xhtml.count('role="doc-backlink"') == 2

    # 6. Web HTML: zero invalid <li role="doc-footnote"> & full Life & Letters search index
    web_sec_files = list((ed_dir / "web" / "sections").glob("*.html"))
    web_sec_html = web_sec_files[0].read_text(encoding="utf-8")
    assert 'role="doc-footnote"' not in web_sec_html
    assert 'dir="rtl"' in web_sec_html
    assert "margin-right" in web_sec_html

    search_doc = json.loads((ed_dir / "web" / "search_index.json").read_text(encoding="utf-8"))
    kinds_indexed = {entry["kind"] for entry in search_doc["entries"]}
    assert "letter" in kinds_indexed
    assert kinds_indexed - {"letter"}, f"Non-letter units missing from search_index.json: {kinds_indexed}"

    # 7. Official W3C EPUBCheck v5.1.0 validation (0 fatals, 0 errors, 0 warnings)
    cmd = _resolve_epubcheck_cmd(epub_path)
    if cmd is not None:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        combined = f"{proc.stdout}\n{proc.stderr}"
        assert proc.returncode == 0, f"EPUBCheck failed:\n{combined}"
        assert "No errors or warnings detected" in combined, f"EPUBCheck emitted warnings:\n{combined}"
    else:
        pytest.skip("Java / EPUBCheck v5.1.0 not present")


def test_compiled_gate_strict_v3_standards_catches_planted_defects(tmp_path: Path) -> None:
    """Verify compiled_gate passes clean v3 editions (including external tools) and catches planted
    TEI tessellation, Unknown correspondent, <lb/> in <l>, EPUB NAV-011 / hardcoded theme, WCAG skip, and <li role='doc-footnote'>."""
    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "heading", "CHAPTER I", heading_level=1),
                make_block("p0001-b0002", 1, "letter_header", "LETTER I", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0003", 1, "body", "First leaf text.<fnref>1</fnref>"),
                make_block("p0001-b0004", 1, "footnote", "Note one.", note_label="1"),
            ],
            printed_folio="1",
        ),
        make_page(
            2,
            [
                make_block("p0002-b0001", 2, "body", "Second leaf text."),
            ],
            printed_folio="2",
        ),
    ]
    ast = compile_collection_ast(pages, book_id="gate-v3-test", title="Gate V3 Test")
    ed_dir = tmp_path / "edition"
    render_complete_edition(ast, ed_dir, include_proofing=True)

    has_ext = bool(shutil.which("xmllint") and TEI_RNG_PATH.is_file() and _resolve_epubcheck_cmd(ed_dir / "epub" / "gate-v3-test.epub"))
    clean_res = validate_compiled_edition(ed_dir, ast, require_proofing=True, strict_standards=True, run_external_tools=has_ext)
    assert clean_res["ok"], f"Clean v3 edition failed compiled_gate: {clean_res['errors']}"

    # Plant defects across Web HTML, TEI XML, and EPUB archive
    sec_html_path = next((ed_dir / "web" / "sections").glob("*.html"))
    sec_html_path.write_text(
        sec_html_path.read_text(encoding="utf-8")
        .replace('<h2 id="', '<h4 id="', 1)
        .replace('<li id="fn-', '<li role="doc-footnote" id="fn-', 1),
        encoding="utf-8",
    )

    tei_path = ed_dir / "tei" / "gate-v3-test.tei.xml"
    tei_orig = tei_path.read_text(encoding="utf-8")
    assert '<div xml:id="div-letter-' in tei_orig
    tei_bad = tei_orig.replace(
        '<div xml:id="div-letter-',
        '<p>Untessellated stray paragraph</p><div xml:id="div-letter-',
        1,
    ).replace(
        "</correspDesc>",
        '<correspAction type="sent"><persName>Unknown</persName></correspAction></correspDesc>',
        1,
    )
    tei_path.write_text(tei_bad, encoding="utf-8")

    epub_path = ed_dir / "epub" / "gate-v3-test.epub"
    tampered_epub = ed_dir / "epub" / "tampered.epub"
    with zipfile.ZipFile(epub_path, "r") as zin, zipfile.ZipFile(tampered_epub, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "OEBPS/styles.css":
                data = data + b"\nbody { background: #faf7f2; }\n"
            elif item.filename.startswith("OEBPS/sec-") and item.filename.endswith(".xhtml"):
                txt = data.decode("utf-8")
                txt = txt.replace('id="page-1"', 'id="page-TMP"').replace('id="page-2"', 'id="page-1"').replace('id="page-TMP"', 'id="page-2"')
                data = txt.encode("utf-8")
            zout.writestr(item, data)
    tampered_epub.replace(epub_path)

    bad_res = validate_compiled_edition(ed_dir, ast, require_proofing=True, strict_standards=True, run_external_tools=False)
    assert not bad_res["ok"]
    codes = {e["code"] for e in bad_res["errors"]}
    assert "heading_level_skip" in codes
    assert "invalid_li_doc_footnote" in codes
    assert "tei_div_tessellation_violation" in codes
    assert "tei_placeholder_unknown_correspondent" in codes
    assert "epub_hardcoded_paper_theme" in codes
    assert "epub_non_monotone_page_order" in codes


def test_heading_clamp_resets_after_h2_return(tmp_path: Path) -> None:
    """Returning to an h2 heading after an h3 must reset max_next_h so a later h3 does not jump to h4."""
    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "heading", "FRONTMATTER", heading_level=1),
                make_block("p0001-b0002", 1, "body", "Opening paragraph."),
                make_block("p0001-b0003", 1, "heading", "MAIN TITLE", heading_level=1),
                make_block("p0001-b0004", 1, "heading", "SUBTITLE", heading_level=2),
                make_block("p0001-b0005", 1, "heading", "ANOTHER TITLE", heading_level=1),
                make_block("p0001-b0006", 1, "heading", "SECTION DETAIL", heading_level=3),
                make_block("p0001-b0007", 1, "body", "Prose after headings."),
            ],
            printed_folio="1",
        )
    ]
    plan = {
        "sections": [
            {
                "kind": "frontmatter",
                "title_plain": "FRONTMATTER",
                "start_block_id": "p0001-b0001",
                "end_block_id": "p0001-b0007",
                "units": [
                    {"kind": "apparatus", "start_block_id": "p0001-b0001", "end_block_id": "p0001-b0007"}
                ],
            }
        ]
    }
    ast = compile_collection_ast(pages, book_id="heading-reset-test", title="Heading Reset Test", structure_plan=plan)
    ed_dir = tmp_path / "edition"
    render_complete_edition(ast, ed_dir, include_proofing=True)

    sec_html = next((ed_dir / "web" / "sections").glob("*.html")).read_text(encoding="utf-8")
    assert '<h2 id="p0001-b0005"' in sec_html
    assert '<h3 id="p0001-b0006"' in sec_html
    assert "<h4" not in sec_html

    gate = validate_compiled_edition(ed_dir, ast, require_proofing=True, strict_standards=True, run_external_tools=False)
    assert gate["ok"], gate["errors"]


def test_literal_bracket_page_crossref_not_parsed_as_pagebreak(tmp_path: Path) -> None:
    """Printed editorial cross-references like [p. 112] must stay literal text and never steal id='page-112'."""
    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "heading", "INTRODUCTION", heading_level=1),
                make_block("p0001-b0002", 1, "body", "As the editor noted (he wrote [p. 112], and continued)."),
            ],
            printed_folio="i",
        ),
        make_page(
            112,
            [
                make_block("p0112-b0001", 112, "letter_header", "LETTER X", epistolary_transition="starts_new_letter"),
                make_block("p0112-b0002", 112, "body", "Actual page 112 content."),
            ],
            printed_folio="112",
        ),
    ]
    ast = compile_collection_ast(pages, book_id="crossref-test", title="Crossref Test")
    ed_dir = tmp_path / "edition"
    render_complete_edition(ast, ed_dir, include_proofing=True)

    sec_html = next((ed_dir / "web" / "sections").glob("*.html")).read_text(encoding="utf-8")
    assert "he wrote [p. 112], and continued" in sec_html
    assert sec_html.count('id="page-112"') == 1

    tei_xml = (ed_dir / "tei" / "crossref-test.tei.xml").read_text(encoding="utf-8")
    assert "he wrote [p. 112], and continued" in tei_xml
    assert tei_xml.count('<pb xml:id="page-112"') == 1

    gate = validate_compiled_edition(ed_dir, ast, require_proofing=True, strict_standards=True, run_external_tools=False)
    assert gate["ok"], gate["errors"]


def test_cli_validate_edition_serializes_validation_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`lanterns validate-edition` must JSON-serialize ValidationFinding objects on failing editions."""
    from lanterns.cli import main as cli_main

    bad_dir = tmp_path / "bad_edition"
    bad_dir.mkdir(parents=True)
    rc = cli_main(["--root", str(tmp_path), "validate-edition", "--edition-dir", str(bad_dir)])
    assert rc == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["checked"] == 1
    assert payload["results"][0]["findings"]
