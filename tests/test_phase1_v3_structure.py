"""Phase 1 regression tests for <fnref> vs <sup>, facsimile_plate, structural AST, Pass 2A/2B/2C, and rebaser."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from lanterns.core.config import BookConfig, load_schema
from lanterns.core.models import (
    BookSection,
    BoundaryAnchor,
    ChildSpan,
    CollectionAST,
    CorrespondentRef,
    EpistolaryMetadata,
    LetterUnit,
    PageExtractWire,
    SectionItem,
    StructuralUnit,
)
from lanterns.core.paths import RepoPaths
from lanterns.core.tags import (
    extract_inline_markers,
    parse_tagged_text,
    strip_footnote_callouts_plain,
    strip_glued_footnote_digits,
)
from lanterns.epistolary.compiler import compile_collection_ast
from lanterns.extraction.normalize import normalize_wire_to_page_ir
from lanterns.renderers.html_utils import footnotes_by_block_map, render_block_stream_html
from lanterns.validation.validators import source_block_coverage
from tests.conftest import make_block, make_page


def test_fnref_vs_sup_preserves_abbreviations_and_glued_formula_tokens() -> None:
    # Typographic contractions must NEVER be treated or stripped as footnote callouts
    abbr_line = (
        "L<sup>d</sup> Cecil, Rob<sup>t</sup> Harley, Tho<sup>s</sup> More, "
        "Ja<sup>s</sup> Watt, Jn<sup>o</sup> Roe, Esq<sup>r</sup>, "
        "Capt<sup>n</sup> Drake, Gen<sup>l</sup> Wolfe wrote.<fnref>a</fnref>"
    )
    assert extract_inline_markers(abbr_line) == ["a"]
    assert strip_footnote_callouts_plain(abbr_line) == (
        "Ld Cecil, Robt Harley, Thos More, Jas Watt, Jno Roe, Esqr, Captⁿ Drake, Genˡ Wolfe wrote.".replace(
            "ⁿ", "n"
        ).replace("ˡ", "l")
    )

    # `<fnref>` always extracts even after punctuation/letters where `<sup>` is ambiguous
    tagged = "folio 48v<fnref>1</fnref> and variant<sup>1</sup><fnref>a</fnref>"
    assert extract_inline_markers(tagged) == ["1", "1", "a"]
    _, plain, spans = parse_tagged_text(tagged)
    assert plain == "folio 48v1 and variant1a"
    assert any(sp.tag == "fnref" and plain[sp.start : sp.end] == "a" for sp in spans)

    # Uppercase postal/chemical/shelfmark tokens must stay intact while prose callouts clean cleanly
    sample = "CO2, SO4, H2O, S.W.1, W.C.2, EC1, MS2, B12, John Tucker3, Sackea1 in 1616.1, I610"
    assert strip_glued_footnote_digits(sample) == (
        "CO2, SO4, H2O, S.W.1, W.C.2, EC1, MS2, B12, John Tucker, Sackea in 1616., I610"
    )


def test_facsimile_plate_schema_wire_rendering_and_legacy_sup_fallback() -> None:
    wire_payload = {
        "printed_folio": "58",
        "page_orientation": "upright",
        "warning_codes": ["mixed_scripts"],
        "blocks": [
            {
                "type": "letter_header",
                "bbox": [100, 40, 900, 90],
                "text": "XI.<fnref>*</fnref> To Miss Martin.",
                "epistolary_transition": "starts_new_letter",
            },
            {
                "type": "facsimile_plate",
                "bbox": [120, 120, 880, 620],
                "text": "[Holograph Bengali MS leaf signed Sri Torulata.]",
            },
            {
                "type": "caption",
                "bbox": [180, 630, 820, 680],
                "text": "<i>Facsimile of Autograph Letter.</i>",
            },
            {
                "type": "body",
                "bbox": [100, 700, 900, 820],
                "text": "Note this legacy variant<sup>a</sup> reading.",
            },
            {
                "type": "footnote",
                "bbox": [100, 850, 900, 910],
                "text": "* Communicated by Mrs. Baker.",
                "note_label": "*",
            },
            {
                "type": "footnote",
                "bbox": [100, 915, 900, 960],
                "text": "a MS reads <i>mon amie</i>.",
                "note_label": "a",
            },
        ],
    }
    wire = PageExtractWire.model_validate(wire_payload)
    assert wire.blocks[1].type == "facsimile_plate"

    p58 = normalize_wire_to_page_ir(json.loads(wire.model_dump_json()), page_number=58)
    ast = compile_collection_ast([p58], book_id="facsimile-fallback-book")
    assert len(ast.footnotes) == 2
    assert all(not fn.is_orphan for fn in ast.footnotes.values())
    assert [b.type for b in ast.letters[0].blocks] == ["letter_header", "facsimile_plate", "caption", "body"]

    # Ensure facsimile_plate + caption remain in section flow as <figure class="... block-facsimile-plate">
    p_blocks = make_page(
        1,
        [
            make_block("p0001-b0001", 1, "facsimile_plate", "MS Autograph Leaf"),
            make_block("p0001-b0002", 1, "caption", "<i>Plate I</i>"),
        ],
        printed_folio="1",
    )
    html_out = render_block_stream_html(
        p_blocks.blocks,
        fn_by_block=footnotes_by_block_map(ast.footnotes),
        available_footnotes=ast.footnotes,
        ref_registry={},
        seen_page_ids=set(),
        folio_to_page={p58.printed_folio or "58": 58},
    )
    assert '<figure id="p0001-b0001" class="archival-block block-facsimile-plate align-left">' in html_out


def test_structural_unit_ast_schema_book_config_and_units_conservation(tmp_path: Path) -> None:
    repo = RepoPaths.from_root(tmp_path)
    assert repo.structure_overrides_path("demo") == tmp_path / "books" / "demo" / "structure_overrides.json"
    assert repo.structure_auto_path(tmp_path / "run1") == tmp_path / "run1" / "ir" / "structure_auto.json"

    cfg = BookConfig(
        book_id="urdu-letters-vol2",
        title="Ghalib's Urdu Letters",
        language="ur",
        text_direction="rtl",
        series_id="ghalib-collected",
        volume_number=2,
    )
    cfg.save(paths=repo)
    loaded = BookConfig.load("urdu-letters-vol2", paths=repo)
    assert loaded.language == "ur"
    assert loaded.text_direction == "rtl"
    assert loaded.series_id == "ghalib-collected"
    assert loaded.volume_number == 2

    b1 = make_block("p0001-b0001", 1, "editorial", "Biographer context.")
    b2 = make_block("p0001-b0002", 1, "body", "Outer letter text.", epistolary_transition="starts_new_letter")
    b3 = make_block("p0001-b0003", 1, "blockquote", "Enclosed note.", epistolary_transition="starts_enclosure")
    page = make_page(1, [b1, b2, b3], printed_folio="1")

    a1 = BoundaryAnchor(page_number=1, block_id="p0001-b0001", bbox_y0=100.0, anchor_text_prefix="Biographer")
    a2 = BoundaryAnchor(page_number=1, block_id="p0001-b0002", bbox_y0=140.0, anchor_text_prefix="Outer")
    a3 = BoundaryAnchor(page_number=1, block_id="p0001-b0003", bbox_y0=180.0, anchor_text_prefix="Enclosed")

    child = ChildSpan(
        child_id="child-0001",
        kind="enclosure",
        start_anchor=a3,
        end_anchor=a3,
        block_ids=["p0001-b0003"],
        senders=[CorrespondentRef(name="East India Company", kind="org", role="sender")],
    )
    u_narr = StructuralUnit(
        unit_id="unit-0001",
        kind="narrative",
        start_anchor=a1,
        end_anchor=a1,
        block_ids=["p0001-b0001"],
    )
    u_let = StructuralUnit(
        unit_id="unit-0002",
        kind="letter",
        start_anchor=a2,
        end_anchor=a3,
        block_ids=["p0001-b0002", "p0001-b0003"],
        children=[child],
        senders=[CorrespondentRef(name="Mirza Ghalib", kind="person", role="sender")],
        place="Delhi",
        date_iso="1858-05-01",
    )
    lu = LetterUnit(
        letter_id="letter-0001",
        slug="letter-0001-ghalib",
        sequence_index=1,
        section_id="sec-01",
        start_page=1,
        end_page=1,
        block_ids=["p0001-b0002", "p0001-b0003"],
        blocks=[b2, b3],
        children=[child],
        metadata=EpistolaryMetadata(
            senders=u_let.senders,
            place="Delhi",
            date_iso="1858-05-01",
            has_enclosure=True,
        ),
    )
    sec = BookSection(
        section_id="sec-01",
        kind="chapter",
        title_tagged="Chapter I",
        title_plain="Chapter I",
        start_page=1,
        end_page=1,
        items=[SectionItem(kind="blocks", blocks=[b1]), SectionItem(kind="letter", letter_id="letter-0001", blocks=[b2, b3])],
        units=[u_narr, u_let],
    )
    ast_obj = CollectionAST(
        book_id="urdu-letters-vol2",
        title="Ghalib's Urdu Letters",
        language="ur",
        text_direction="rtl",
        series_id="ghalib-collected",
        volume_number=2,
        active_run_id="exp-01",
        total_pages=1,
        pages=[page],
        sections=[sec],
        letters=[lu],
        footnotes={},
    )
    jsonschema.validate(instance=ast_obj.model_dump(mode="json"), schema=load_schema("collection_ast"))
    assert source_block_coverage(ast_obj) == []

    # Missing block in sec.units or child block outside parent triggers blocker
    bad_ast = ast_obj.model_copy(deep=True)
    bad_ast.sections[0].units[1].block_ids = ["p0001-b0002"]
    codes = {f.code for f in source_block_coverage(bad_ast)}
    assert "structural_child_out_of_bounds" in codes and "source_block_dropped" in codes
