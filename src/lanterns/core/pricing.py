"""Fail-closed model pricing table supporting standard, cached-input, cache-write, and Batch rates."""

from __future__ import annotations

import functools
import json
from datetime import date
from typing import Any

from lanterns.core.paths import RepoPaths


def parse_model_route(spec: str, default_provider: str = "gemini") -> tuple[str, str]:
    """Split `'gemini/gemini-3.8-flash'` or `'openrouter/qwen/qwen3.8-flash'` into `(provider, model)`."""
    clean = (spec or "").strip()
    if not clean:
        raise ValueError("Empty model route specification")
    if "/" in clean:
        provider, model = clean.split("/", 1)
        return provider.strip(), model.strip()
    return default_provider, clean


@functools.lru_cache(maxsize=8)
def load_pricing(root: str | None = None) -> dict[str, Any]:
    path = RepoPaths.from_root(root).config_dir / "pricing.json"
    if not path.is_file():
        raise FileNotFoundError(f"Pricing table missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def lookup_rates(table: dict[str, Any], provider: str, model: str) -> dict[str, Any] | None:
    clean_model = model.removeprefix("models/").strip()
    exact_key = f"{provider}/{clean_model}"
    if exact_key in table:
        return table[exact_key]
    base_model = clean_model.split(":", 1)[0]
    return table.get(f"{provider}/{base_model}")


def ensure_priced_routes(routes: list[tuple[str, str | None] | str], *, root: str | None = None) -> None:
    """Fail closed before spending a single token if any configured route is missing from `config/pricing.json`."""
    table = load_pricing(root)
    missing: list[str] = []
    for item in routes:
        if isinstance(item, str):
            if not item.strip():
                continue
            provider, model = parse_model_route(item)
        else:
            provider, raw_model = item
            if not provider or not raw_model:
                continue
            model = raw_model
        if lookup_rates(table, provider, model) is None:
            missing.append(f"{provider}/{model}")
    if missing:
        unique = ", ".join(sorted(set(missing)))
        raise ValueError(f"Unpriced model route(s): {unique}. Register rates in config/pricing.json first.")


def rates_effective_on(rates: dict[str, Any], on_date: date) -> dict[str, Any]:
    scheduled = rates.get("scheduled_rates")
    if not scheduled:
        return rates
    resolved = dict(rates)
    for entry in sorted(scheduled, key=lambda item: str(item.get("effective_from", ""))):
        effective_from = entry.get("effective_from")
        if not effective_from or date.fromisoformat(str(effective_from)) > on_date:
            continue
        resolved.update({k: v for k, v in entry.items() if k != "effective_from"})
    return resolved


def estimate_cost_usd(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    thoughts_tokens: int = 0,
    *,
    cached_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
    is_batch: bool = False,
    service_tier: str | None = None,
    explicit_cost: float | None = None,
    on_date: date | None = None,
    root: str | None = None,
) -> float:
    if explicit_cost is not None and explicit_cost > 0:
        return float(explicit_cost)
    rates = lookup_rates(load_pricing(root), provider, model)
    if rates is None:
        raise ValueError(f"Cannot estimate cost for unpriced model {provider}/{model}")
    rates = rates_effective_on(rates, on_date or date.today())
    cached = min(max(int(cached_input_tokens), 0), max(int(input_tokens), 0))
    written = min(max(int(cache_write_input_tokens), 0), max(int(input_tokens) - cached, 0))
    uncached = max(int(input_tokens) - cached - written, 0)
    is_discounted = is_batch or (str(service_tier or "").strip().lower() == "flex")
    batch_mult = float(rates.get("batch_discount_multiplier", 1.0)) if is_discounted else 1.0

    in_rate = float(rates.get("input_per_million", 0.0))
    cache_rate = float(rates.get("cached_input_per_million", in_rate))
    write_rate = float(rates.get("cache_write_per_million", in_rate))
    out_rate = float(rates.get("output_per_million", 0.0))
    thought_rate = float(rates.get("thoughts_per_million", out_rate))

    total_micro = (
        uncached * in_rate * batch_mult
        + cached * cache_rate * batch_mult
        + written * write_rate * batch_mult
        + int(output_tokens) * out_rate * batch_mult
        + int(thoughts_tokens) * thought_rate * batch_mult
    )
    return round(total_micro / 1_000_000, 8)


def conservative_call_reservation_usd(
    provider: str,
    model: str | None,
    *,
    is_crop: bool = False,
    on_date: date | None = None,
    root: str | None = None,
) -> float:
    """Upper-bound pre-call slot reservation so concurrent workers never overshoot `--cost-ceiling-usd`."""
    fallback = 0.003 if is_crop else 0.006
    if not provider or not model:
        return fallback
    if lookup_rates(load_pricing(root), provider, model) is None:
        raise ValueError(f"Unpriced model route: {provider}/{model}. Register rates in config/pricing.json first.")
    in_tok, out_tok, th_tok = (1800, 700, 700) if is_crop else (3600, 2800, 1800)
    est = estimate_cost_usd(provider, model, in_tok, out_tok, th_tok, on_date=on_date, root=root)
    return max(fallback if est > 0 else 0.0, round(est, 6))
