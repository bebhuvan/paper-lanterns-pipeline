"""Profile-driven 4-Tier Anti-Recitation & Multi-Family Fallback Router.

Tiers:
  - Tier 1: Primary model (with optional proactive `[L01]` scanline prefixing)
  - Tier 2: Same-family alternate model (`gemini-3.6-flash` / `gemini-4-hydrogen`) or
            `[L01]` scanline retry / 2-band horizontal image split on `RECITATION` / `MAX_TOKENS`
  - Tier 3: Cross-family backup (`openrouter/qwen/...`, `qwen_cloud/...`, `anthropic/...`)
            when Gemini hits persistent `RECITATION`, `SAFETY`, or quota/availability blocks
  - Tier 4: Fail-closed `PageIR(failed_closed=True, failure_reason=...)` so zero pages vanish
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from lanterns.core.cost import CostCeilingExceeded, CostLedger
from lanterns.core.models import PageIR
from lanterns.core.pricing import conservative_call_reservation_usd, parse_model_route
from lanterns.providers.anthropic import AnthropicProvider
from lanterns.providers.base import (
    MockProvider,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResult,
    ProviderSuccess,
)
from lanterns.providers.gemini import GeminiProvider
from lanterns.providers.gemini_cli_harness import GeminiCLIHarnessProvider, load_native_scan_check
from lanterns.providers.openai_compat import ENDPOINTS as OPENAI_ENDPOINTS
from lanterns.providers.openai_compat import OpenAICompatProvider

SCANLINE_PREFIX_SUFFIX = (
    "ANTI-RECITATION SCANLINE MODE: Prefix each transcribed physical print line inside `text` "
    "with sequential line markers `[L01] `, `[L02] `, `[L03] `, etc., reading strictly from the scan glyphs."
)

BAND_TOP_Y1 = 540
BAND_BOTTOM_Y0 = 460


@dataclass
class RouterAttempt:
    tier: int
    strategy: str
    provider: str
    model: str
    status: str
    error_kind: str | None = None
    message: str = ""
    cost_usd: float = 0.0


@dataclass
class RouterOutcome:
    success: ProviderSuccess | None
    failed_closed: bool
    failure_reason: str | None = None
    rescued: bool = False
    tier_reached: int = 1
    total_cost_usd: float = 0.0
    attempts: list[RouterAttempt] = field(default_factory=list)


def build_provider(provider_or_route: str, model: str | None = None, **kwargs: Any) -> Any:
    """Instantiate a VLM provider from `('gemini', 'gemini-3.8-flash')` or `'openrouter/qwen/qwen3.8-flash'`."""
    if model is None:
        prov_name, model_name = parse_model_route(provider_or_route)
    else:
        prov_name, model_name = provider_or_route.strip(), model.strip()

    if prov_name == "gemini":
        return GeminiProvider(model=model_name, **kwargs)
    if prov_name == "gemini_cli":
        return GeminiCLIHarnessProvider(model=model_name, **kwargs)
    if prov_name in OPENAI_ENDPOINTS:
        return OpenAICompatProvider(provider_name=prov_name, model=model_name, **kwargs)
    if prov_name == "anthropic":
        return AnthropicProvider(model=model_name, **kwargs)
    if prov_name == "mock":
        return MockProvider(model=model_name, **kwargs)
    raise ValueError(f"Unknown provider family {prov_name!r} for model {model_name!r}")


def make_failed_closed_page_ir(
    page_number: int,
    failure_reason: str,
    *,
    source_page_sha256: str = "",
    cost_usd: float = 0.0,
) -> PageIR:
    """Tier-4 fail-closed `PageIR` guaranteeing zero silent page drops."""
    return PageIR(
        page_number=page_number,
        printed_folio=None,
        page_orientation="upright",
        warning_codes=["failed_closed"],
        failed_closed=True,
        failure_reason=failure_reason,
        source_page_sha256=source_page_sha256,
        blocks=[],
        provider="router",
        model="fail-closed",
        harness="router",
        rescued=False,
        cost_usd=round(cost_usd, 8),
    )


def _with_scanline_prefix(request: ProviderRequest) -> ProviderRequest:
    cloned = copy.deepcopy(request)
    cloned.settings.pop("cached_content", None)
    suffix = f"{cloned.dynamic_suffix}\n{SCANLINE_PREFIX_SUFFIX}" if cloned.dynamic_suffix else SCANLINE_PREFIX_SUFFIX
    cloned.dynamic_suffix = suffix
    return cloned


def _for_non_gemini_family(request: ProviderRequest, prompt_fallback: str) -> ProviderRequest:
    cloned = copy.deepcopy(request)
    cloned.settings.pop("cached_content", None)
    cloned.settings.pop("service_tier", None)
    if not cloned.prompt and prompt_fallback:
        cloned.prompt = prompt_fallback
    return cloned


def split_image_two_horizontal_bands(image_path: str | Path, work_dir: Path, tag: str) -> tuple[Path, Path]:
    """Split a page image horizontally into overlapping top (`0..540`) and bottom (`460..1000`) PNG bands."""
    work_dir.mkdir(parents=True, exist_ok=True)
    top_path = work_dir / f"{tag}_band_top.png"
    bot_path = work_dir / f"{tag}_band_bottom.png"
    with Image.open(image_path) as raw:
        w, h = raw.size
        top_crop = raw.crop((0, 0, w, max(1, round(h * BAND_TOP_Y1 / 1000.0))))
        bot_crop = raw.crop((0, round(h * BAND_BOTTOM_Y0 / 1000.0), w, h))
        top_crop.save(top_path, format="PNG")
        bot_crop.save(bot_path, format="PNG")
    return top_path, bot_path


def merge_two_band_wires(top_data: dict[str, Any], bot_data: dict[str, Any]) -> dict[str, Any]:
    """Remap 2-band y-coordinates back to full-page `0..1000` and deduplicate seam overlap."""
    merged_blocks: list[dict[str, Any]] = []
    top_span = BAND_TOP_Y1
    bot_span = 1000 - BAND_BOTTOM_Y0

    for blk in top_data.get("blocks") or []:
        b_copy = copy.deepcopy(blk)
        box = b_copy.get("bbox") or [0, 0, 1000, 1000]
        y0 = round(int(box[1]) * top_span / 1000.0)
        y1 = round(int(box[3]) * top_span / 1000.0)
        b_copy["bbox"] = [int(box[0]), min(1000, y0), int(box[2]), min(1000, max(y0 + 1, y1))]
        merged_blocks.append(b_copy)

    for blk in bot_data.get("blocks") or []:
        b_copy = copy.deepcopy(blk)
        box = b_copy.get("bbox") or [0, 0, 1000, 1000]
        y0 = BAND_BOTTOM_Y0 + round(int(box[1]) * bot_span / 1000.0)
        y1 = BAND_BOTTOM_Y0 + round(int(box[3]) * bot_span / 1000.0)
        b_copy["bbox"] = [int(box[0]), min(1000, y0), int(box[2]), min(1000, max(y0 + 1, y1))]
        text_norm = " ".join(str(b_copy.get("text") or "").split())
        if text_norm and any(
            abs(int(prev["bbox"][1]) - b_copy["bbox"][1]) <= 45
            and " ".join(str(prev.get("text") or "").split()) == text_norm
            for prev in merged_blocks
        ):
            continue
        merged_blocks.append(b_copy)

    folio = top_data.get("printed_folio") or bot_data.get("printed_folio")
    warnings = list(dict.fromkeys([*(top_data.get("warning_codes") or []), *(bot_data.get("warning_codes") or [])]))
    return {
        "printed_folio": folio,
        "page_orientation": top_data.get("page_orientation") or "upright",
        "warning_codes": warnings,
        "blocks": merged_blocks,
    }


class FallbackRouter:
    """Executes the 4-tier anti-recitation and cross-family VLM fallback protocol."""

    def __init__(
        self,
        primary_provider: Any,
        *,
        fallback_providers: list[Any] | None = None,
        fallback_routes: list[str] | None = None,
        use_scanline_prefix: bool = False,
        prompt_fallback_text: str = "",
        band_split_dir: Path | None = None,
        book_dir: Path | None = None,
        prefer_scan_checks: bool = True,
        repo_root: str | None = None,
    ) -> None:
        self.primary_provider = primary_provider
        self._fallback_providers = list(fallback_providers) if fallback_providers is not None else None
        self._fallback_routes = list(fallback_routes or [])
        self.use_scanline_prefix = use_scanline_prefix
        self.prompt_fallback_text = prompt_fallback_text
        self.band_split_dir = band_split_dir
        self.book_dir = book_dir
        self.prefer_scan_checks = prefer_scan_checks
        self.repo_root = repo_root

    def _resolved_fallbacks(self) -> list[Any]:
        if self._fallback_providers is not None:
            return self._fallback_providers
        if getattr(self.primary_provider, "name", "") == "mock":
            return []
        built: list[Any] = []
        for route in self._fallback_routes:
            built.append(build_provider(route))
        self._fallback_providers = built
        return built

    def execute_page(
        self,
        request: ProviderRequest,
        *,
        page_number: int,
        ledger: CostLedger | None = None,
        initial_error: ProviderError | None = None,
    ) -> RouterOutcome:
        # Step 0: Check committed native-vision scan check (`books/<book_id>/scan_checks/page_NNNN.json`)
        if self.prefer_scan_checks and self.book_dir is not None:
            checked = load_native_scan_check(self.book_dir, page_number)
            if checked is not None:
                att = RouterAttempt(
                    tier=0,
                    strategy="native_scan_check",
                    provider="gemini_cli",
                    model="scan-check",
                    status="ok",
                )
                return RouterOutcome(success=checked, failed_closed=False, rescued=False, tier_reached=0, attempts=[att])

        attempts: list[RouterAttempt] = []
        total_cost = 0.0

        if initial_error is not None:
            t1_res: ProviderResult = initial_error
            attempts.append(
                RouterAttempt(
                    tier=1,
                    strategy="tier1_batch",
                    provider=str(getattr(self.primary_provider, "name", "unknown")),
                    model=str(getattr(self.primary_provider, "model", "unknown")),
                    status="error",
                    error_kind=initial_error.kind.value,
                    message=initial_error.message,
                    cost_usd=float((initial_error.provenance or {}).get("cost_estimate_usd") or 0.0),
                )
            )
        else:
            # Tier 1: Primary model (with optional proactive `[L01]` scanline prefix)
            t1_req = _with_scanline_prefix(request) if self.use_scanline_prefix else request
            if not t1_req.prompt and self.prompt_fallback_text and not t1_req.settings.get("cached_content"):
                t1_req = copy.deepcopy(t1_req)
                t1_req.prompt = self.prompt_fallback_text

            t1_res, t1_cost = self._call_tracked(self.primary_provider, t1_req, page_number, ledger, "tier1_primary", 1, attempts)
            total_cost += t1_cost
            if isinstance(t1_res, ProviderSuccess):
                return RouterOutcome(
                    success=t1_res,
                    failed_closed=False,
                    rescued=False,
                    tier_reached=1,
                    total_cost_usd=round(total_cost, 8),
                    attempts=attempts,
                )
            if ledger is not None and ledger.cancelled_event.is_set():
                return self._fail_closed(t1_res.message, total_cost, attempts, tier=1)

        # Tier 2: Anti-Recitation scanline retry, same-family alternate model, & 2-band split
        t2_outcome, t2_cost = self._run_tier2(request, t1_res, page_number, ledger, attempts)
        total_cost += t2_cost
        if t2_outcome is not None:
            t2_outcome.total_cost_usd = round(total_cost, 8)
            return t2_outcome
        if ledger is not None and ledger.cancelled_event.is_set():
            return self._fail_closed("Halted by cost ceiling during Tier 2", total_cost, attempts, tier=2)

        # Tier 3: Cross-family backup (`openrouter`, `qwen_cloud`, `anthropic`)
        t3_outcome, t3_cost = self._run_tier3(request, page_number, ledger, attempts, t1_err=t1_res)
        total_cost += t3_cost
        if t3_outcome is not None:
            t3_outcome.total_cost_usd = round(total_cost, 8)
            return t3_outcome

        # Tier 4: Fail closed (zero silent drops)
        reasons = "; ".join(f"{a.provider}/{a.model}:{a.error_kind or a.status}" for a in attempts)
        return self._fail_closed(f"All fallback tiers exhausted ({reasons})", total_cost, attempts, tier=4)

    def _try_max_tokens_retry(
        self,
        request: ProviderRequest,
        t1_err: ProviderError,
        page_number: int,
        ledger: CostLedger | None,
        attempts: list[RouterAttempt],
    ) -> tuple[RouterOutcome | None, float]:
        if t1_err.kind != ProviderErrorKind.MAX_TOKENS:
            return None, 0.0
        cur_max = int((request.settings or {}).get("max_output_tokens") or 16384)
        spent = 0.0
        for target_max in (32768, 65536):
            if cur_max >= target_max:
                continue
            hi_req = copy.deepcopy(_with_scanline_prefix(request) if self.use_scanline_prefix else request)
            if not hi_req.prompt and self.prompt_fallback_text and not hi_req.settings.get("cached_content"):
                hi_req.prompt = self.prompt_fallback_text
            hi_req.settings = {**(hi_req.settings or {}), "max_output_tokens": target_max}
            res, c = self._call_tracked(
                self.primary_provider, hi_req, page_number, ledger, "tier2_max_tokens_retry", 2, attempts
            )
            spent += c
            if isinstance(res, ProviderSuccess):
                return (
                    RouterOutcome(success=res, failed_closed=False, rescued=True, tier_reached=2, attempts=attempts),
                    spent,
                )
            if not isinstance(res, ProviderError) or res.kind != ProviderErrorKind.MAX_TOKENS:
                break
        return None, spent

    def _run_tier2(
        self,
        request: ProviderRequest,
        t1_err: ProviderError,
        page_number: int,
        ledger: CostLedger | None,
        attempts: list[RouterAttempt],
    ) -> tuple[RouterOutcome | None, float]:
        spent = 0.0
        primary_family = getattr(self.primary_provider, "name", "gemini")

        # 2a: If MAX_TOKENS hit below 32k/65k ceiling, retry on same primary up through 65536 BEFORE splitting
        mt_out, mt_c = self._try_max_tokens_retry(request, t1_err, page_number, ledger, attempts)
        spent += mt_c
        if mt_out is not None:
            return mt_out, spent

        # 2b: If RECITATION hit without scanline prefix, retry with `[L01]` scanline prefix first
        if t1_err.kind == ProviderErrorKind.RECITATION and not self.use_scanline_prefix:
            sl_req = _with_scanline_prefix(_for_non_gemini_family(request, self.prompt_fallback_text))
            res, c = self._call_tracked(
                self.primary_provider, sl_req, page_number, ledger, "tier2_scanline_retry", 2, attempts
            )
            spent += c
            if isinstance(res, ProviderSuccess):
                return RouterOutcome(success=res, failed_closed=False, rescued=True, tier_reached=2, attempts=attempts), spent

        # 2c: Same-tier 2-band horizontal split on primary BEFORE downgrading to alternate models
        if (
            t1_err.kind in {ProviderErrorKind.RECITATION, ProviderErrorKind.MAX_TOKENS}
            and len(request.image_paths) == 1
            and self.band_split_dir is not None
            and Path(request.image_paths[0]).is_file()
        ):
            band_res, band_c = self._try_two_band_split(request, page_number, ledger, attempts)
            spent += band_c
            if band_res is not None:
                return RouterOutcome(success=band_res, failed_closed=False, rescued=True, tier_reached=2, attempts=attempts), spent

        # 2d: Same-family alternate models in fallback_chain
        for alt in self._resolved_fallbacks():
            if getattr(alt, "name", "") != primary_family:
                continue
            alt_req = _for_non_gemini_family(request, self.prompt_fallback_text)
            if t1_err.kind == ProviderErrorKind.RECITATION or self.use_scanline_prefix:
                alt_req = _with_scanline_prefix(alt_req)
            res, c = self._call_tracked(alt, alt_req, page_number, ledger, "tier2_same_family_alt", 2, attempts)
            spent += c
            if isinstance(res, ProviderSuccess):
                return RouterOutcome(success=res, failed_closed=False, rescued=True, tier_reached=2, attempts=attempts), spent

        return None, spent

    def _try_two_band_split(
        self,
        request: ProviderRequest,
        page_number: int,
        ledger: CostLedger | None,
        attempts: list[RouterAttempt],
    ) -> tuple[ProviderSuccess | None, float]:
        assert self.band_split_dir is not None
        top_png, bot_png = split_image_two_horizontal_bands(
            request.image_paths[0], self.band_split_dir, f"p{page_number:04d}"
        )
        base_req = _with_scanline_prefix(_for_non_gemini_family(request, self.prompt_fallback_text))
        top_req = copy.deepcopy(base_req)
        top_req.image_paths = [str(top_png)]
        bot_req = copy.deepcopy(base_req)
        bot_req.image_paths = [str(bot_png)]

        top_res, c1 = self._call_tracked(self.primary_provider, top_req, page_number, ledger, "tier2_band_top", 2, attempts)
        if not isinstance(top_res, ProviderSuccess):
            return None, c1
        bot_res, c2 = self._call_tracked(self.primary_provider, bot_req, page_number, ledger, "tier2_band_bottom", 2, attempts)
        if not isinstance(bot_res, ProviderSuccess):
            return None, c1 + c2

        merged_wire = merge_two_band_wires(top_res.data, bot_res.data)
        merged_prov = dict(top_res.provenance)
        merged_prov["strategy"] = "two_band_horizontal_split"
        merged_prov["cost_estimate_usd"] = round(c1 + c2, 8)
        return ProviderSuccess(
            data=merged_wire,
            raw_response={"top": top_res.raw_response, "bottom": bot_res.raw_response},
            provenance=merged_prov,
        ), c1 + c2

    def _run_tier3(
        self,
        request: ProviderRequest,
        page_number: int,
        ledger: CostLedger | None,
        attempts: list[RouterAttempt],
        *,
        t1_err: ProviderError | None = None,
    ) -> tuple[RouterOutcome | None, float]:
        spent = 0.0
        primary_family = getattr(self.primary_provider, "name", "gemini")
        cross_req = _for_non_gemini_family(request, self.prompt_fallback_text)
        if t1_err is not None and t1_err.kind == ProviderErrorKind.RECITATION:
            cross_req = _with_scanline_prefix(cross_req)

        for backup in self._resolved_fallbacks():
            if getattr(backup, "name", "") == primary_family:
                continue
            res, c = self._call_tracked(backup, cross_req, page_number, ledger, "tier3_cross_family", 3, attempts)
            spent += c
            if isinstance(res, ProviderSuccess):
                return RouterOutcome(success=res, failed_closed=False, rescued=True, tier_reached=3, attempts=attempts), spent
            if ledger is not None and ledger.cancelled_event.is_set():
                break
        return None, spent

    def _call_tracked(
        self,
        provider: Any,
        request: ProviderRequest,
        page_number: int,
        ledger: CostLedger | None,
        stage_label: str,
        tier: int,
        attempts: list[RouterAttempt],
    ) -> tuple[ProviderResult, float]:
        prov_name = str(getattr(provider, "name", "unknown"))
        model_name = str(getattr(provider, "model", "unknown"))
        harness_name = str(getattr(provider, "harness", "sdk"))
        reserved = 0.0
        if ledger is not None and prov_name != "mock":
            est = conservative_call_reservation_usd(prov_name, model_name, root=self.repo_root)
            try:
                reserved = ledger.acquire_slot(est)
            except CostCeilingExceeded as exc:
                err = ProviderError(ProviderErrorKind.RATE_LIMITED, str(exc))
                attempts.append(
                    RouterAttempt(
                        tier=tier,
                        strategy=stage_label,
                        provider=prov_name,
                        model=model_name,
                        status="ceiling_blocked",
                        error_kind="cost_ceiling",
                        message=str(exc),
                    )
                )
                return err, 0.0

        res = provider.generate(request)
        prov_dict = res.provenance or {}
        call_cost = float(prov_dict.get("cost_estimate_usd") or 0.0)
        if ledger is not None and prov_name != "mock":
            if isinstance(res, ProviderSuccess) or call_cost > 0.0:
                ledger.release_and_record(
                    reserved,
                    stage=stage_label,
                    provider=prov_dict.get("provider", prov_name),
                    model=prov_dict.get("model", model_name),
                    harness=prov_dict.get("harness", harness_name),
                    cost_usd=call_cost,
                    page_number=page_number,
                    input_tokens=int(prov_dict.get("input_tokens") or 0),
                    cached_input_tokens=int(prov_dict.get("cached_input_tokens") or 0),
                    output_tokens=int(prov_dict.get("output_tokens") or 0),
                    thoughts_tokens=int(prov_dict.get("thoughts_tokens") or 0),
                    status="ok" if isinstance(res, ProviderSuccess) else f"error:{res.kind.value}",
                )
            else:
                ledger.release_without_spend(reserved)

        if isinstance(res, ProviderSuccess):
            attempts.append(
                RouterAttempt(
                    tier=tier,
                    strategy=stage_label,
                    provider=prov_name,
                    model=model_name,
                    status="ok",
                    cost_usd=call_cost,
                )
            )
        else:
            attempts.append(
                RouterAttempt(
                    tier=tier,
                    strategy=stage_label,
                    provider=prov_name,
                    model=model_name,
                    status="error",
                    error_kind=res.kind.value,
                    message=res.message,
                    cost_usd=call_cost,
                )
            )
        return res, call_cost

    @staticmethod
    def _fail_closed(
        reason: str,
        total_cost: float,
        attempts: list[RouterAttempt],
        *,
        tier: int,
    ) -> RouterOutcome:
        return RouterOutcome(
            success=None,
            failed_closed=True,
            failure_reason=reason,
            rescued=False,
            tier_reached=tier,
            total_cost_usd=round(total_cost, 8),
            attempts=attempts,
        )
