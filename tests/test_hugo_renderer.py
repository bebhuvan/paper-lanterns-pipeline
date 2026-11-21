"""Hugo website content renderer: deterministic, escape-safe runs, block bboxes, periods, and loud failures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lanterns.core.edition_overlay import EditionOverlay, LifePeriod
from lanterns.core.models import FootnoteItem
from lanterns.epistolary.compiler import compile_collection_ast
from lanterns.renderers.render_hugo import (
    HugoEditionMeta,
    HugoRenderError,
    build_hugo_bundle,
    collect_download_files,
    format_letter_date,
    page_kind_strip,
    render_hugo_edition,
    tagged_to_lines,
)
from tests.conftest import make_block, make_page


def _two_letter_ast():
    pages = [
        make_page(1, [make_block("p0001-b0001", 1, "heading", "THE LETTERS", heading_level=1)]),
        make_page(
            2,
            [
                make_block("p0002-b0001", 2, "letter_header", "I\n<i>To his Brother</i>", epistolary_transition="starts_new_letter"),
                make_block(
                    "p0002-b0002",
                    2,
                    "dateline",
                    "[<sc>From the Fortress</sc>],\n<i>December</i> 22, 1849.",
                    alignment="right",
                    line_breaks_preserved=True,
                    bbox=[440, 165, 780, 198],
                ),
                make_block("p0002-b0003", 2, "body", "A <b>bold</b> line & <script>x</script>.<sup>1</sup>", bbox=[40, 205, 800, 718]),
                make_block("p0002-b0004", 2, "signature", "F. <sc>D.</sc>", alignment="right"),
            ],
            printed_folio="53",
        ),
        make_page(
            3,
            [
                make_block("p0003-b0001", 3, "letter_header", "II", epistolary_transition="starts_new_letter"),
                make_block("p0003-b0002", 3, "body", "Second letter body."),
            ],
            printed_folio="54",
        ),
    ]
    ast = compile_collection_ast(pages, book_id="hugo-test", title="Hugo Test Letters")
    ast.archive_id = "hugotestarchive"
    ast.letters[0].metadata.date_iso = "1849-12-22"
    ast.letters[1].metadata.date_iso = "1854"
    ast.letters[1].metadata.header_tagged = "II\n<i>To Mme. N. D. Fonvisin</i><sup>2</sup>"
    ast.letters[0].metadata.salutation_tagged = "<sc>My dear Alexander Ivanovitch</sc><sup>2</sup>,"
    ast.footnotes["fn-p0002-1"] = FootnoteItem(
        footnote_id="fn-p0002-1",
        label="1",
        origin_page=2,
        block_ids=["p0002-b0009"],
        tagged_text="A <i>note</i>.",
        plain_text="A note.",
        referenced_in_block_ids=["p0002-b0003"],
        referenced_in_letter_ids=[ast.letters[0].letter_id],
    )
    ast.letters[0].footnote_ids = ["fn-p0002-1"]
    return ast


PERIODS = (
    LifePeriod(label="Petersburg", start="1837", end="1849-04-23", source="Table, p. xi"),
    LifePeriod(label="The Fortress", start="1849-04-23", end="1849-12-25", source="Table, p. xiii"),
)
OVERLAY = EditionOverlay(
    periods=PERIODS,
    name_forms={"F. D.": "Fyodor Dostoevsky", "his Brother": "his brother"},
    default_sender="Fyodor Dostoevsky",
    default_sender_source="Title page",
    translators=("Ethel Colburn Mayne",),
)


def test_bundle_letter_front_matter_carries_runs_bboxes_dates_and_linked_callouts() -> None:
    ast = _two_letter_ast()
    files = build_hugo_bundle(ast, HugoEditionMeta(overlay=OVERLAY))
    first = json.loads(files["1.md"])

    assert first["layout"] == "letter"
    assert first["heading"] == "I · To his Brother"
    assert first["title"] == "A bold line & <script>x</script>."
    assert first["stable_id"] == "p0002-b0001"
    assert first["pipeline_slug"] == ast.letters[0].slug
    assert first["letter_date"] == {"iso": "1849-12-22", "display": "22 December 1849", "short": "22 · XII · 1849"}
    assert first["period"] == "The Fortress"
    assert first["pages"] == [{"page": 2, "leaf": 1, "folio": "53"}]

    dateline = next(b for b in first["blocks"] if b["type"] == "dateline")
    assert dateline["bbox"] == [440, 165, 780, 198]
    assert dateline["lines"] == [
        [{"t": "["}, {"t": "From the Fortress", "c": "sc"}, {"t": "],"}],
        [{"t": "December", "c": "i"}, {"t": " 22, 1849."}],
    ]
    body = next(b for b in first["blocks"] if b["type"] == "body")
    runs = body["lines"][0]
    # Raw angle brackets stay literal text for the template to escape; no HTML is pre-rendered.
    assert "".join(r["t"] for r in runs) == "A bold line & <script>x</script>.1"
    assert {"t": "bold", "c": "b"} in runs
    assert runs[-1] == {"t": "1", "tag": "sup", "fn": "fn-p0002-1"}
    assert first["footnotes"][0]["lines"] == [[{"t": "A "}, {"t": "note", "c": "i"}, {"t": "."}]]

    assert first["salutation"] == "My dear Alexander Ivanovitch,"

    second = json.loads(files["2.md"])
    assert second["period"] == ""
    assert second["letter_date"]["display"] == "1854"
    assert second["heading"] == "II · To Mme. N. D. Fonvisin"


def test_book_front_matter_counts_periods_and_page_strip() -> None:
    ast = _two_letter_ast()
    book = json.loads(build_hugo_bundle(ast, HugoEditionMeta(overlay=OVERLAY))["_index.md"])
    assert book["layout"] == "book"
    assert book["counts"]["letters"] == 2
    assert book["year_counts"] == [[1849, 1], [1854, 1]]
    assert [(p["label"], p["count"]) for p in book["periods"]] == [("Petersburg", 0), ("The Fortress", 1)]
    assert book["unperiodised_count"] == 1
    assert book["page_kinds"] == page_kind_strip(ast)
    assert book["page_kinds"][1:] == "LL"
    assert book["scan_base"] == "https://archive.org/download/hugotestarchive/page/"


def test_unattested_sup_is_not_linked_and_build_is_deterministic() -> None:
    assert tagged_to_lines("x<sup>e</sup>", keep_line_breaks=False) == [[{"t": "x"}, {"t": "e", "tag": "sup"}]]
    ast = _two_letter_ast()
    meta = HugoEditionMeta(overlay=OVERLAY)
    assert build_hugo_bundle(ast, meta) == build_hugo_bundle(ast, meta)


@pytest.mark.parametrize("bad", ["22/12/1849", "1849-13-01", "1849-12-32"])
def test_malformed_dates_stop_the_run(bad: str) -> None:
    with pytest.raises(HugoRenderError):
        format_letter_date(bad)


def test_render_writes_fresh_bundle_and_manifest(tmp_path: Path) -> None:
    ast = _two_letter_ast()
    ed = tmp_path / "edition"
    for sub, name in (("epub", "hugo-test.epub"), ("markdown", "hugo-test.md"), ("tei", "hugo-test.tei.xml")):
        (ed / sub).mkdir(parents=True)
        (ed / sub / name).write_text(sub, encoding="utf-8")
    stale = ed / "hugo" / "letter-9999-stale.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")

    meta = HugoEditionMeta(downloads=collect_download_files(ed, "hugo-test"))
    info = render_hugo_edition(ast, ed, meta)

    assert info["page_count"] == 3
    assert not stale.exists()
    manifest = json.loads((ed / "hugo" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pages"] == ["1.md", "2.md", "_index.md"]
    assert [d["to"] for d in manifest["downloads"]] == ["hugo-test.epub", "hugo-test.md", "hugo-test.tei.xml"]


def test_missing_download_file_stops_the_run(tmp_path: Path) -> None:
    with pytest.raises(HugoRenderError):
        collect_download_files(tmp_path, "hugo-test")


def test_page_title_uses_full_names_and_default_writer_only_in_the_letters_section() -> None:
    ast = _two_letter_ast()
    ast.letters[0].metadata.sender_display = "F. D."
    ast.letters[0].metadata.recipient_display = "his Brother"
    ast.letters[1].metadata.sender_display = None
    ast.letters[1].metadata.recipient_display = None
    files = build_hugo_bundle(ast, HugoEditionMeta(overlay=OVERLAY))
    first, second = json.loads(files["1.md"]), json.loads(files["2.md"])
    assert first["page_title"] == "Fyodor Dostoevsky to his brother, 22 December 1849"
    assert first["sender"] == "F. D."  # the printed form is kept for the page itself
    in_letters_part = any(s.section_id == ast.letters[1].section_id and s.kind == "epistolary_part" for s in ast.sections)
    assert second["page_title"] == ("Fyodor Dostoevsky, 1854" if in_letters_part else "Letter, 1854")
    book = json.loads(files["_index.md"])
    assert book["translators"] == ["Ethel Colburn Mayne"]
