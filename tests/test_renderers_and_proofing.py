"""Adversarial end-to-end tests for multi-format renderers, Proofing Studio security, P1->P2 human sanctity, and compiled_gate."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from lanterns.core.models import CollectionAST
from lanterns.epistolary import compile_collection_ast, intake_proposal
from lanterns.renderers import render_complete_edition
from lanterns.validation.compiled_gate import validate_compiled_edition
from tests.conftest import make_block, make_page


def _build_sample_ast(repo_root: Path | None = None, *, include_failed_page: bool = False) -> CollectionAST:
    compile_fn = compile_collection_ast

    pages = [
        make_page(
            1,
            [
                make_block("p0001-b0001", 1, "heading", "CHAPTER I. EARLY LETTERS", heading_level=1),
                make_block("p0001-b0002", 1, "editorial", "Erasmus wrote from Antwerp to his guardian."),
                make_block("p0001-b0003", 1, "letter_header", "Epistle 1.", epistolary_transition="starts_new_letter"),
                make_block("p0001-b0004", 1, "salutation", "Erasmus to Master Peter Winckel."),
                make_block("p0001-b0005", 1, "body", "<i>Scire poteras</i><sup>*</sup> Rájput depôt Becauſe."),
                make_block("p0001-b0006", 1, "valediction", "Farewell.", alignment="cascading", line_breaks_preserved=True),
                make_block("p0001-b0007", 1, "signature", "<sc>Erasmus</sc>.", alignment="right"),
                make_block("p0001-b0008", 1, "dateline", "Antwerp, 4 Nov. 1506.", alignment="left"),
                make_block("p0001-b0009", 1, "footnote", "* Cf. Allen vol. i, p. 73.", note_label="*"),
            ],
            printed_folio="41",
        ),
        make_page(
            2,
            [
                make_block("p0002-b0001", 2, "editorial", "A second letter followed the next week."),
                make_block("p0002-b0002", 2, "letter_header", "Epistle 2.", epistolary_transition="starts_new_letter"),
                make_block("p0002-b0003", 2, "salutation", "Erasmus to Servatius."),
                make_block("p0002-b0004", 2, "body", "Pray do not suspect my affection<sup>†</sup>."),
                make_block("p0002-b0005", 2, "signature", "<sc>Erasmus</sc>.", alignment="right"),
                make_block("p0002-b0006", 2, "footnote", "† Servatius Rogerus at Stein.", note_label="†"),
            ],
            printed_folio="42",
        ),
    ]
    if include_failed_page:
        pages.append(make_page(3, [], printed_folio=None, failed_closed=True, failure_reason="Simulated timeout"))

    return compile_fn(
        pages,
        book_id="erasmus-sample",
        title="The Epistles of Erasmus",
        collection="world-correspondence",
        author_or_editor="F. M. Nichols",
        year=1901,
        active_run_id="run-render-test",
        repo_root=repo_root,
    )


def _render_full_edition(ast: CollectionAST, edition_dir: Path) -> None:
    render_complete_edition(ast, edition_dir, include_proofing=True)


class TestRenderersAndProofing:
    def test_end_to_end_multi_format_edition_and_compiled_gate(self, tmp_path: Path) -> None:
        ast = _build_sample_ast()
        edition_dir = tmp_path / "library" / "world-correspondence" / "erasmus-sample"
        _render_full_edition(ast, edition_dir)

        # 1. Whole-book Web HTML
        web_index = edition_dir / "web" / "index.html"
        assert web_index.is_file()
        sec_files = list((edition_dir / "web" / "sections").glob("*.html"))
        assert len(sec_files) >= 1
        sec_html = "\n".join(p.read_text(encoding="utf-8") for p in sec_files)
        assert "Becauſe" in sec_html
        assert "Rájput" in sec_html
        assert "depôt" in sec_html
        assert 'id="fn-' in sec_html and 'id="fnref-' in sec_html

        # 2. Individual Online Letters (self-contained footnotes + YAML frontmatter + JSON catalog)
        letters_index_json = json.loads((edition_dir / "letters" / "index.json").read_text(encoding="utf-8"))
        items = letters_index_json if isinstance(letters_index_json, list) else letters_index_json.get("letters", [])
        assert len(items) == 2

        for ltr in ast.letters:
            l_html_path = edition_dir / "letters" / f"{ltr.slug}.html"
            l_md_path = edition_dir / "letters" / f"{ltr.slug}.md"
            l_json_path = edition_dir / "letters" / f"{ltr.slug}.json"
            assert l_html_path.is_file()
            assert l_md_path.is_file()
            assert l_json_path.is_file()
            l_html = l_html_path.read_text(encoding="utf-8")
            assert 'href="#fn-' in l_html and 'href="#fnref-' in l_html
            md_text = l_md_path.read_text(encoding="utf-8")
            assert md_text.startswith("---\n")
            assert "slug:" in md_text

        # Verify Letter 1 HTML does NOT leak Letter 2's footnote!
        l1_html = (edition_dir / "letters" / f"{ast.letters[0].slug}.html").read_text(encoding="utf-8")
        assert "Allen vol. i" in l1_html
        assert "Servatius Rogerus at Stein" not in l1_html

        # 3. EPUB 3.3 structure & uncompressed mimetype first entry
        epub_path = edition_dir / "epub" / f"{ast.book_id}.epub"
        assert epub_path.is_file()
        with zipfile.ZipFile(epub_path, "r") as zf:
            names = zf.namelist()
            assert names[0] == "mimetype"
            info = zf.getinfo("mimetype")
            assert info.compress_type == zipfile.ZIP_STORED
            assert zf.read("mimetype") == b"application/epub+zip"
            assert "META-INF/container.xml" in names
            assert "OEBPS/content.opf" in names
            assert "OEBPS/nav.xhtml" in names
            ET.fromstring(zf.read("OEBPS/content.opf"))
            ET.fromstring(zf.read("OEBPS/nav.xhtml"))

        # 4. Scholarly Markdown & Well-formed TEI P5 XML with <correspDesc>
        md_book = edition_dir / "markdown" / f"{ast.book_id}.md"
        tei_book = edition_dir / "tei" / f"{ast.book_id}.tei.xml"
        assert md_book.is_file()
        assert tei_book.is_file()
        tei_xml = tei_book.read_text(encoding="utf-8")
        root = ET.fromstring(tei_xml)
        assert "correspDesc" in tei_xml
        xml_ids = [
            val
            for elem in root.iter()
            for attr, val in elem.attrib.items()
            if attr.endswith("id")
        ]
        assert len(xml_ids) == len(set(xml_ids)), f"Duplicate xml:id in TEI: {xml_ids}"

        # 5. Proofing Studio zero innerHTML & strict CSP
        proof_html = (edition_dir / "proofing" / "index.html").read_text(encoding="utf-8")
        proof_js = (edition_dir / "proofing" / "app.js").read_text(encoding="utf-8")
        assert "Content-Security-Policy" in proof_html
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            assert forbidden not in proof_js, f"Security violation: {forbidden} found in proofing/app.js!"
            assert forbidden not in proof_html, f"Security violation: {forbidden} found in proofing/index.html!"

        # 6. Compiled Gate passes with 0 errors on valid edition
        gate_res = validate_compiled_edition(edition_dir, ast, require_proofing=True)
        assert gate_res["ok"] is True, f"Compiled gate failed: {gate_res}"
        assert gate_res["error_count"] == 0

        # Adversarially inject innerHTML into proofing/app.js -> compiled_gate MUST fail!
        (edition_dir / "proofing" / "app.js").write_text(proof_js + "\n// el.innerHTML = 'x';\n", encoding="utf-8")
        bad_gate = validate_compiled_edition(edition_dir, ast, require_proofing=True)
        assert bad_gate["ok"] is False

    def test_failed_closed_page_renders_visible_unavailable_banner(self, tmp_path: Path) -> None:
        ast = _build_sample_ast(include_failed_page=True)
        edition_dir = tmp_path / "edition-unavail"
        _render_full_edition(ast, edition_dir)
        sec_html = "\n".join(p.read_text(encoding="utf-8") for p in (edition_dir / "web" / "sections").glob("*.html"))
        assert "page-unavailable" in sec_html or "Page 3 unavailable" in sec_html

    def test_p1_to_p2_signoff_enforces_distinct_verifier_and_protects_human_text(self, tmp_path: Path) -> None:
        # Set up synthetic repo_root with books/p1p2-book/
        (tmp_path / "books" / "p1p2-book").mkdir(parents=True)
        run1_pages = [
            make_page(
                1,
                [
                    make_block("p0001-b0001", 1, "salutation", "Sir,", epistolary_transition="starts_new_letter"),
                    make_block("p0001-b0002", 1, "body", "AI OCR typo: Induftry."),
                    make_block("p0001-b0003", 1, "signature", "NEWTON.", alignment="right"),
                ],
                printed_folio="1",
            )
        ]

        # P1 volunteer Alice fixes typo to diplomatic 'Induſtry.' and signs off p1_proofed
        p1_proposal = {
            "version": "correction-v2",
            "book_id": "p1p2-book",
            "reviewer": "alice_p1",
            "corrections": [
                {
                    "correction_id": "c-001",
                    "page_number": 1,
                    "block_id": "p0001-b0002",
                    "op": "set_text",
                    "replacement_tagged_text": "Human diplomatic fix: Induſtry.",
                    "reviewer": "alice_p1",
                    "source": "human",
                }
            ],
            "page_signoff": {
                "page_number": 1,
                "status": "p1_proofed",
                "reviewer": "alice_p1",
            },
        }
        p1_path = tmp_path / "p1.json"
        p1_path.write_text(json.dumps(p1_proposal), encoding="utf-8")
        res1 = intake_proposal("p1p2-book", p1_path, repo_root=tmp_path, pages=run1_pages)
        assert res1.get("ok", True) is True

        # Self-verification attempt by SAME user 'alice_p1' for P2 'verified' MUST fail/reject!
        same_user_p2 = {
            "version": "correction-v2",
            "book_id": "p1p2-book",
            "reviewer": "alice_p1",
            "corrections": [],
            "page_signoff": {
                "page_number": 1,
                "status": "verified",
                "reviewer": "alice_p1",
                "p1_reviewer": "alice_p1",
            },
        }
        bad_p2_path = tmp_path / "bad_p2.json"
        bad_p2_path.write_text(json.dumps(same_user_p2), encoding="utf-8")
        with pytest.raises((ValueError, RuntimeError)):
            res_bad = intake_proposal("p1p2-book", bad_p2_path, repo_root=tmp_path, pages=run1_pages)
            if isinstance(res_bad, dict) and res_bad.get("ok") is False:
                raise ValueError("Rejected self-verification as expected")

        # Distinct verifier 'bob_p2' succeeds!
        valid_p2 = {
            "version": "correction-v2",
            "book_id": "p1p2-book",
            "reviewer": "bob_p2",
            "corrections": [],
            "page_signoff": {
                "page_number": 1,
                "status": "verified",
                "reviewer": "bob_p2",
                "p1_reviewer": "alice_p1",
            },
        }
        ok_p2_path = tmp_path / "ok_p2.json"
        ok_p2_path.write_text(json.dumps(valid_p2), encoding="utf-8")
        intake_proposal("p1p2-book", ok_p2_path, repo_root=tmp_path, pages=run1_pages)

        # Now simulate a SUBSEQUENT AI model re-run that tries to emit different text OR a machine correction!
        machine_attempt = {
            "version": "correction-v2",
            "book_id": "p1p2-book",
            "generated_by": "verify-hotspots",
            "corrections": [
                {
                    "correction_id": "m-999",
                    "page_number": 1,
                    "block_id": "p0001-b0002",
                    "op": "set_text",
                    "replacement_tagged_text": "Machine overwrite attempt: Industry.",
                    "source": "machine",
                }
            ],
        }
        m_path = tmp_path / "machine.json"
        m_path.write_text(json.dumps(machine_attempt), encoding="utf-8")
        try:
            intake_proposal("p1p2-book", m_path, repo_root=tmp_path, pages=run1_pages)
        except (ValueError, RuntimeError):
            pass

        rerun_ai_pages = [
            make_page(
                1,
                [
                    make_block("p0001-b0001", 1, "salutation", "Sir,", epistolary_transition="starts_new_letter"),
                    make_block("p0001-b0002", 1, "body", "AI OCR typo: Induftry."),
                    make_block("p0001-b0003", 1, "signature", "NEWTON.", alignment="right"),
                ],
                printed_folio="1",
            )
        ]
        compiled = compile_collection_ast(
            rerun_ai_pages,
            book_id="p1p2-book",
            title="Newton Test",
            repo_root=tmp_path,
        )
        body_texts = [b.plain_text for ltr in compiled.letters for b in ltr.blocks if b.type == "body"]
        assert any("Human diplomatic fix: Induſtry." in t for t in body_texts), (
            f"Human-verified text was overwritten or lost! Got: {body_texts}"
        )

        # Verify EDITION_REPORT.md reflects Round P2 Verified count from ast.proof_summary
        ed_dir = tmp_path / "p1p2-edition"
        _render_full_edition(compiled, ed_dir)
        report_md = (ed_dir / "EDITION_REPORT.md").read_text(encoding="utf-8")
        assert "Round P2 Verified**: 1 page(s)" in report_md


def test_cross_page_paragraph_coalescing_and_heading_dedup(tmp_path: Path) -> None:
    from lanterns.epistolary.compiler import compile_collection_ast
    from lanterns.validation import validate_compiled_edition

    p1 = make_page(
        1,
        [
            make_block("p0001-b0001", 1, "heading", "LETTERS", heading_level=1),
            make_block("p0001-b0002", 1, "heading", "FROM", heading_level=2),
            make_block("p0001-b0003", 1, "heading", "A CAMP", heading_level=1),
            make_block("p0001-b0004", 1, "letter_header", "LETTER I", epistolary_transition="starts_new_letter"),
            make_block(
                "p0001-b0005",
                1,
                "body",
                "They reigned<sup>1</sup> formerly at",
                continues_to_next=True,
            ),
            make_block("p0001-b0006", 1, "footnote", "<sup>1</sup> First note.", note_label="1"),
        ],
        printed_folio="9",
    )
    p2 = make_page(
        2,
        [
            make_block("p0002-b0001", 2, "running_header", "10 LETTERS FROM A CAMP"),
            make_block(
                "p0002-b0002",
                2,
                "body",
                "Biana,<sup>1</sup> and possessed extensive territories.",
                continues_from_prev=True,
            ),
            make_block("p0002-b0003", 2, "signature", "THE AUTHOR.", alignment="right"),
            make_block("p0002-b0004", 2, "footnote", "<sup>1</sup> Second note.", note_label="1"),
        ],
        printed_folio="10",
    )
    ast = compile_collection_ast([p1, p2], book_id="join-book", title="Join Test")
    assert len(ast.sections) == 1
    assert ast.sections[0].title_plain == "LETTERS FROM A CAMP"

    ed_dir = tmp_path / "join-edition"
    _render_full_edition(ast, ed_dir)
    gate = validate_compiled_edition(ed_dir, ast, require_proofing=True)
    assert gate["ok"] is True, gate["errors"]

    md_text = (ed_dir / "markdown" / "join-book.md").read_text(encoding="utf-8")
    assert "## LETTERS FROM A CAMP" in md_text
    assert "### LETTERS\n" not in md_text
    assert "formerly at Biana,[^fn-p0002-1] and possessed" in md_text
    assert "[p. 10]" not in md_text
    assert "reigned[^fn-p0001-1] formerly" in md_text

    ltr_html = (ed_dir / "letters" / f"{ast.letters[0].slug}.html").read_text(encoding="utf-8")
    assert "10 LETTERS FROM A CAMP" not in ltr_html
    assert 'id="page-2"' in ltr_html
    assert ltr_html.count('class="archival-block block-body') == 1


def test_cross_page_coalescing_across_type_drift_and_illustration_plate(tmp_path: Path) -> None:
    from lanterns.epistolary.compiler import compile_collection_ast

    pages = [
        make_page(
            14,
            [
                make_block("p0014-b0001", 14, "heading", "PREFACE", heading_level=1),
                make_block(
                    "p0014-b0002",
                    14,
                    "editorial",
                    "who, in literature and",
                    continues_to_next=True,
                ),
            ],
            printed_folio="vi",
        ),
        make_page(
            15,
            [
                make_block(
                    "p0015-b0001",
                    15,
                    "body",
                    "friendship, made use of guest and friend.",
                    continues_from_prev=True,
                ),
            ],
            printed_folio="vii",
        ),
        make_page(
            16,
            [
                make_block(
                    "p0016-b0001",
                    16,
                    "body",
                    "Alamgir was a new indented paragraph.",
                    alignment="indented",
                    continues_from_prev=True,
                ),
            ],
            printed_folio="viii",
        ),
        make_page(
            44,
            [
                make_block("p0044-b0001", 44, "letter_header", "LETTER I", epistolary_transition="starts_new_letter"),
                make_block(
                    "p0044-b0002",
                    44,
                    "body",
                    "make a few false pro-",
                    continues_to_next=True,
                ),
            ],
            printed_folio="6",
        ),
        make_page(
            45,
            [
                make_block("p0045-b0001", 45, "illustration", ""),
                make_block("p0045-b0002", 45, "caption", "Portrait plate."),
            ],
            printed_folio="7",
        ),
        make_page(
            46,
            [
                make_block(
                    "p0046-b0001",
                    46,
                    "editorial",
                    "mises to lure them back.",
                    continues_from_prev=True,
                ),
                make_block("p0046-b0002", 46, "signature", "&c. &c.", alignment="right"),
            ],
            printed_folio="8",
        ),
    ]
    ast = compile_collection_ast(pages, book_id="drift-plate-book", title="Drift Plate Test")
    assert len(ast.letters) == 1
    assert ast.letters[0].metadata.letter_number_label == "I"
    assert ast.letters[0].metadata.sender_display is None

    ed_dir = tmp_path / "drift-edition"
    _render_full_edition(ast, ed_dir)
    md_text = (ed_dir / "markdown" / "drift-plate-book.md").read_text(encoding="utf-8")
    assert "literature and friendship, made use" in md_text
    assert "guest and friend.\n\nAlamgir was a new indented paragraph." in md_text
    assert "false promises to lure them back." in md_text
    assert "[p. vii]" not in md_text
    assert "[p. viii]" not in md_text
    assert "[p. 8]" not in md_text

    letters_idx = json.loads((ed_dir / "letters" / "index.json").read_text(encoding="utf-8"))
    assert letters_idx["letters"][0]["header_label"] == "LETTER I"
    ltr_html = (ed_dir / "letters" / f"{ast.letters[0].slug}.html").read_text(encoding="utf-8")
    assert '<h1 class="block-heading">LETTER I</h1>' in ltr_html
    assert "— → —" not in (ed_dir / "letters" / "index.html").read_text(encoding="utf-8")

