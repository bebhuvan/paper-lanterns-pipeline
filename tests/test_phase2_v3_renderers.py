"""Phase 2 v3 compliance tests: TEI P5 (`tei_all.rng` + CMIF), EPUB 3.3, DPUB-ARIA, WCAG, and Markdown."""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from lanterns.core.models import (
    ChildSpan,
    CorrespondentRef,
    make_boundary_anchor,
)
from lanterns.epistolary.compiler import compile_collection_ast
from lanterns.renderers.render_letters import render_letters_edition
from lanterns.renderers.render_markdown_tei import render_markdown_and_tei
from tests.conftest import make_block, make_page

REPO_ROOT = Path(__file__).resolve().parents[1]
TEI_RNG_PATH = REPO_ROOT / "workspace" / "tools" / "tei_all.rng"
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
