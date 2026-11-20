"""Adversarial tests for Pydantic v2 models, JSON Schemas, diplomatic Unicode, and inline tags."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from lanterns.core.models import (
    BLOCK_ALIGNMENTS,
    BLOCK_TYPES,
    EPISTOLARY_TRANSITIONS,
    FURNITURE_BLOCK_TYPES,
    STYLE_TAGS,
    BlockIR,
    BookSection,
    CollectionAST,
    EpistolaryMetadata,
    FootnoteItem,
    LetterUnit,
    PageExtractWire,
    PageIR,
    RubricBreakdown,
    SectionItem,
    StyleSpan,
    TableCell,
    ValidationFinding,
    WireBlock,
)
from lanterns.core.tags import (
    escape_html,
    escape_xml,
    extract_inline_markers,
    parse_inline_tags,
    parse_tagged_text,
    serialize_tagged_text,
    validate_constrained_tags,
)
from lanterns.core.unicode_norm import (
    fold_for_search,
    normalize_diplomatic_text,
    strip_control_chars,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "schemas"


def _load_schema(name: str) -> dict[str, Any]:
    path = SCHEMAS_DIR / name
    assert path.exists(), f"Missing JSON schema: {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(data)
    return data


class TestModelsAndSchemaParity:
    def test_all_json_schemas_are_valid_draft_2020_12(self) -> None:
        expected = [
            "page_extract_wire.schema.json",
            "block_ir.schema.json",
            "collection_ast.schema.json",
            "letter_map.schema.json",
            "correction_v2.schema.json",
            "proof_ledger.schema.json",
        ]
        for filename in expected:
            schema = _load_schema(filename)
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    def test_wire_schema_matches_pydantic_enums_and_roundtrips(self) -> None:
        wire_schema = _load_schema("page_extract_wire.schema.json")
        item_props = wire_schema["properties"]["blocks"]["items"]["properties"]
        assert tuple(item_props["type"]["enum"]) == BLOCK_TYPES
        assert tuple(item_props["alignment"]["enum"]) == BLOCK_ALIGNMENTS
        assert tuple(item_props["epistolary_transition"]["enum"]) == EPISTOLARY_TRANSITIONS

        wire = PageExtractWire(
            printed_folio="[ 14 ]",
            page_orientation="upright",
            warning_codes=["archaic_orthography"],
            blocks=[
                WireBlock(
                    type="salutation",
                    bbox=[110, 140, 420, 175],
                    text="<sc>Sir</sc>,",
                    alignment="left",
                    epistolary_transition="starts_new_letter",
                    line_breaks_preserved=True,
                ),
                WireBlock(
                    type="body",
                    bbox=[110, 185, 880, 520],
                    text="The Reaſon why Matter evenly ſcattered<sup>*</sup>...",
                ),
                WireBlock(
                    type="table_row",
                    bbox=[110, 540, 880, 590],
                    text="180 | To Colet | Paris, 1504-5",
                    table_role="data",
                    cells=[TableCell(text="180"), TableCell(text="To Colet"), TableCell(text="Paris, 1504-5")],
                ),
            ],
        )
        payload = wire.model_dump()
        jsonschema.validate(instance=payload, schema=wire_schema)

    def test_collection_ast_validates_against_schema(self) -> None:
        ast_schema = _load_schema("collection_ast.schema.json")
        block = BlockIR(
            block_id="p0003-b0001",
            page_number=3,
            reading_order=0,
            printed_folio="1",
            type="body",
            bbox=[100, 150, 900, 400],
            tagged_text="<i>Scire poteras</i><sup>1</sup> — M—— [? May 1849]",
            plain_text="Scire poteras1 — M—— [? May 1849]",
            spans=[StyleSpan(tag="i", start=0, end=13), StyleSpan(tag="sup", start=13, end=14)],
        )
        page = PageIR(page_number=3, printed_folio="1", blocks=[block], provider="gemini", model="gemini-4-hydrogen")
        fn = FootnoteItem(
            footnote_id="fn-p0003-1",
            label="1",
            origin_page=3,
            origin_folio="1",
            block_ids=["p0003-b0002"],
            tagged_text="Ep. xxxi. 48.",
            plain_text="Ep. xxxi. 48.",
            referenced_in_block_ids=["p0003-b0001"],
            referenced_in_letter_ids=["letter-0001"],
            section_id="sec-01",
        )
        letter = LetterUnit(
            letter_id="letter-0001",
            slug="0001-erasmus-to-winckel",
            sequence_index=1,
            section_id="sec-01",
            start_page=3,
            end_page=3,
            start_folio="1",
            end_folio="1",
            block_ids=["p0003-b0001"],
            blocks=[block],
            metadata=EpistolaryMetadata(
                sender_display="Erasmus",
                recipient_display="Peter Winckel",
                dateline_position="bottom",
                languages=["la", "en"],
                incipit="Scire poteras",
            ),
            footnote_ids=["fn-p0003-1"],
        )
        section = BookSection(
            section_id="sec-01",
            kind="chapter",
            title_tagged="CHAPTER I.",
            title_plain="CHAPTER I.",
            start_page=3,
            end_page=3,
            items=[SectionItem(kind="letter", letter_id="letter-0001")],
            footnote_ids=["fn-p0003-1"],
        )
        rubric = RubricBreakdown(
            total_score=96.5,
            text_and_diacritic_fidelity=29.0,
            epistolary_structure_and_layout=24.5,
            apparatus_and_footnote_integrity=19.0,
            completeness_and_page_coverage=15.0,
            compiled_output_validity=9.0,
            provenance_tier="machine_checked",
            blocker_count=0,
            hotspot_count=0,
        )
        ast_obj = CollectionAST(
            book_id="erasmus-epistles",
            collection="world-correspondence",
            title="The Epistles of Erasmus",
            author_or_editor="F. M. Nichols",
            year=1901,
            active_run_id="run-001",
            total_pages=1,
            pages=[page],
            sections=[section],
            letters=[letter],
            footnotes={"fn-p0003-1": fn},
            findings=[ValidationFinding(code="sample_info", severity="info", message="All clean")],
            rubric=rubric,
            model_breakdown={"gemini/gemini-4-hydrogen": 1},
            total_spend_usd=0.0042,
        )
        jsonschema.validate(instance=ast_obj.model_dump(), schema=ast_schema)

    def test_invalid_block_type_or_style_tag_rejected(self) -> None:
        assert FURNITURE_BLOCK_TYPES == frozenset({"running_header", "folio", "scanner_residue", "noise"})
        assert STYLE_TAGS == ("i", "b", "sc", "sup", "sub", "unclear")
        with pytest.raises(ValidationError):
            WireBlock(type="invented_block_type", bbox=[0, 0, 10, 10], text="bad")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            StyleSpan(tag="span", start=0, end=4)  # type: ignore[arg-type]


class TestDiplomaticUnicodeNormalization:
    def test_decomposes_presentation_ligatures_only(self) -> None:
        raw = "o\ufb00er \ufb01rst \ufb02ow o\ufb03ce ba\ufb04e \ufb05ate \ufb06ate"
        out = normalize_diplomatic_text(raw)
        assert out == "offer first flow office baffle \u017ftate state"

    def test_strictly_preserves_long_s_and_lexical_ligatures(self) -> None:
        archaic = "Becauſe Hypotheſis Cæſar Æt. 39. Hors-d’œuvre encyclopædia"
        out = normalize_diplomatic_text(archaic)
        assert "ſ" in out
        assert "Becauſe Hypotheſis" in out
        assert "Cæſar" in out
        assert "Æt. 39." in out
        assert "œuvre" in out
        assert "encyclopædia" in out

    def test_preserves_hunterian_iast_european_and_multilingual_scripts(self) -> None:
        nfd_samples = [
            unicodedata.normalize("NFD", "Rájput Śiva Kṛṣṇa saṃsāra duḥkha ṛṣi"),
            unicodedata.normalize("NFD", "Größe für Zelter über Straße"),
            unicodedata.normalize("NFD", "depôt vs dépôt — naïveté — garçon"),
            "सत्यमेव जयते — साबरमती आश्रम",
            "خطوط غالب — بسم الله الرحمن الرحيم",
            "রবীন্দ্রনাথ ঠাকুর",
            "Ἐρασμος — Достоевский",
            "Count *** to M—— [? May 1849] [sic]",
        ]
        for sample in nfd_samples:
            out = normalize_diplomatic_text(sample)
            expected_nfc = unicodedata.normalize("NFC", sample)
            assert out == expected_nfc, f"Mismatch for {sample!r}: got {out!r}, expected {expected_nfc!r}"

        assert normalize_diplomatic_text("depôt") == "depôt"
        assert normalize_diplomatic_text("dépôt") == "dépôt"
        assert normalize_diplomatic_text("depôt") != normalize_diplomatic_text("dépôt")

    def test_search_folding_and_control_char_stripping(self) -> None:
        assert fold_for_search("Rájput") == "rajput"
        assert fold_for_search("depôt") == "depot"
        assert fold_for_search("Becauſe Cæſar") == "because caesar"

        cleaned, hit = strip_control_chars({"a": "clean\x00text\x07\nline2"})
        assert hit is True
        assert cleaned == {"a": "cleantext\nline2"}


class TestConstrainedInlineTags:
    def test_parses_all_six_allowed_tags_and_nested_spans(self) -> None:
        tagged = "<i>My dear <sc>Bapu</sc></i>, H<sub>2</sub>O<sup>*</sup> <b>urgent</b> <unclear>faded</unclear>"
        canonical_tagged, plain, spans = parse_tagged_text(tagged)
        plain2, spans2 = parse_inline_tags(tagged)
        assert plain == "My dear Bapu, H2O* urgent faded"
        assert plain2 == plain
        assert spans2 == spans
        by_tag = {(s.tag, plain[s.start : s.end]) for s in spans}
        assert ("i", "My dear Bapu") in by_tag
        assert ("sc", "Bapu") in by_tag
        assert ("sub", "2") in by_tag
        assert ("sup", "*") in by_tag
        assert ("b", "urgent") in by_tag
        assert ("unclear", "faded") in by_tag
        assert serialize_tagged_text(plain, spans) == canonical_tagged

    def test_rejects_disallowed_tags_and_distinguishes_ordinals_from_footnote_callouts(self) -> None:
        bad = validate_constrained_tags("<i>ok</i> <script>alert(1)</script> <div class='x'>no</div>")
        assert bad == ["div", "script"]
        assert validate_constrained_tags("<i>ok</i> <sc>Sir</sc> <sup>†</sup>") == []

        # Ordinals and French abbreviations are NOT footnote callouts; * and 12 ARE!
        line = "Weimar, 5<sup>th</sup> March, M<sup>me</sup> de Staël<sup>*</sup> wrote at f<sup>o</sup> 4<sup>12</sup>."
        markers = extract_inline_markers(line)
        assert markers == ["*", "12"]

        assert escape_html("<script>&\"") == "&lt;script&gt;&amp;&quot;"
        assert "\x00" not in escape_xml("text\x00<tag>")
