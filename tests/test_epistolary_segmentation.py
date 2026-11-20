"""Adversarial epistolary boundary segmentation tests across 500 years of correspondence."""

from __future__ import annotations

import json
from pathlib import Path

from lanterns.core.models import CollectionAST, PageIR
from lanterns.epistolary.compiler import compile_collection_ast
from tests.conftest import make_block, make_page


def _compile_pages(
    pages: list[PageIR],
    *,
    book_id: str = "test-book",
    title: str = "Test Correspondence",
    book_dir: Path | None = None,
    letter_map: dict | None = None,
) -> CollectionAST:
    repo_root = book_dir.parents[1] if book_dir is not None else None
    return compile_collection_ast(
        pages,
        book_id=book_id,
        title=title,
        active_run_id="exp-001",
        repo_root=repo_root,
        letter_map=letter_map,
    )


class TestEpistolaryBoundarySegmentation:
    def test_three_short_letters_on_single_page_split_at_block_granularity(self) -> None:
        page = make_page(
            14,
            [
                make_block("p0014-b0000", 14, "running_header", "LETTERS TO SARDAR PATEL"),
                # Letter 1
                make_block("p0014-b0001", 14, "letter_header", "VIII", epistolary_transition="starts_new_letter"),
                make_block("p0014-b0002", 14, "dateline", "Sabarmati, June 3, 1928", alignment="right"),
                make_block("p0014-b0003", 14, "body", "Bhai Amritlal Thakkar will preside at Kathiavad."),
                make_block("p0014-b0004", 14, "signature", "BAPU", alignment="right"),
                # Letter 2 (telegram on same page)
                make_block("p0014-b0005", 14, "letter_header", "IX — TELEGRAM", epistolary_transition="starts_new_letter"),
                make_block("p0014-b0006", 14, "dateline", "Bardoli, June 5, 1928", alignment="right"),
                make_block("p0014-b0007", 14, "body", "REACH AHMEDABAD TOMORROW MORNING."),
                make_block("p0014-b0008", 14, "signature", "VALLABHBHAI", alignment="right"),
                # Letter 3 (short note on same page)
                make_block("p0014-b0009", 14, "salutation", "Chi. Manibehn,", epistolary_transition="starts_new_letter"),
                make_block("p0014-b0010", 14, "body", "Keep spun yarn accounts ready."),
                make_block("p0014-b0011", 14, "valediction", "Blessings from", alignment="right"),
                make_block("p0014-b0012", 14, "signature", "BAPU", alignment="right"),
                make_block("p0014-b0013", 14, "folio", "14"),
            ],
            printed_folio="14",
        )
        ast = _compile_pages([page], book_id="gandhi-patel")
        assert len(ast.letters) == 3
        assert ast.letters[0].block_ids == ["p0014-b0001", "p0014-b0002", "p0014-b0003", "p0014-b0004"]
        assert ast.letters[1].block_ids == ["p0014-b0005", "p0014-b0006", "p0014-b0007", "p0014-b0008"]
        assert ast.letters[2].block_ids == ["p0014-b0009", "p0014-b0010", "p0014-b0011", "p0014-b0012"]
        for ltr in ast.letters:
            assert ltr.start_page == 14 and ltr.end_page == 14

    def test_single_letter_spanning_four_pages_across_furniture(self) -> None:
        pages = [
            make_page(
                10,
                [
                    make_block("p0010-b0001", 10, "folio", "[ 8 ]"),
                    make_block("p0010-b0002", 10, "letter_header", "LETTER I.", epistolary_transition="starts_new_letter"),
                    make_block("p0010-b0003", 10, "salutation", "<sc>Sir</sc>,"),
                    make_block("p0010-b0004", 10, "body", "When I wrote my Treatiſe about our Syſtem...", continues_to_next=True),
                ],
                printed_folio="8",
            ),
            make_page(
                11,
                [
                    make_block("p0011-b0001", 11, "running_header", "LETTERS FROM SIR ISAAC NEWTON"),
                    make_block("p0011-b0002", 11, "folio", "[ 9 ]"),
                    make_block("p0011-b0003", 11, "body", "had an Eye upon ſuch Principles...", continues_from_prev=True, continues_to_next=True),
                ],
                printed_folio="9",
            ),
            make_page(
                12,
                [
                    make_block("p0012-b0001", 12, "running_header", "TO DOCTOR BENTLEY"),
                    make_block("p0012-b0002", 12, "folio", "[ 10 ]"),
                    make_block("p0012-b0003", 12, "body", "as might work with conſidering Men...", continues_from_prev=True),
                    make_block("p0012-b0004", 12, "body", "There is yet another Argument for a Deity."),
                ],
                printed_folio="10",
            ),
            make_page(
                13,
                [
                    make_block("p0013-b0001", 13, "folio", "[ 11 ]"),
                    make_block("p0013-b0002", 13, "valediction", "Your most humble Servant,", alignment="cascading", line_breaks_preserved=True),
                    make_block("p0013-b0003", 13, "signature", "IS. NEWTON.", alignment="right"),
                    make_block("p0013-b0004", 13, "dateline", "Cambridge, Dec. 10, 1692.", alignment="left"),
                ],
                printed_folio="11",
            ),
        ]
        ast = _compile_pages(pages, book_id="newton-bentley")
        assert len(ast.letters) == 1
        ltr = ast.letters[0]
        assert ltr.start_page == 10
        assert ltr.end_page == 13
        assert ltr.start_folio == "8"
        assert ltr.end_folio == "11"
        for blk in ltr.blocks:
            assert blk.type not in {"running_header", "folio"}

    def test_enclosed_inner_letter_stays_inside_parent_letter(self) -> None:
        page = make_page(
            30,
            [
                make_block("p0030-b0001", 30, "letter_header", "XX", epistolary_transition="starts_new_letter"),
                make_block("p0030-b0002", 30, "salutation", "Dear Vallabhbhai,"),
                make_block("p0030-b0003", 30, "body", "Mahadev has sent a letter from Belgaum prison. I enclose a copy:"),
                make_block(
                    "p0030-b0004",
                    30,
                    "blockquote",
                    "Belgaum Central Prison, May 2, 1933.\nRevered Bapu, All is well here.\nYours, Mahadev.",
                    epistolary_transition="starts_enclosure",
                    line_breaks_preserved=True,
                ),
                make_block(
                    "p0030-b0005",
                    30,
                    "body",
                    "You will see from the enclosure that his health has improved.",
                    epistolary_transition="ends_enclosure",
                ),
                make_block("p0030-b0006", 30, "signature", "BAPU", alignment="right"),
            ],
            printed_folio="20",
        )
        ast = _compile_pages([page], book_id="enclosure-test")
        assert len(ast.letters) == 1, f"Enclosed letter falsely split into {len(ast.letters)} letters!"
        ltr = ast.letters[0]
        assert ltr.metadata.has_enclosure is True
        assert len(ltr.blocks) == 6

    def test_bottom_dateline_postscript_and_address_block_preserve_exact_source_order(self) -> None:
        page = make_page(
            5,
            [
                make_block("p0005-b0001", 5, "letter_header", "Epistle 1.", epistolary_transition="starts_new_letter"),
                make_block("p0005-b0002", 5, "salutation", "Erasmus to Master Peter Winckel."),
                make_block("p0005-b0003", 5, "body", "<i>Scire poteras</i> me allusisse ad verum illius nomen."),
                make_block("p0005-b0004", 5, "valediction", "Farewell.", alignment="right"),
                make_block("p0005-b0005", 5, "signature", "ERASMUS.", alignment="right"),
                make_block("p0005-b0006", 5, "dateline", "Antwerp, 4 Nov. 1506.", alignment="left"),
                make_block("p0005-b0007", 5, "postscript", "P.S. — Greet Colet warmly if you cross to London."),
                make_block(
                    "p0005-b0008",
                    5,
                    "address_block",
                    "To the Worshipful Master Peter Winckel,\nGuardian at Gouda.",
                    line_breaks_preserved=True,
                ),
            ],
            printed_folio="41",
        )
        ast = _compile_pages([page], book_id="erasmus-order")
        assert len(ast.letters) == 1
        ltr = ast.letters[0]
        assert [b.type for b in ltr.blocks] == [
            "letter_header",
            "salutation",
            "body",
            "valediction",
            "signature",
            "dateline",
            "postscript",
            "address_block",
        ]
        assert ltr.metadata.dateline_position == "bottom"
        assert ltr.metadata.has_postscript is True
        assert ltr.metadata.address_block_tagged is not None

    def test_unchaptered_pure_epistolary_and_unheadered_informal_letters(self) -> None:
        # No heading blocks AND no letter_header blocks at all!
        pages = [
            make_page(
                1,
                [
                    make_block("p0001-b0001", 1, "dateline", "Weimar, 5th March, 1806.", alignment="right", epistolary_transition="starts_new_letter"),
                    make_block("p0001-b0002", 1, "body", "Ich habe mehrmals versucht dir zu schreiben, lieber Zelter."),
                    make_block("p0001-b0003", 1, "signature", "G.", alignment="right"),
                    make_block("p0001-b0004", 1, "salutation", "Beloved Naren,", epistolary_transition="starts_new_letter"),
                    make_block("p0001-b0005", 1, "body", "Work on fearlessly without looking back."),
                    make_block("p0001-b0006", 1, "signature", "Ever yours in the Lord,\nVIVEKANANDA", alignment="right", line_breaks_preserved=True),
                ],
                printed_folio="1",
            )
        ]
        ast = _compile_pages(pages, book_id="unchaptered-flow")
        assert len(ast.letters) == 2
        assert len(ast.sections) >= 1
        assert any(sec.kind == "epistolary_part" for sec in ast.sections)

    def test_interleaved_editorial_paragraphs_preserved_and_linked(self) -> None:
        page = make_page(
            66,
            [
                make_block("p0066-b0001", 66, "heading", "CHAPTER II.EARLY LETTERS.", heading_level=1),
                make_block("p0066-b0002", 66, "editorial", "During the autumn of 1812 Faraday began his correspondence with Benjamin Abbott."),
                make_block("p0066-b0003", 66, "dateline", "London, July 12, 1812.", alignment="right", epistolary_transition="starts_new_letter"),
                make_block("p0066-b0004", 66, "salutation", "Dear Abbott,"),
                make_block("p0066-b0005", 66, "body", "I have repeated the chlorine experiment this morning."),
                make_block("p0066-b0006", 66, "signature", "M. FARADAY", alignment="right"),
                make_block("p0066-b0007", 66, "editorial", "Shortly after sending this letter, Faraday waited upon Sir Humphry Davy."),
                make_block("p0066-b0008", 66, "dateline", "October 9, 1812.", alignment="right", epistolary_transition="starts_new_letter"),
                make_block("p0066-b0009", 66, "salutation", "Dear Friend,"),
                make_block("p0066-b0010", 66, "body", "Sir Humphry received me with great kindness."),
                make_block("p0066-b0011", 66, "signature", "M. FARADAY", alignment="right"),
            ],
            printed_folio="46",
        )
        ast = _compile_pages([page], book_id="faraday-life-letters")
        assert len(ast.letters) == 2
        # Neither editorial block may be absorbed into letter.block_ids
        for ltr in ast.letters:
            assert "p0066-b0002" not in ltr.block_ids
            assert "p0066-b0007" not in ltr.block_ids

        # Both editorial blocks must survive inside BookSection.items
        section_block_ids = [
            b.block_id
            for sec in ast.sections
            for item in sec.items
            if item.kind == "blocks"
            for b in item.blocks
        ]
        assert "p0066-b0002" in section_block_ids
        assert "p0066-b0007" in section_block_ids

        # Framing links on LetterUnits
        assert "p0066-b0002" in ast.letters[0].preceding_editorial_block_ids
        assert "p0066-b0007" in ast.letters[0].following_editorial_block_ids or "p0066-b0007" in ast.letters[1].preceding_editorial_block_ids

    def test_letter_map_force_and_forbid_split_overrides(self, tmp_path: Path) -> None:
        book_dir = tmp_path / "books" / "override-test"
        book_dir.mkdir(parents=True)
        overrides = {
            "schema_version": "1.0.0",
            "book_id": "override-test",
            "force_split_before_block_ids": ["p0002-b0003"],
            "forbid_split_before_block_ids": ["p0002-b0005"],
        }
        (book_dir / "letter_map.json").write_text(json.dumps(overrides), encoding="utf-8")

        page = make_page(
            2,
            [
                make_block("p0002-b0001", 2, "salutation", "Friend,", epistolary_transition="starts_new_letter"),
                make_block("p0002-b0002", 2, "body", "First note without signature."),
                # Model missed starts_new_letter here -> force_split_before_block_ids must split!
                make_block("p0002-b0003", 2, "body", "Second note started mid-page without header."),
                make_block("p0002-b0004", 2, "signature", "N.", alignment="right"),
                # Model falsely emitted starts_new_letter on a P.S. -> forbid_split_before_block_ids must keep attached!
                make_block("p0002-b0005", 2, "postscript", "P.S. Burn this.", epistolary_transition="starts_new_letter"),
            ],
            printed_folio="2",
        )
        ast = _compile_pages([page], book_id="override-test", book_dir=book_dir, letter_map=overrides)
        assert len(ast.letters) == 2
        assert ast.letters[0].block_ids == ["p0002-b0001", "p0002-b0002"]
        assert ast.letters[1].block_ids == ["p0002-b0003", "p0002-b0004", "p0002-b0005"]
        assert ast.letters[1].boundary_confidence == "human_mapped"

    def test_post_signature_enclosure_and_zero_salutation_zero_signature_multi_letters(self) -> None:
        # 1. Post-signature enclosure (Goethe exam_04_goethe_1803_p0026: GOETHE. -> ENCLOSURE.)
        goethe_page = make_page(
            26,
            [
                make_block("p0026-b0001", 26, "body", "Only a few words, to accompany the enclosed sheet.", epistolary_transition="starts_new_letter"),
                make_block("p0026-b0002", 26, "signature", "GOETHE.", alignment="right"),
                make_block(
                    "p0026-b0003",
                    26,
                    "heading",
                    "ENCLOSURE.",
                    alignment="center",
                    heading_level=3,
                    epistolary_transition="starts_enclosure",
                ),
                make_block("p0026-b0004", 26, "blockquote", "Enclosed reflection on musical dramatic rhythm."),
            ],
            printed_folio="12",
        )
        ast_goethe = _compile_pages([goethe_page], book_id="goethe-zelter")
        assert len(ast_goethe.letters) == 1, f"Post-signature enclosure split into {len(ast_goethe.letters)} letters!"
        assert ast_goethe.letters[0].metadata.has_enclosure is True
        assert len(ast_goethe.letters[0].blocks) == 4

        # 2. Three letters on one page with ZERO salutation and ZERO signature (Napoleon exam_02_napoleon_1795_p0033)
        napoleon_page = make_page(
            33,
            [
                make_block("p0033-b0001", 33, "letter_header", "[18.] Napoleon to Joseph.", epistolary_transition="starts_new_letter"),
                make_block("p0033-b0002", 33, "dateline", "Paris, Aug. 29, 1795.", alignment="right"),
                make_block("p0033-b0003", 33, "body", "Our affair with Milleli cannot be delayed."),
                make_block("p0033-b0004", 33, "letter_header", "[19.] Napoleon to Joseph.", epistolary_transition="starts_new_letter"),
                make_block("p0033-b0005", 33, "dateline", "Paris, Aug. 30, 1795.", alignment="right"),
                make_block("p0033-b0006", 33, "body", "Whatever happens, do not be uneasy."),
                make_block("p0033-b0007", 33, "letter_header", "[20.] Napoleon to Joseph.", epistolary_transition="starts_new_letter"),
                make_block("p0033-b0008", 33, "dateline", "Paris, Sept. 5, 1795.", alignment="right"),
                make_block("p0033-b0009", 33, "body", "Send me news of Fesch and our mother."),
            ],
            printed_folio="25",
        )
        ast_nap = _compile_pages([napoleon_page], book_id="napoleon-joseph")
        assert len(ast_nap.letters) == 3
        assert [ltr.block_ids[0] for ltr in ast_nap.letters] == ["p0033-b0001", "p0033-b0004", "p0033-b0007"]

        # 3. Mid-volume page slice (page_number > 1) opening with unsigned continuing letter prose + margin_note before next letter
        dost_page = make_page(
            219,
            [
                make_block("p0219-b0001", 219, "margin_note", "Æ<sc>t.</sc> 48]"),
                make_block("p0219-b0002", 219, "body", "the envelope with the post-mark intact."),
                make_block("p0219-b0003", 219, "letter_header", "LII", epistolary_transition="starts_new_letter"),
                make_block("p0219-b0004", 219, "body", "My attacks, after a long respite, are coming on again."),
            ],
            printed_folio="183",
        )
        ast_dost = _compile_pages([dost_page], book_id="dostoevsky-slice")
        assert len(ast_dost.letters) == 2
        assert ast_dost.letters[0].boundary_confidence == "ambiguous"
        assert ast_dost.letters[1].block_ids == ["p0219-b0003", "p0219-b0004"]

    def test_hybrid_essays_and_letters_preserves_essay_chapters_without_false_splits(self) -> None:
        pages = [
            make_page(
                9,
                [
                    make_block("p0009-b0001", 9, "heading", "PREFACE", heading_level=1),
                    make_block("p0009-b0002", 9, "body", "The articles in this volume belong to one period."),
                ],
                printed_folio="[ v ]",
            ),
            make_page(
                13,
                [
                    make_block("p0013-b0001", 13, "heading", "I\nINDUSTRY AND IDLENESS", heading_level=1),
                    make_block("p0013-b0002", 13, "body", "The above are the title and epigraph of a book."),
                    make_block("p0013-b0003", 13, "dateline", "[1888.]"),
                ],
                printed_folio="1",
            ),
            make_page(
                179,
                [
                    make_block("p0179-b0001", 179, "letter_header", "X\nLETTER TO PETER VERÍGIN", epistolary_transition="starts_new_letter"),
                    make_block("p0179-b0002", 179, "salutation", "<sc>Dear Brother</sc>,"),
                    make_block("p0179-b0003", 179, "body", "I. M. Tregoúbof has sent on to me your letter."),
                ],
                printed_folio="[ 167 ]",
            ),
            make_page(
                382,
                [
                    make_block("p0382-b0001", 382, "heading", "INDEX", heading_level=1),
                    make_block("p0382-b0002", 382, "index_entry", "<sc>Absolution</sc>, 282"),
                ],
                printed_folio="370",
            ),
        ]
        ast = _compile_pages(pages, book_id="hybrid-essays-letters")
        assert len(ast.letters) == 1
        assert ast.letters[0].start_page == 179
        assert [s.kind for s in ast.sections] == ["frontmatter", "chapter", "index"]

    def test_signed_roman_frontmatter_after_toc_remains_frontmatter(self) -> None:
        pages = [
            make_page(
                11,
                [
                    make_block("p0011-b0001", 11, "heading", "CONTENTS", heading_level=1),
                    make_block("p0011-b0002", 11, "toc_entry", "I. THE FIRST MEETING . . . . 1"),
                ],
                printed_folio="vii",
            ),
            make_page(
                13,
                [
                    make_block("p0013-b0001", 13, "heading", "FOREWORD", heading_level=1),
                    make_block("p0013-b0002", 13, "editorial", "This volume gathers the surviving letters."),
                    make_block("p0013-b0003", 13, "dateline", "Weimar, 1915.", alignment="left"),
                    make_block("p0013-b0004", 13, "signature", "THE EDITOR.", alignment="right"),
                ],
                printed_folio="x",
            ),
            make_page(
                15,
                [
                    make_block("p0015-b0001", 15, "heading", "INTRODUCTION", heading_level=1),
                    make_block("p0015-b0002", 15, "editorial", "An introductory essay on the friendship."),
                    make_block("p0015-b0003", 15, "signature", "THE AUTHOR.", alignment="right"),
                ],
                printed_folio="xvii",
            ),
            make_page(
                25,
                [
                    make_block("p0025-b0001", 25, "heading", "I\nTHE FIRST MEETING", heading_level=1),
                    make_block("p0025-b0002", 25, "editorial", "They met in November 1868."),
                    make_block("p0025-b0003", 25, "letter_header", "Author to Recipient.", epistolary_transition="starts_new_letter"),
                    make_block("p0025-b0004", 25, "body", "Pray come to see us soon."),
                    make_block("p0025-b0005", 25, "signature", "A.", alignment="right"),
                ],
                printed_folio="1",
            ),
        ]
        ast = _compile_pages(pages, book_id="signed-roman-fm-test")
        assert len(ast.letters) == 1
        assert ast.letters[0].start_page == 25
        assert [s.kind for s in ast.sections] == ["frontmatter", "frontmatter", "frontmatter", "chapter"]
