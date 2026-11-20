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


def test_pass2b_skeleton_never_compresses_quoted_or_colon_preceded_body_blocks() -> None:
    from lanterns.extraction.structure_pass import build_unit_skeleton

    blocks = [
        make_block("p0001-b0001", 1, "editorial", "In her bereavement she wrote as follows:"),
        make_block("p0001-b0002", 1, "body", "We are all overwhelmed by sorrow today."),
        make_block("p0001-b0003", 1, "body", "Paragraph one of plain letter prose."),
        make_block("p0001-b0004", 1, "body", "Paragraph two of plain letter prose."),
        make_block("p0001-b0005", 1, "body", "Paragraph three of plain letter prose."),
        make_block("p0001-b0006", 1, "body", "Paragraph four of plain letter prose."),
        make_block("p0001-b0007", 1, "body", "“Yes, I still hope to meet you soon,” wrote the sister."),
        make_block("p0001-b0008", 1, "signature", "Yours ever, Toru."),
    ]
    skel = "\n".join(build_unit_skeleton(blocks))
    # Block following trailing colon and quoted block MUST remain explicit in skeleton
    assert "p0001-b0002" in skel
    assert "p0001-b0007" in skel
    # Interior compressible run b3..b6 (4 eligible blocks) keeps b3 and b6 and compresses b4..b5
    assert "p0001-b0003" in skel
    assert "p0001-b0006" in skel
    assert "interior body paragraphs omitted (p0001-b0004..p0001-b0005)" in skel


def test_structure_pass_2a_2b_2c_with_starts_enclosure_and_hydrogen_escalation(tmp_path: Path) -> None:
    from lanterns.core.atomic import atomic_write_json
    from lanterns.core.cost import CostLedger
    from lanterns.extraction.structure_pass import (
        DEFAULT_ESCALATION_MODEL,
        DEFAULT_PASS2A_MODEL,
        DEFAULT_PASS2B_MODEL,
        run_structure_pass,
    )
    from lanterns.providers.base import DecisionAnswer, DecisionBatchResult, ProviderRequest, ProviderSuccess

    repo = RepoPaths.from_root(tmp_path)
    for d in ("prompts", "schemas", "config"):
        import shutil

        shutil.copytree(Path(__file__).resolve().parents[1] / d, tmp_path / d)
    BookConfig(book_id="canary-mock", title="Canary Mock", active_run_id="exp-01").save(paths=repo)

    pages = [
        make_page(1, [make_block("p0001-b0001", 1, "heading", "CHAPTER I", heading_level=1)]),
        make_page(
            2,
            [
                make_block("p0002-b0001", 2, "letter_header", "From A to B", epistolary_transition="starts_new_letter"),
                make_block("p0002-b0002", 2, "body", "Outer prose opening."),
                make_block("p0002-b0003", 2, "blockquote", "Enclosed petition from Bombay.", epistolary_transition="starts_enclosure"),
                make_block("p0002-b0004", 2, "body", "Petition closing.", epistolary_transition="ends_enclosure"),
                make_block("p0002-b0005", 2, "signature", "Yours faithfully, A."),
                make_block("p0002-b0006", 2, "dateline", "Calcutta, 4 June 1880.", epistolary_transition="starts_new_letter"),
                make_block("p0002-b0007", 2, "body", "Second letter missed by coarse pass."),
            ],
        ),
        make_page(3, [make_block("p0003-b0001", 3, "body", "Second letter continues here.")]),
    ]
    run_dir = repo.run_dir("canary-mock", "exp-01", kind="experiment")
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "ir" / "pages.json", {"pages": [p.model_dump(mode="json") for p in pages]})

    calls: list[tuple[str, str]] = []

    class _FakeRouterProvider:
        name = "mock"

        def __init__(self, route: str) -> None:
            self.route = route

        def generate(self, request: ProviderRequest) -> ProviderSuccess:
            calls.append(("generate", self.route))
            if "## Task: Pass 2A" in request.dynamic_suffix:
                assert self.route == DEFAULT_PASS2A_MODEL
                data = {
                    "sections": [
                        {
                            "kind": "chapter",
                            "title_plain": "CHAPTER I",
                            "start_block_id": "p0001-b0001",
                            "end_block_id": "p0003-b0001",
                        }
                    ]
                }
            elif "## Task: Pass 2B" in request.dynamic_suffix:
                assert self.route == DEFAULT_PASS2B_MODEL
                # Returns compact 10-element tuples; leaves p0002-b0003 unsplit (no child) & merges p0002-b0006 inside
                data = {
                    "units": [
                        ["apparatus", "p0001-b0001", "p0001-b0001", "CHAPTER I", [], [], None, None, None, []],
                        [
                            "letter",
                            "p0002-b0001",
                            "p0003-b0001",
                            "Letter 1",
                            [["A", "person"]],
                            [["B", "person"]],
                            "Calcutta",
                            None,
                            None,
                            [],
                        ],
                    ]
                }
            else:
                # Pass 2C escalation on low-confidence p0002-b0006 boundary
                assert self.route == DEFAULT_ESCALATION_MODEL
                data = {"verdict": "starts_new_letter", "confidence": 0.96, "rationale": "New dateline opener."}
            return ProviderSuccess(
                data=data,
                raw_response={},
                provenance={"provider": "gemini", "model": self.route.split("/")[-1], "input_tokens": 200, "output_tokens": 80, "cost_estimate_usd": 0.001},
            )

        def evaluate_decisions(
            self,
            input_text: str,
            questions: dict[str, dict[str, object]],
            *,
            image_paths: list[str] | None = None,
            model: str = "gemini-decision-1",
        ) -> DecisionBatchResult:
            calls.append(("decision", self.route))
            if "p0002-b0003" in input_text:
                ans = DecisionAnswer("boundary", "choice", "starts_enclosure", 0.94, 0.94)
            else:
                ans = DecisionAnswer("boundary", "choice", "continuation", 0.61, 0.61)  # < 0.85 triggers Hydrogen escalation!
            return DecisionBatchResult(
                answers={"boundary": ans},
                provenance={"model": model, "input_tokens": 50, "cost_estimate_usd": 0.00014},
            )

    ledger = CostLedger(run_dir / "cost_ledger.jsonl", ceiling_usd=0.25)
    plan = run_structure_pass(
        book_id="canary-mock",
        active_run_id="exp-01",
        run_dir=run_dir,
        repo_root=tmp_path,
        cost_ceiling_usd=0.25,
        ledger=ledger,
        provider_factory=lambda route: _FakeRouterProvider(route),
    )

    jsonschema.validate(instance=plan, schema=load_schema("structure_plan"))
    assert plan["decisions_verified"] == 2
    assert plan["escalated_count"] == 1
    assert any(c == ("generate", DEFAULT_ESCALATION_MODEL) for c in calls)
    units = plan["sections"][0]["units"]
    assert [u["kind"] for u in units] == ["apparatus", "letter", "letter"]
    assert units[1]["children"] and units[1]["children"][0]["kind"] == "enclosure"
    assert units[1]["children"][0]["block_ids"] == ["p0002-b0003", "p0002-b0004"]
    assert units[2]["block_ids"] == ["p0002-b0006", "p0002-b0007", "p0003-b0001"]


def test_boundary_anchor_rebaser_survives_split_merge_and_single_page_reextract() -> None:
    from lanterns.core.models import make_boundary_anchor
    from lanterns.epistolary.corrections import rebase_boundary_anchor

    orig_blk = make_block("p0002-b0003", 2, "body", "From Bombay we sailed eastward toward Surat.", bbox=[80, 680, 900, 740])
    anchor = make_boundary_anchor(orig_blk, source="human")

    # 1. Exact ID + page match
    assert rebase_boundary_anchor(anchor, [orig_blk]) == "p0002-b0003"

    # 2. After split_block (`p0002-b0003-s1`, `p0002-b0003-s2`) -> prefix matches -s1
    split_blocks = [
        make_block("p0002-b0003-s1", 2, "letter_header", "From Bombay we sailed", bbox=[80, 680, 900, 705]),
        make_block("p0002-b0003-s2", 2, "body", "eastward toward Surat.", bbox=[80, 706, 900, 740]),
    ]
    assert rebase_boundary_anchor(anchor, split_blocks) == "p0002-b0003-s1"

    # 3. After merge_with_next / page re-extract with duplicate prefix at two y-positions -> bbox_y0 breaks tie
    reextracted_blocks = [
        make_block("p0002-x0001", 2, "body", "From Bombay we sailed eastward toward Surat.", bbox=[80, 120, 900, 170]),
        make_block("p0002-x0009", 2, "body", "From Bombay we sailed eastward toward Surat.", bbox=[80, 685, 900, 735]),
    ]
    assert rebase_boundary_anchor(anchor, reextracted_blocks) == "p0002-x0009"


def test_compile_collection_ast_from_structure_plan_offline_fallback_and_cli(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    from lanterns.cli import main
    from lanterns.core.atomic import atomic_write_json
    from lanterns.core.models import make_boundary_anchor

    repo = RepoPaths.from_root(tmp_path)
    for d in ("prompts", "schemas", "config"):
        import shutil

        shutil.copytree(Path(__file__).resolve().parents[1] / d, tmp_path / d)
    BookConfig(book_id="plan-book", title="Plan Test", language="en", series_id="pl-series", volume_number=3, active_run_id="exp-02").save(paths=repo)

    b1 = make_block("p0001-b0001", 1, "editorial", "Biographical note.")
    b2 = make_block("p0001-b0002", 1, "body", "My dear friend, I write.<fnref>1</fnref>", epistolary_transition="starts_new_letter")
    b3 = make_block("p0001-b0003", 1, "blockquote", "Enclosed memorandum.", epistolary_transition="starts_enclosure")
    b4 = make_block("p0001-b0004", 1, "signature", "Yours ever, R.", epistolary_transition="ends_enclosure")
    f1 = make_block("p0001-b0005", 1, "footnote", "Footnote one.", note_label="1")
    pages = [make_page(1, [b1, b2, b3, b4, f1], printed_folio="1")]

    # A. Offline fallback ($0) builds sec.units + derives ChildSpan(enclosure) + full conservation
    ast_offline = compile_collection_ast(pages, book_id="plan-book", repo_root=tmp_path)
    assert source_block_coverage(ast_offline) == []
    assert [u.kind for u in ast_offline.sections[0].units] == ["editorial_bridge", "letter"]
    assert ast_offline.sections[0].units[1].footnote_ids == ["fn-p0001-1"]
    assert ast_offline.series_id == "pl-series" and ast_offline.volume_number == 3

    # B. Explicit structure_plan + sparse structure_overrides rebase (split b3..b4 or override unit start on b1)
    plan = {
        "schema_version": "1.0.0",
        "book_id": "plan-book",
        "decisions_verified": 0,
        "escalated_count": 0,
        "max_call_output_tokens": 50,
        "models_used": {"pass2a": "mock", "pass2b": "mock"},
        "total_cost_usd": 0.0,
        "sections": [
            {
                "section_id": "sec-01",
                "kind": "chapter",
                "title_plain": "Chapter I",
                "start_block_id": "p0001-b0001",
                "end_block_id": "p0001-b0004",
                "start_anchor": make_boundary_anchor(b1).model_dump(mode="json"),
                "end_anchor": make_boundary_anchor(b4).model_dump(mode="json"),
                "units": [
                    {
                        "unit_id": "unit-0001",
                        "kind": "narrative",
                        "start_block_id": "p0001-b0001",
                        "end_block_id": "p0001-b0001",
                        "start_anchor": make_boundary_anchor(b1).model_dump(mode="json"),
                        "end_anchor": make_boundary_anchor(b1).model_dump(mode="json"),
                        "block_ids": ["p0001-b0001"],
                        "children": [],
                    },
                    {
                        "unit_id": "unit-0002",
                        "kind": "letter",
                        "start_block_id": "p0001-b0002",
                        "end_block_id": "p0001-b0004",
                        "start_anchor": make_boundary_anchor(b2).model_dump(mode="json"),
                        "end_anchor": make_boundary_anchor(b4).model_dump(mode="json"),
                        "block_ids": ["p0001-b0002", "p0001-b0003", "p0001-b0004"],
                        "senders": [{"name": "R.", "kind": "person"}],
                        "recipients": [{"name": "Friend", "kind": "person"}],
                        "place": "London",
                        "date_iso": "1881-03-04",
                        "children": [
                            {
                                "child_id": "child-0001",
                                "kind": "enclosure",
                                "start_block_id": "p0001-b0003",
                                "end_block_id": "p0001-b0003",
                                "start_anchor": make_boundary_anchor(b3).model_dump(mode="json"),
                                "end_anchor": make_boundary_anchor(b3).model_dump(mode="json"),
                                "block_ids": ["p0001-b0003"],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    ast_plan = compile_collection_ast(pages, book_id="plan-book", repo_root=tmp_path, structure_plan=plan, structure_overrides={})
    assert source_block_coverage(ast_plan) == []
    assert [u.kind for u in ast_plan.sections[0].units] == ["narrative", "letter"]
    assert ast_plan.letters[0].metadata.place == "London"
    assert ast_plan.letters[0].metadata.date_iso == "1881-03-04"
    assert ast_plan.letters[0].children[0].block_ids == ["p0001-b0003"]
    assert ast_plan.sections[0].units[1].footnote_ids == ["fn-p0001-1"]

    # C. CLI `lanterns structure` smoke test using mock provider
    run_dir = repo.run_dir("plan-book", "exp-02", kind="experiment")
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "ir" / "pages.json", {"pages": [p.model_dump(mode="json") for p in pages]})
    import lanterns.extraction.structure_pass as sp_mod

    class _TrivialProvider:
        name = "mock"

        def generate(self, request: object) -> object:
            from lanterns.providers.base import ProviderSuccess

            return ProviderSuccess(
                data={"units": [["narrative", "p0001-b0001", "p0001-b0001", None, [], [], None, None, None, []]]},
                raw_response={},
                provenance={"provider": "mock", "model": "mock", "output_tokens": 12},
            )

    monkeypatch.setattr(sp_mod, "build_provider", lambda *_a, **_k: _TrivialProvider())  # type: ignore[attr-defined]
    out_file = run_dir / "ir" / "structure_auto.json"
    rc = main(["--root", str(tmp_path), "structure", "plan-book", "--run-id", "exp-02", "--skip-decisions", "--out", str(out_file)])
    assert rc == 0 and out_file.is_file()
    loaded_plan = json.loads(out_file.read_text(encoding="utf-8"))
    assert sum(len(s["units"]) for s in loaded_plan["sections"]) >= 1
