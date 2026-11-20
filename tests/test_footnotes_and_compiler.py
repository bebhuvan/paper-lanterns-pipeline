"""Adversarial tests for cross-page footnotes, resetting symbols, dual projection, and 100% block conservation."""

from __future__ import annotations

from lanterns.core.models import CollectionAST, PageIR, ValidationFinding
from lanterns.epistolary.compiler import compile_collection_ast
from lanterns.validation.rubric import score_collection_rubric
from lanterns.validation.validators import run_ast_validators
from tests.conftest import make_block, make_page


def _compile_pages(pages: list[PageIR], *, book_id: str = "fn-test") -> CollectionAST:
    return compile_collection_ast(
        pages,
        book_id=book_id,
        title="Footnote Apparatus Test",
        active_run_id="exp-fn",
    )


def _run_validators(ast: CollectionAST) -> list[ValidationFinding]:
    return run_ast_validators(ast)


class TestFootnotesAndCompiler:
    def test_cross_page_split_footnote_stitching(self) -> None:
        pages = [
            make_page(
                10,
                [
                    make_block("p0010-b0001", 10, "letter_header", "Epistle 12.", epistolary_transition="starts_new_letter"),
                    make_block("p0010-b0002", 10, "body", "Erasmus wrote from Louvain<sup>1</sup> to Bishop Fisher."),
                    make_block(
                        "p0010-b0003",
                        10,
                        "footnote",
                        "<sup>1</sup> See Allen, <i>Opus Epistolarum</i>, vol. iii, where the manuscript breaks-",
                        note_label="1",
                        continues_to_next=True,
                    ),
                ],
                printed_folio="10",
            ),
            make_page(
                11,
                [
                    make_block("p0011-b0001", 11, "body", "He remained there until autumn."),
                    make_block("p0011-b0002", 11, "signature", "ERASMUS.", alignment="right"),
                    make_block(
                        "p0011-b0003",
                        11,
                        "footnote",
                        "off after folio 48v and resumes in the Basel codex.",
                        note_label=None,
                        continues_from_prev=True,
                    ),
                ],
                printed_folio="11",
            ),
        ]
        ast = _compile_pages(pages)
        assert len(ast.footnotes) == 1
        fn = next(iter(ast.footnotes.values()))
        assert fn.origin_page == 10
        assert "Basel codex" in fn.plain_text
        assert "p0010-b0003" in fn.block_ids
        assert "p0011-b0003" in fn.block_ids
        assert fn.is_orphan is False

    def test_per_page_resetting_symbols_and_dual_footnote_projection(self) -> None:
        # Both page 5 (Letter 1) and page 6 (Letter 2) reuse '*' and '†' inside the SAME chapter!
        pages = [
            make_page(
                5,
                [
                    make_block("p0005-b0001", 5, "heading", "CHAPTER I.", heading_level=1),
                    make_block("p0005-b0002", 5, "letter_header", "Letter I.", epistolary_transition="starts_new_letter"),
                    make_block(
                        "p0005-b0003",
                        5,
                        "body",
                        "First letter cites star<sup>*</sup> and dagger<sup>†</sup>.",
                    ),
                    make_block("p0005-b0004", 5, "signature", "NEWTON.", alignment="right"),
                    make_block("p0005-b0005", 5, "footnote", "* Page 5 star note.", note_label="*"),
                    make_block("p0005-b0006", 5, "footnote", "† Page 5 dagger note.", note_label="†"),
                ],
                printed_folio="5",
            ),
            make_page(
                6,
                [
                    make_block("p0006-b0001", 6, "letter_header", "Letter II.", epistolary_transition="starts_new_letter"),
                    make_block(
                        "p0006-b0002",
                        6,
                        "body",
                        "Second letter also cites resetting star<sup>*</sup> and variant<sup>a</sup>.",
                    ),
                    make_block("p0006-b0003", 6, "signature", "BENTLEY.", alignment="right"),
                    make_block("p0006-b0004", 6, "footnote", "* Page 6 star note (distinct!).", note_label="*"),
                    make_block("p0006-b0005", 6, "footnote", "a Textual variant in MS.", note_label="a"),
                ],
                printed_folio="6",
            ),
        ]
        ast = _compile_pages(pages)
        assert len(ast.footnotes) == 4, f"Expected 4 distinct footnotes without symbol collision, got {len(ast.footnotes)}"
        assert len(ast.letters) == 2

        l1_fns = [ast.footnotes[fid] for fid in ast.letters[0].footnote_ids]
        l2_fns = [ast.footnotes[fid] for fid in ast.letters[1].footnote_ids]

        # Standalone Letter 1 gets ONLY page 5 notes; Standalone Letter 2 gets ONLY page 6 notes
        assert len(l1_fns) == 2
        assert len(l2_fns) == 2
        assert set(ast.letters[0].footnote_ids).isdisjoint(set(ast.letters[1].footnote_ids))
        assert any("Page 5 star note" in f.plain_text for f in l1_fns)
        assert any("Page 6 star note" in f.plain_text for f in l2_fns)

        # Complete Book Chapter gets ALL 4 footnotes in order!
        chapter = next(s for s in ast.sections if s.kind == "chapter")
        assert len(chapter.footnote_ids) == 4
        assert chapter.footnote_ids[:2] == ast.letters[0].footnote_ids
        assert chapter.footnote_ids[2:] == ast.letters[1].footnote_ids

    def test_orphan_footnote_preserved_and_flagged_not_dropped(self) -> None:
        page = make_page(
            8,
            [
                make_block("p0008-b0001", 8, "letter_header", "Letter III.", epistolary_transition="starts_new_letter"),
                make_block("p0008-b0002", 8, "body", "Body text where OCR missed superscript marker."),
                make_block("p0008-b0003", 8, "signature", "G.", alignment="right"),
                make_block("p0008-b0004", 8, "footnote", "‡ Orphan note at foot of page 8.", note_label="‡"),
            ],
            printed_folio="8",
        )
        ast = _compile_pages([page])
        assert len(ast.footnotes) == 1
        fn = next(iter(ast.footnotes.values()))
        assert fn.is_orphan is True
        # Must still be attached to section apparatus so zero printed words vanish!
        assert any(fn.footnote_id in sec.footnote_ids for sec in ast.sections)
        findings = ast.findings or _run_validators(ast)
        assert any("footnote" in f.code or "orphan" in f.code or "unlinked" in f.code for f in findings)

    def test_failed_closed_page_records_unavailable_page_and_blocks_provenance(self) -> None:
        pages = [
            make_page(
                1,
                [
                    make_block("p0001-b0001", 1, "letter_header", "I.", epistolary_transition="starts_new_letter"),
                    make_block("p0001-b0002", 1, "body", "Opening page."),
                ],
                printed_folio="1",
            ),
            make_page(2, [], printed_folio=None, failed_closed=True, failure_reason="Persistent RECITATION across all 3 tiers"),
        ]
        ast = _compile_pages(pages)
        all_unavail = [p for sec in ast.sections for p in sec.unavailable_pages]
        assert 2 in all_unavail
        rubric = ast.rubric or score_collection_rubric(ast)
        assert rubric.provenance_tier == "blocked"

    def test_source_block_coverage_validator_catches_single_dropped_block(self) -> None:
        page = make_page(
            3,
            [
                make_block("p0003-b0001", 3, "editorial", "Biographical bridge paragraph that MUST NOT be dropped."),
                make_block("p0003-b0002", 3, "salutation", "Dear Zelter,", epistolary_transition="starts_new_letter"),
                make_block("p0003-b0003", 3, "body", "Your setting of the poem arrived safely."),
                make_block("p0003-b0004", 3, "signature", "GOETHE", alignment="right"),
            ],
            printed_folio="3",
        )
        ast = _compile_pages([page])
        # Verify intact AST has 0 coverage blockers
        clean_findings = _run_validators(ast)
        assert not any(f.severity == "blocker" and "coverage" in f.code for f in clean_findings)

        # Adversarially drop the editorial block from BookSection.items!
        corrupted = ast.model_copy(deep=True)
        for sec in corrupted.sections:
            sec.items = [it for it in sec.items if it.kind != "blocks"]

        findings = _run_validators(corrupted)
        blockers = [f for f in findings if f.severity == "blocker" and ("coverage" in f.code or "p0003-b0001" in f.message)]
        assert len(blockers) >= 1, f"Expected blocker when editorial block dropped, got: {findings}"

    def test_bare_inline_symbol_callout_and_postscript_initialed_signature(self) -> None:
        # Regression A-04 (Napoleon p.33): full-height inline asterisk 'Chiapé*' without <sup>*</sup>
        # and spaced '; * ' callouts when page has matching footnote note_label='*' MUST link cleanly!
        nap_page = make_page(
            33,
            [
                make_block("p0033-b0001", 33, "letter_header", "[20.] Napoleon to Joseph.", epistolary_transition="starts_new_letter"),
                make_block("p0033-b0002", 33, "body", "Chiapé* is to join the army of the Alps."),
                make_block("p0033-b0003", 33, "footnote", "* Afterwards Director-General of the Telegraph.", note_label="*"),
            ],
            printed_folio="25",
        )
        ast_nap = _compile_pages([nap_page], book_id="napoleon-inline-sym")
        assert len(ast_nap.footnotes) == 1
        fn = next(iter(ast_nap.footnotes.values()))
        assert fn.is_orphan is False, "Bare word-attached 'Chiapé*' failed to link to same-page '*' footnote!"
        assert fn.footnote_id in ast_nap.letters[0].footnote_ids

        from lanterns.extraction.normalize import normalize_wire_to_page_ir

        spaced_page = normalize_wire_to_page_ir(
            {
                "printed_folio": "312",
                "blocks": [
                    {"type": "body", "bbox": [100, 100, 900, 200], "text": "“Therefore, let us have faith in our stellar friendship; * even though doomed.”"},
                    {"type": "footnote", "bbox": [100, 220, 900, 260], "text": "* From Joyful Wisdom.", "note_label": "*"},
                ],
            },
            page_number=336,
        )
        ast_spaced = _compile_pages([spaced_page], book_id="spaced-star-sym")
        assert len(ast_spaced.footnotes) == 1
        assert next(iter(ast_spaced.footnotes.values())).is_orphan is False

        # Regression A-03 (Bapu-Mira p.26): letter with main signature 'Bapu' + postscript + initialed 'B.'
        # must NOT trigger false `multiple_signatures_without_enclosure` warning!
        bapu_page = make_page(
            26,
            [
                make_block("p0026-b0001", 26, "letter_header", "7", epistolary_transition="starts_new_letter"),
                make_block("p0026-b0002", 26, "body", "You cannot complain of any brevity about this letter."),
                make_block("p0026-b0003", 26, "signature", "<sc>Bapu</sc>", alignment="right"),
                make_block("p0026-b0004", 26, "postscript", "I was uncertain about Rolland's passage."),
                make_block("p0026-b0005", 26, "signature", "B.", alignment="right"),
            ],
            printed_folio="16",
        )
        ast_bapu = _compile_pages([bapu_page], book_id="bapu-ps-sig")
        bapu_findings = _run_validators(ast_bapu)
        assert not any(f.code == "multiple_signatures_without_enclosure" for f in bapu_findings), (
            f"Initialed postscript signature falsely triggered multiple_signatures_without_enclosure: {bapu_findings}"
        )

    def test_same_page_multiparagraph_footnote_and_toc_legend_asterisk(self) -> None:
        toc_page = make_page(
            12,
            [
                make_block("p0012-b0001", 12, "toc_entry", "<sc>xvi.</sc><sup>*</sup><sc>Letters on Henry George</sc> . . 213"),
                make_block("p0012-b0002", 12, "editorial", "The articles marked with (*) were prohibited."),
            ],
            printed_folio="viii",
        )
        body_page = make_page(
            45,
            [
                make_block("p0045-b0001", 45, "letter_header", "X", epistolary_transition="starts_new_letter"),
                make_block("p0045-b0002", 45, "body", "People who take alcoholic drink<sup>*</sup> every day."),
                make_block("p0045-b0003", 45, "footnote", "* First paragraph of note.", note_label="*"),
                make_block("p0045-b0004", 45, "footnote", "Second paragraph of the same note.", note_label=None),
            ],
            printed_folio="33",
        )
        ast = _compile_pages([toc_page, body_page], book_id="multipar-fn")
        assert len(ast.footnotes) == 1
        fn = next(iter(ast.footnotes.values()))
        assert fn.is_orphan is False
        assert "Second paragraph" in fn.plain_text
        assert not any(f.code in {"unmatched_callout", "unlinked_footnote"} for f in ast.findings)
        assert not any(f.code in {"unmatched_callout", "unlinked_footnote"} for f in _run_validators(ast))
