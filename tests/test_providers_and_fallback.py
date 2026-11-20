"""Adversarial tests for fail-closed pricing, CostLedger ceiling, [L01] strip, thinking floors, 4-tier fallback, and scan_checks."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from PIL import Image
import pytest

from lanterns.core.cost import CostCeilingExceeded, CostLedger
from lanterns.core.models import PageExtractWire, WireBlock
from lanterns.core.pricing import (
    conservative_call_reservation_usd,
    ensure_priced_routes,
    estimate_cost_usd,
    load_pricing,
    parse_model_route,
)
from lanterns.providers.base import (
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResult,
    ProviderSuccess,
)
from lanterns.providers.gemini import (
    check_google_genai_version,
    gemini_resolve_thinking_level,
    gemini_supports_explicit_cache,
    gemini_uses_default_sampling,
    validate_gemini_model_name,
)
from lanterns.providers.gemini_cli_harness import (
    GeminiCLIHarnessProvider,
    load_native_scan_check,
    save_native_scan_check,
)
from lanterns.providers.router import (
    SCANLINE_PREFIX_SUFFIX,
    FallbackRouter,
    make_failed_closed_page_ir,
)


class _StubProvider:
    """Controllable provider stub for testing 4-tier FallbackRouter state transitions."""

    def __init__(self, name: str, model: str, responses: list[ProviderResult]) -> None:
        self.name = name
        self.pricing_family = name
        self.model = model
        self.harness = "sdk"
        self._responses = list(responses)
        self.calls: list[ProviderRequest] = []

    def generate(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request)
        if not self._responses:
            return ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, "exhausted stub")
        return self._responses.pop(0)


class TestPricingAndCostLedger:
    def test_fail_closed_pricing_rejects_unpriced_model_routes(self) -> None:
        assert parse_model_route("gemini/gemini-3.8-flash") == ("gemini", "gemini-3.8-flash")
        assert parse_model_route("openrouter/qwen/qwen3-vl-235b-a22b-instruct") == (
            "openrouter",
            "qwen/qwen3-vl-235b-a22b-instruct",
        )

        ensure_priced_routes(
            [
                "gemini/gemini-4-hydrogen",
                "gemini/gemini-3.8-flash",
                "gemini/gemini-decision-1",
                "openrouter/qwen/qwen3-vl-235b-a22b-instruct",
                "anthropic/claude-sonnet-5-5",
            ]
        )

        with pytest.raises(ValueError, match="(?i)unpriced model"):
            ensure_priced_routes(["gemini/gemini-99-nonexistent"])
        with pytest.raises(ValueError, match="(?i)unpriced model"):
            estimate_cost_usd("gemini", "gemini-99-nonexistent", 1000, 500)
        with pytest.raises(ValueError, match="(?i)unpriced model"):
            conservative_call_reservation_usd("openrouter", "unknown/model")

    def test_pricing_supports_batch_discount_cached_tokens_and_decision_zero_output(self) -> None:
        table = load_pricing()
        assert "gemini/gemini-decision-1" in table
        dec_cost = estimate_cost_usd("gemini", "gemini-decision-1", 10_000, 5)
        assert dec_cost == pytest.approx(0.001, rel=1e-5)

        full_cost = estimate_cost_usd("gemini", "gemini-4-hydrogen", 100_000, 10_000, on_date=date(2026, 11, 16))
        batch_cost = estimate_cost_usd(
            "gemini",
            "gemini-4-hydrogen",
            100_000,
            10_000,
            is_batch=True,
            on_date=date(2026, 11, 16),
        )
        assert batch_cost == pytest.approx(full_cost * 0.5, rel=1e-5)

        cached_cost = estimate_cost_usd(
            "gemini",
            "gemini-4-hydrogen",
            100_000,
            10_000,
            cached_input_tokens=90_000,
            on_date=date(2026, 11, 16),
        )
        assert cached_cost < full_cost

    def test_cost_ledger_enforces_hard_ceiling_and_jsonl_durability(self, tmp_path: Path) -> None:
        ledger_file = tmp_path / "cost_ledger.json"
        ledger = CostLedger(ledger_file, ceiling_usd=0.0100, default_reservation_usd=0.0040)

        res1 = ledger.acquire_slot(0.0040)
        ledger.release_and_record(
            res1,
            stage="extract",
            provider="gemini",
            model="gemini-3.8-flash",
            cost_usd=0.0065,
            page_number=1,
            input_tokens=2000,
            output_tokens=1000,
        )
        assert ledger.total_spent_usd == pytest.approx(0.0065)

        with pytest.raises(CostCeilingExceeded):
            ledger.acquire_slot(0.0050)

        reloaded = CostLedger(ledger_file, ceiling_usd=0.0100)
        assert reloaded.total_spent_usd == pytest.approx(0.0065)
        assert len(reloaded.entries) == 1


class TestProvidersNormalizationAndFallback:
    def test_gemini_thinking_level_floors_and_rejects_deprecated_models(self) -> None:
        ok, ver = check_google_genai_version()
        assert ok is True, f"Expected google-genai >= 3.2.0, got {ver}"

        for deprecated in ("gemini-1.5-pro", "gemini-2.0-flash", "gemini-2.5-pro"):
            with pytest.raises(ValueError, match="Deprecated Gemini model"):
                validate_gemini_model_name(deprecated)

        assert validate_gemini_model_name("models/gemini-4-hydrogen") == "gemini-4-hydrogen"
        for m in ("gemini-3.7-flash", "gemini-3.8-flash", "gemini-4-hydrogen", "gemini-4-carbon"):
            assert gemini_resolve_thinking_level(m, "minimal") == "low"
            assert gemini_uses_default_sampling(m) is True

        assert gemini_supports_explicit_cache("gemini-3.8-flash") is True
        assert gemini_supports_explicit_cache("gemini-4-hydrogen") is False

    def test_scanline_l01_prefix_strip_and_single_column_right_aligned_order_preserved(self) -> None:
        from lanterns.extraction.normalize import normalize_wire_to_page_ir

        wire = PageExtractWire(
            printed_folio="14",
            blocks=[
                WireBlock(
                    type="dateline",
                    bbox=[520, 120, 880, 155],
                    text="[L01] Weimar, 5th March, 1806.",
                    alignment="right",
                    epistolary_transition="starts_new_letter",
                ),
                WireBlock(
                    type="body",
                    bbox=[110, 170, 880, 420],
                    text="[L01] First line of body\n[L02] Second line of body",
                    alignment="left",
                ),
                WireBlock(
                    type="signature",
                    bbox=[600, 440, 880, 475],
                    text="[L03] GOETHE.",
                    alignment="right",
                ),
            ],
        )
        page_ir = normalize_wire_to_page_ir(wire.model_dump(), page_number=14, source_page_sha256="sha-p14")

        assert [b.type for b in page_ir.blocks] == ["dateline", "body", "signature"]
        for blk in page_ir.blocks:
            assert "[L01]" not in blk.plain_text
            assert "[L02]" not in blk.plain_text
            assert "[L03]" not in blk.plain_text
            assert "[L01]" not in blk.tagged_text

        # Also verify explicit two-column blocks sort left column before right column
        two_col_wire = PageExtractWire(
            printed_folio="20",
            blocks=[
                WireBlock(type="body", bbox=[100, 120, 480, 200], text="Left 1", alignment="two_column_left"),
                WireBlock(type="body", bbox=[520, 120, 900, 200], text="Right 1", alignment="two_column_right"),
                WireBlock(type="body", bbox=[100, 220, 480, 300], text="Left 2", alignment="two_column_left"),
                WireBlock(type="body", bbox=[520, 220, 900, 300], text="Right 2", alignment="two_column_right"),
            ],
        )
        two_col_ir = normalize_wire_to_page_ir(two_col_wire.model_dump(), page_number=20)
        assert [b.plain_text for b in two_col_ir.blocks] == ["Left 1", "Left 2", "Right 1", "Right 2"]

    def test_gemini_cli_harness_ingests_committed_scan_check_override(self, tmp_path: Path) -> None:
        book_dir = tmp_path / "books" / "newton-1756"
        wire_payload = {
            "printed_folio": "[ 4 ]",
            "page_orientation": "upright",
            "warning_codes": [],
            "blocks": [
                {
                    "type": "body",
                    "bbox": [100, 120, 890, 820],
                    "text": "Verified from scan pixels: Induſtry and patient Thought.",
                    "alignment": "left",
                }
            ],
        }
        saved = save_native_scan_check(book_dir, 4, wire_payload, reviewer="qa-adversary", notes="Pixel check")
        assert saved.is_file()

        loaded = load_native_scan_check(book_dir, 4)
        assert loaded is not None
        assert loaded.data["blocks"][0]["text"] == "Verified from scan pixels: Induſtry and patient Thought."

        # Also verify FallbackRouter Tier 0 picks up scan_check without calling VLM!
        primary = _StubProvider("gemini", "gemini-3.8-flash", [])
        router = FallbackRouter(primary, book_dir=book_dir, prefer_scan_checks=True)
        outcome = router.execute_page(ProviderRequest(prompt="Extract", schema_name="page_extract_wire"), page_number=4)
        assert outcome.tier_reached == 0
        assert outcome.failed_closed is False
        assert len(primary.calls) == 0

        # And GeminiCLIHarnessProvider.generate returns scan check directly
        cli_prov = GeminiCLIHarnessProvider(book_dir=book_dir)
        res = cli_prov.generate(ProviderRequest(prompt="Extract", settings={"page_number": 4}))
        assert isinstance(res, ProviderSuccess)

    def test_four_tier_router_recitation_band_split_cross_family_and_fail_closed(self, tmp_path: Path) -> None:
        img_path = tmp_path / "p0001.png"
        Image.new("L", (400, 600), color=245).save(img_path)

        ok_wire = {
            "printed_folio": "1",
            "page_orientation": "upright",
            "warning_codes": [],
            "blocks": [{"type": "body", "bbox": [100, 100, 800, 300], "text": "Rescued by Qwen."}],
        }
        recit_err = ProviderError(ProviderErrorKind.RECITATION, "Gemini RECITATION stop")

        # Case A: Tier 1 RECITATION -> Tier 2a [L01] RECITATION -> Tier 2b same-family RECITATION -> Tier 2c band split RECITATION -> Tier 3 cross-family Qwen rescues!
        primary = _StubProvider("gemini", "gemini-3.8-flash", [recit_err, recit_err, recit_err])
        same_family_alt = _StubProvider("gemini", "gemini-3.6-flash", [recit_err])
        cross_family_qwen = _StubProvider(
            "openrouter",
            "qwen/qwen3-vl-235b-a22b-instruct",
            [ProviderSuccess(data=ok_wire, raw_response={}, provenance={"provider": "openrouter", "model": "qwen/qwen3-vl-235b-a22b-instruct", "cost_estimate_usd": 0.0012})],
        )

        router = FallbackRouter(
            primary,
            fallback_providers=[same_family_alt, cross_family_qwen],
            band_split_dir=tmp_path / "bands",
        )
        req = ProviderRequest(prompt="Master prompt", schema_name="page_extract_wire", image_paths=[str(img_path)])
        outcome = router.execute_page(req, page_number=1)

        assert outcome.failed_closed is False
        assert outcome.rescued is True
        assert outcome.tier_reached == 3
        assert outcome.success is not None
        assert outcome.success.data["blocks"][0]["text"] == "Rescued by Qwen."
        # Verify Tier 2a injected [L01] scanline prefix
        assert SCANLINE_PREFIX_SUFFIX in (primary.calls[1].dynamic_suffix or "")

        # Case B: All tiers fail -> Tier 4 fail-closed PageIR (zero silent drops!)
        p_fail = _StubProvider("gemini", "gemini-3.8-flash", [recit_err, recit_err, recit_err])
        q_fail = _StubProvider("openrouter", "qwen/qwen3.8-flash", [ProviderError(ProviderErrorKind.TIMEOUT, "timeout")])
        router_fail = FallbackRouter(p_fail, fallback_providers=[q_fail], band_split_dir=tmp_path / "bands2")
        out_fail = router_fail.execute_page(req, page_number=9)
        assert out_fail.failed_closed is True
        assert out_fail.tier_reached == 4
        failed_ir = make_failed_closed_page_ir(9, out_fail.failure_reason or "exhausted")
        assert failed_ir.failed_closed is True
        assert failed_ir.page_number == 9

    def test_anthropic_opus_sonnet_haiku_sdk_and_qwen4_payload_contracts(self, tmp_path: Path) -> None:
        from lanterns.providers.anthropic import (
            AnthropicProvider,
            check_anthropic_sdk_version,
            resolve_claude_thinking_and_effort,
        )
        from lanterns.providers.openai_compat import OpenAICompatProvider, is_qwen4_model

        ok_sdk, sdk_ver = check_anthropic_sdk_version()
        assert ok_sdk is True, f"Expected anthropic >= 2.8.0, got {sdk_ver}"

        ensure_priced_routes(
            [
                "qwen_cloud/qwen4-max",
                "qwen_cloud/qwen3.8-max",
                "qwen_cloud/qwen3.7-plus",
                "qwen_cloud/qwen3.8-flash",
                "anthropic/claude-opus-5-5",
                "anthropic/claude-opus-4-8",
                "anthropic/claude-sonnet-5-5",
                "anthropic/claude-sonnet-4-6",
                "anthropic/claude-haiku-5-5",
                "anthropic/claude-haiku-4-5-20251001",
            ]
        )

        # Verify Claude 5.5 thinking/effort compliance (never sends banned 'disabled')
        eff_opus, think_opus, min_opus = resolve_claude_thinking_and_effort("claude-opus-5-5", "low")
        assert eff_opus == "low" and think_opus is None and min_opus >= 20480
        eff_sonnet, think_sonnet, _ = resolve_claude_thinking_and_effort("claude-sonnet-5-5", "low")
        assert eff_sonnet == "low" and think_sonnet == {"type": "between_tools"}
        eff_haiku, think_haiku, _ = resolve_claude_thinking_and_effort("claude-haiku-5-5", "low")
        assert eff_haiku == "low" and think_haiku is None

        img_path = tmp_path / "scan.png"
        Image.new("L", (600, 900), color=240).save(img_path)
        req = ProviderRequest(prompt="Transcribe", schema_name="page_extract_wire", image_paths=[str(img_path)])

        for claude_id in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
            prov = AnthropicProvider(model=claude_id, api_key="test-key")
            payload, meta = prov.build_payload(req)
            assert payload["model"] == claude_id
            assert "output_config" in payload
            assert payload["output_config"]["effort"] == "low"
            assert payload["output_config"]["format"]["type"] == "json_schema"
            assert meta["structured_format"] is True

        # Verify Qwen 4 vs Qwen 3 parameter routing
        assert is_qwen4_model("qwen4-max") is True
        assert is_qwen4_model("qwen3.8-flash") is False
        q4 = OpenAICompatProvider(provider_name="qwen_cloud", model="qwen4-max", api_key="test-key")
        q4_body = q4._build_payload(req, include_response_format=True)
        assert q4_body["reasoning_effort"] == "low"
        assert "vl_high_resolution_images" not in q4_body
        assert q4_body["messages"][0]["content"][1]["image_url"]["detail"] == "high"

        # Verify binder signature 'B' is stripped in _propagate_page_folios
        from lanterns.epistolary.compiler import compile_collection_ast
        from lanterns.extraction.normalize import normalize_wire_to_page_ir

        p5 = normalize_wire_to_page_ir(
            {
                "printed_folio": None,
                "blocks": [
                    {
                        "type": "dateline",
                        "bbox": [450, 800, 880, 840],
                        "text": "Off Gibraltar, Nov. 20th, 1847.",
                        "alignment": "right",
                        "epistolary_transition": "starts_new_letter",
                    },
                    {"type": "body", "bbox": [100, 850, 880, 900], "text": "The Rock of Gibraltar.", "alignment": "indented"},
                    {"type": "folio", "bbox": [480, 920, 515, 945], "text": "B", "alignment": "center"},
                ],
            },
            page_number=5,
        )
        compiled = compile_collection_ast([p5], book_id="sig-check", title="Test")
        assert compiled.pages[0].printed_folio is None
        assert compiled.letters[0].start_folio is None


def test_build_originals_pdf_from_jp2_zip_jbig2_free(tmp_path: Path) -> None:
    import io
    import zipfile

    from PIL import Image

    from lanterns.extraction.pdf_ingest import triage_pdf
    from lanterns.source.archive_org import build_originals_pdf_from_jp2_zip

    jp2_zip = tmp_path / "sample_jp2.zip"
    with zipfile.ZipFile(jp2_zip, "w") as zf:
        for leaf_idx in range(3):
            img = Image.new("RGB", (2000, 3000), (245, 240, 230))
            buf = io.BytesIO()
            img.save(buf, format="JPEG2000")
            zf.writestr(f"sample_jp2/sample_{leaf_idx:04d}.jp2", buf.getvalue())

    scandata = """<?xml version="1.0" encoding="utf-8"?>
    <book><bookData><dpi>400</dpi></bookData><pageData>
      <page leafNum="0"><addToAccessFormats>false</addToAccessFormats></page>
      <page leafNum="1"><addToAccessFormats>true</addToAccessFormats></page>
      <page leafNum="2"><addToAccessFormats>true</addToAccessFormats></page>
    </pageData></book>
    """
    out_pdf = tmp_path / "sample.originals.pdf"
    res = build_originals_pdf_from_jp2_zip(jp2_zip, out_pdf, scandata_xml=scandata, source_zip_sha256="abc123")
    assert res["reused_cache"] is False
    assert res["page_count"] == 2
    assert res["optical_dpi"] == 400

    triage = triage_pdf(out_pdf)
    assert triage["total_pages"] == 2
    assert triage["jbig2_pages"] == []
    assert triage["median_optical_dpi"] == 400.0

    cached = build_originals_pdf_from_jp2_zip(jp2_zip, out_pdf, scandata_xml=scandata, source_zip_sha256="abc123")
    assert cached["reused_cache"] is True
    assert cached["sha256"] == res["sha256"]


def test_reconcile_claude_wire_output_drops_optional_nulls_and_clamps_bbox() -> None:
    from lanterns.core.config import load_schema, validate_data
    from lanterns.providers.anthropic import reconcile_claude_wire_output

    schema = load_schema("page_extract_wire")
    raw_claude = {
        "printed_folio": None,
        "page_orientation": None,
        "warning_codes": [],
        "blocks": [
            {
                "type": "body",
                "bbox": [-2, 10, 1004, 995],
                "text": "Sample text.",
                "alignment": None,
                "epistolary_transition": None,
                "line_breaks_preserved": None,
                "heading_level": None,
                "note_label": None,
                "continues_from_prev": None,
                "continues_to_next": None,
                "table_role": None,
                "cells": None,
            }
        ],
    }
    cleaned = reconcile_claude_wire_output(raw_claude, schema)
    validate_data("page_extract_wire", cleaned)
    assert cleaned["blocks"][0]["bbox"] == [0, 10, 1000, 995]
    assert "alignment" not in cleaned["blocks"][0]
    assert cleaned["blocks"][0]["heading_level"] is None


def test_batch_runner_chunked_resume_50pct_discount_and_fallback_rescue(tmp_path: Path) -> None:
    import json
    import time

    from lanterns.core.cost import CostLedger
    from lanterns.core.models import PageIR
    from lanterns.extraction.batch_runner import execute_batch_candidate_pages
    from lanterns.extraction.pdf_ingest import PagePreflight
    from lanterns.providers.anthropic import AnthropicProvider
    from lanterns.providers.base import ProviderError, ProviderErrorKind, ProviderRequest, ProviderSuccess
    from lanterns.providers.router import FallbackRouter

    # 1. Verify exact 50% batch discount in AnthropicProvider._provenance
    ap = AnthropicProvider(model="claude-sonnet-5-5", api_key="sk-test")
    sample_raw = {"stop_reason": "end_turn", "usage": {"input_tokens": 1000, "output_tokens": 500}}
    meta = {"effort": "low", "images": [], "structured_format": True}
    std_prov = ap._provenance(sample_raw, time.time(), meta, is_batch=False)
    bat_prov = ap._provenance(sample_raw, time.time(), meta, is_batch=True)
    assert bat_prov["is_batch"] is True
    assert round(bat_prov["cost_estimate_usd"], 8) == round(std_prov["cost_estimate_usd"] * 0.5, 8)

    # 2. Verify chunked batch submission, crash-resume from batch_jobs.json, and per-page fallback rescue
    class FakeBatchPrimary:
        name = "anthropic"
        model = "claude-sonnet-5-5"

        def __init__(self) -> None:
            self.submitted_chunks: list[list[str]] = []

        def submit_message_batch(self, items: list[tuple[str, ProviderRequest]]) -> dict[str, str]:
            cids = [cid for cid, _ in items]
            self.submitted_chunks.append(cids)
            return {"id": f"msgbatch_new_{len(self.submitted_chunks)}"}

        def retrieve_message_batch(self, batch_id: str) -> dict[str, object]:
            return {"id": batch_id, "processing_status": "ended", "request_counts": {"succeeded": 2}}

        def collect_message_batch_results(self, batch_id: str, reqs: dict[str, ProviderRequest]) -> dict[str, object]:
            out: dict[str, object] = {}
            for cid in reqs:
                if cid == "page-0003":
                    out[cid] = ProviderError(ProviderErrorKind.SCHEMA_INVALID, "Simulated batch item error")
                else:
                    out[cid] = ProviderSuccess(
                        data={
                            "printed_folio": "1",
                            "warning_codes": [],
                            "blocks": [{"type": "body", "bbox": [10, 10, 900, 200], "text": f"From {batch_id} {cid}"}],
                        },
                        raw_response={},
                        provenance={"provider": "anthropic", "model": "claude-sonnet-5-5", "harness": "anthropic_batch", "is_batch": True, "cost_estimate_usd": 0.011},
                    )
            return out

        def generate(self, request: ProviderRequest) -> ProviderError:
            return ProviderError(ProviderErrorKind.SCHEMA_INVALID, "Primary sync also fails p3")

    class FakeRescueFallback:
        name = "gemini"
        model = "gemini-3.8-flash"

        def generate(self, request: ProviderRequest) -> ProviderSuccess:
            return ProviderSuccess(
                data={
                    "printed_folio": "3",
                    "warning_codes": [],
                    "blocks": [{"type": "body", "bbox": [10, 10, 900, 200], "text": "Rescued page 3."}],
                },
                raw_response={},
                provenance={"provider": "gemini", "model": "gemini-3.8-flash", "harness": "sdk", "cost_estimate_usd": 0.006},
            )

    run_root = tmp_path / "batch_run"
    raw_dir = run_root / "raw_wire"
    pages_dir = run_root / "pages"
    raw_dir.mkdir(parents=True)
    pages_dir.mkdir(parents=True)

    # Pre-seed chunk_001 in batch_jobs.json to simulate resuming after a crash!
    (run_root / "batch_jobs.json").write_text(
        json.dumps(
            {
                "version": "batch-jobs-v1",
                "chunks": {
                    "chunk_001_p0001_p0002": {
                        "backend": "anthropic",
                        "job_id": "msgbatch_resumed_existing",
                        "pages": [1, 2],
                        "status": "submitted",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    primary = FakeBatchPrimary()
    router = FallbackRouter(primary, fallback_providers=[FakeRescueFallback()])
    ledger = CostLedger(run_root / "cost_ledger.jsonl", ceiling_usd=2.0, default_reservation_usd=0.04)
    preflights = {
        p: PagePreflight(
            page_number=p,
            width_pt=400.0,
            height_pt=600.0,
            witness_text="",
            witness_char_count=0,
            global_ink_ratio=0.1,
            max_tile_ink_ratio=0.2,
            is_blank_or_tissue=False,
            uses_jbig2=False,
            is_mrc_derivative=False,
            source_page_sha256=f"sha-{p}",
        )
        for p in (1, 2, 3)
    }
    page_map: dict[int, PageIR] = {}

    execute_batch_candidate_pages(
        to_extract=[1, 2, 3],
        primary_provider=primary,
        build_request_fn=lambda p: ProviderRequest(prompt="Test", schema_name="page_extract_wire", settings={"page_number": p}),
        router=router,
        ledger=ledger,
        preflights=preflights,
        run_root=run_root,
        raw_wire_dir=raw_dir,
        pages_out_dir=pages_dir,
        page_map=page_map,
        resume=True,
        chunk_size=2,
        poll_interval_s=0.01,
    )

    # Chunk 1 ([1, 2]) must NOT have been resubmitted; only Chunk 2 ([3]) submitted!
    assert primary.submitted_chunks == [["page-0003"]]
    assert "msgbatch_resumed_existing" in page_map[1].blocks[0].plain_text
    assert page_map[3].rescued is True
    assert page_map[3].failed_closed is False
    assert "Rescued page 3." in page_map[3].blocks[0].plain_text
    assert any(e.get("is_batch") is True and e.get("stage") == "tier1_batch" for e in ledger.entries)


def test_anthropic_content_filter_maps_to_recitation_and_batch_initial_error_triggers_scanline(tmp_path: Path) -> None:
    import types

    import anthropic as anthropic_sdk
    import httpx

    from lanterns.providers.anthropic import AnthropicProvider

    class _FakeResp:
        def __init__(self) -> None:
            self.status_code = 400
            self.headers: dict[str, str] = {}
            self.request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    class _FakeMessages:
        def create(self, **_kwargs: object) -> object:
            raise anthropic_sdk.BadRequestError(
                "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Output blocked by content filtering policy'}}",
                response=_FakeResp(),  # type: ignore[arg-type]
                body=None,
            )

    ap = AnthropicProvider(
        model="claude-sonnet-5-5",
        api_key="sk-test",
        client=types.SimpleNamespace(messages=_FakeMessages()),  # type: ignore[arg-type]
    )
    img_path = tmp_path / "p76.png"
    Image.new("L", (400, 600), color=240).save(img_path)
    req = ProviderRequest(prompt="Extract", schema_name="page_extract_wire", image_paths=[str(img_path)])
    err = ap.generate(req)
    assert isinstance(err, ProviderError)
    assert err.kind == ProviderErrorKind.RECITATION

    ok_wire = {
        "printed_folio": "76",
        "page_orientation": "upright",
        "warning_codes": [],
        "blocks": [{"type": "body", "bbox": [10, 10, 900, 200], "text": "Rescued with L01."}],
    }
    primary = _StubProvider(
        "anthropic",
        "claude-sonnet-5-5",
        [ProviderSuccess(data=ok_wire, raw_response={}, provenance={"provider": "anthropic", "model": "claude-sonnet-5-5", "cost_estimate_usd": 0.01})],
    )
    router = FallbackRouter(primary)
    outcome = router.execute_page(req, page_number=76, initial_error=err)
    assert outcome.failed_closed is False
    assert outcome.rescued is True
    assert outcome.tier_reached == 2
    assert len(primary.calls) == 1
    assert SCANLINE_PREFIX_SUFFIX in (primary.calls[0].dynamic_suffix or "")




