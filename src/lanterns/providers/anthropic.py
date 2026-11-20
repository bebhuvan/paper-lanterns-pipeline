"""Official Anthropic SDK (`anthropic >= 2.8.0`) vision & structured-output adapter.

Supports all Claude Opus, Sonnet, and Haiku generations (`claude-opus-5-5`, `claude-opus-5`,
`claude-opus-4-8`, `claude-sonnet-5-5`, `claude-sonnet-5`, `claude-sonnet-4-6`,
`claude-haiku-5-5`, `claude-haiku-4-5-20251001`) with:
  - Native constrained JSON decoding via `output_config.format` (`type="json_schema"`)
  - Claude 5.5 effort & thinking compliance (`output_config.effort`, adaptive thinking on Opus 5.5,
    `thinking={"type": "between_tools"}` low-overhead mode on Sonnet 5.5, never sending banned `"disabled"`)
  - Ephemeral prompt caching (`cache_control={"type": "ephemeral"}`)
  - Message Batches API (`submit_message_batch` via `client.messages.batches.create`)
"""

from __future__ import annotations

import base64
import copy
import importlib.metadata
import io
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

import anthropic
from PIL import Image

from lanterns.core.config import load_schema
from lanterns.core.env import load_env
from lanterns.core.hashes import sha256_json
from lanterns.core.pricing import estimate_cost_usd
from lanterns.providers.base import (
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResult,
    ProviderSuccess,
    validate_wire_if_requested,
)

MAX_EDGE_PX = 2576
MAX_VISUAL_TOKENS = 4784
PATCH_PX = 28
MAX_IMAGE_BASE64_BYTES = 10 * 1024 * 1024
MIN_ANTHROPIC_SDK = (2, 8, 0)

_EFFORT_BY_LEVEL = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

_STOP_ERRORS = {
    "max_tokens": ProviderErrorKind.MAX_TOKENS,
    "model_context_window_exceeded": ProviderErrorKind.MAX_TOKENS,
    "refusal": ProviderErrorKind.SAFETY,
}


def check_anthropic_sdk_version() -> tuple[bool, str]:
    """Verify installed `anthropic` SDK meets >= 2.8.0 floor."""
    try:
        ver_str = importlib.metadata.version("anthropic")
    except importlib.metadata.PackageNotFoundError:
        return False, "missing"
    parts: list[int] = []
    for seg in ver_str.split(".")[:3]:
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3]) >= MIN_ANTHROPIC_SDK, ver_str


def visual_tokens(width: int, height: int) -> int:
    return math.ceil(width / PATCH_PX) * math.ceil(height / PATCH_PX)


def fits_claude(width: int, height: int) -> bool:
    return (
        math.ceil(width / PATCH_PX) * PATCH_PX <= MAX_EDGE_PX
        and math.ceil(height / PATCH_PX) * PATCH_PX <= MAX_EDGE_PX
        and visual_tokens(width, height) <= MAX_VISUAL_TOKENS
    )


def claude_fit_size(width: int, height: int) -> tuple[int, int]:
    """Largest aspect-preserving size within Claude's vision patch budget."""
    if fits_claude(width, height):
        return width, height
    if height > width:
        h, w = claude_fit_size(height, width)
        return w, h
    aspect = width / max(1, height)
    lo, hi = 1, width
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if fits_claude(mid, max(round(mid / aspect), 1)):
            lo = mid
        else:
            hi = mid
    return lo, max(round(lo / aspect), 1)


def _split_nullable(node: dict[str, Any]) -> dict[str, Any]:
    types = node.get("type")
    if not isinstance(types, list) or "null" not in types:
        return node
    rest = [t for t in types if t != "null"]
    branch = {k: v for k, v in node.items() if k not in ("type", "description", "default")}
    branch["type"] = rest[0] if len(rest) == 1 else rest
    if "enum" in branch:
        branch["enum"] = [v for v in branch["enum"] if v is not None]
    out: dict[str, Any] = {"anyOf": [branch, {"type": "null"}]}
    if "description" in node:
        out["description"] = node["description"]
    return out


def _allows_null(prop_schema: Any) -> bool:
    if not isinstance(prop_schema, dict):
        return False
    ptype = prop_schema.get("type")
    if ptype == "null" or (isinstance(ptype, list) and "null" in ptype):
        return True
    return any(isinstance(opt, dict) and opt.get("type") == "null" for opt in (prop_schema.get("anyOf") or []))


def reconcile_claude_wire_output(data: Any, schema: dict[str, Any] | None) -> Any:
    """Drop `null` emitted for optional non-nullable fields and clamp edge `bbox` ints (`0..1000`)."""
    if schema is None or not isinstance(data, dict):
        return data
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    req = set(schema.get("required") or [])
    out: dict[str, Any] = {}
    for key, val in data.items():
        pschema = props.get(key)
        if val is None and key not in req and not _allows_null(pschema):
            continue
        if key == "bbox" and isinstance(val, list) and len(val) == 4 and all(isinstance(x, int) for x in val):
            out[key] = [max(0, min(1000, x)) for x in val]
        elif isinstance(val, list) and isinstance(pschema, dict) and isinstance(pschema.get("items"), dict):
            item_schema = pschema["items"]
            out[key] = [reconcile_claude_wire_output(it, item_schema) if isinstance(it, dict) else it for it in val]
        elif isinstance(val, dict) and isinstance(pschema, dict):
            out[key] = reconcile_claude_wire_output(val, pschema)
        else:
            out[key] = val
    return out


def claude_clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert Draft 2020-12 JSON Schema into the strict subset accepted by Claude `output_config.format`."""
    unsupported = {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "maxItems",
        "minLength",
        "maxLength",
        "default",
    }

    def _clean(node: Any) -> Any:
        if isinstance(node, list):
            return [_clean(item) for item in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        moved: list[str] = []
        for key, val in node.items():
            if key in {"$schema", "$id", "title"}:
                continue
            if key in unsupported or (key == "minItems" and val not in (0, 1)):
                moved.append(f"{key}: {val}")
                continue
            out[key] = _clean(val)
        if out.get("type") == "object" and "additionalProperties" not in out:
            out["additionalProperties"] = False
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            # Claude constrained decoding requires every property to be listed in `required`
            # (optional properties use nullable `anyOf` types).
            all_props = list(out["properties"].keys())
            existing_req = set(out.get("required") or [])
            for pk, pv in list(out["properties"].items()):
                if pk not in existing_req and isinstance(pv, dict) and "anyOf" not in pv:
                    prop_copy = dict(pv)
                    desc = prop_copy.pop("description", None)
                    wrapped: dict[str, Any] = {"anyOf": [prop_copy, {"type": "null"}]}
                    if desc:
                        wrapped["description"] = desc
                    out["properties"][pk] = wrapped
            out["required"] = all_props
        if moved:
            suffix = "{" + ", ".join(moved) + "}"
            out["description"] = f"{out['description']} {suffix}" if out.get("description") else suffix
        return _split_nullable(out)

    return _clean(copy.deepcopy(schema))


def encode_claude_page_image(image_path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with Image.open(image_path) as opened:
        page = opened.convert("L") if opened.mode in ("L", "LA", "1", "I;16") else opened.convert("RGB")
    width, height = page.size
    fit_w, fit_h = claude_fit_size(width, height)
    downscaled = (fit_w, fit_h) != (width, height)
    target = page.resize((fit_w, fit_h), Image.Resampling.LANCZOS) if downscaled else page
    buf = io.BytesIO()
    target.save(buf, format="WEBP", quality=94, method=3)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    if len(b64) > MAX_IMAGE_BASE64_BYTES:
        raise ValueError(f"Encoded WebP image ({len(b64)} base64 bytes) exceeds 10 MB limit")
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/webp", "data": b64}}
    meta = {"source_px": [width, height], "sent_px": [fit_w, fit_h], "downscaled": downscaled}
    return block, meta


def resolve_claude_thinking_and_effort(api_model: str, level: str) -> tuple[str | None, dict[str, Any] | None, int]:
    """Return `(effort, thinking_param, min_max_tokens)` following Claude 4.5 / 5 / 5.5 platform rules."""
    norm_level = (level or "low").strip().lower()
    effort = _EFFORT_BY_LEVEL.get(norm_level, "low")
    low_overhead = norm_level in {"minimal", "low"}

    if api_model.startswith("claude-opus-5-5"):
        # Opus 5.5: thinking is always adaptive; sending `disabled` or `enabled` returns HTTP 400.
        return effort, None, 20480
    if api_model.startswith("claude-sonnet-5-5"):
        # Sonnet 5.5: `between_tools` turns off upfront thinking cleanly at low/medium/high effort.
        thinking_cfg = {"type": "between_tools"} if low_overhead else None
        return effort, thinking_cfg, 16384
    if api_model.startswith("claude-haiku-5-5"):
        return effort, None, 16384
    if any(api_model.startswith(p) for p in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-", "claude-sonnet-4-6", "claude-fable-")):
        return effort, None, 16384
    # Earlier 4.5 snapshot models without `output_config.effort`
    return None, None, 12288


def _supports_structured_output_config(api_model: str) -> bool:
    return api_model.startswith(
        (
            "claude-opus-5",
            "claude-sonnet-5",
            "claude-haiku-5",
            "claude-fable-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-opus-4-5",
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
        )
    )


def _extract_text_from_blocks(content_blocks: list[Any]) -> str:
    parts: list[str] = []
    for blk in content_blocks:
        btype = blk.get("type") if isinstance(blk, dict) else getattr(blk, "type", None)
        if btype == "text":
            txt = blk.get("text", "") if isinstance(blk, dict) else getattr(blk, "text", "")
            parts.append(str(txt))
    return "".join(parts).strip()


def _parse_json_text(text: str) -> dict[str, Any] | None:
    cleaned = text
    if cleaned.startswith("```"):
        cleaned = "\n".join(ln for ln in cleaned.splitlines() if not ln.strip().startswith("```")).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        start_idx, end_idx = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start_idx < end_idx:
            try:
                parsed = json.loads(cleaned[start_idx : end_idx + 1])
                return parsed if isinstance(parsed, dict) else None
            except ValueError:
                return None
        return None


class AnthropicProvider:
    """Official `anthropic` SDK provider supporting all Opus, Sonnet, and Haiku models."""

    name = "anthropic"
    pricing_family = "anthropic"
    harness = "anthropic_sdk"

    def __init__(
        self,
        model: str = "claude-sonnet-5-5",
        *,
        api_key: str | None = None,
        max_retries: int = 3,
        base_backoff_s: float = 2.0,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        load_env()
        self.model = model
        self.api_model = model.split(":", 1)[0]
        self.api_key = os.getenv("ANTHROPIC_API_KEY") if api_key is None else api_key
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self._client = client

    def _get_client(self, timeout_s: float) -> anthropic.Anthropic:
        if self._client is not None:
            return self._client
        return anthropic.Anthropic(api_key=self.api_key, timeout=timeout_s, max_retries=0)

    def build_payload(
        self, request: ProviderRequest, *, use_structured_format: bool = True
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        level = str(request.settings.get("thinking_level") or "low").strip().lower()
        effort, thinking_param, min_tokens = resolve_claude_thinking_and_effort(self.api_model, level)
        req_max = int(request.settings.get("max_output_tokens") or 0)
        output_cap = max(req_max, min_tokens)

        schema_obj = request.schema_dict or (load_schema(request.schema_name) if request.schema_name else None)
        cleaned_schema = claude_clean_schema(schema_obj) if schema_obj is not None else None
        use_native_schema = (
            use_structured_format and cleaned_schema is not None and _supports_structured_output_config(self.api_model)
        )

        prompt_text = request.prompt
        if cleaned_schema is not None and not use_native_schema:
            prompt_text = (
                f"{prompt_text}\n\nReturn ONLY a single valid JSON object matching this schema:\n"
                f"{json.dumps(cleaned_schema, ensure_ascii=False)}"
            )

        content: list[dict[str, Any]] = []
        if prompt_text:
            text_part: dict[str, Any] = {"type": "text", "text": prompt_text}
            if request.settings.get("enable_context_cache", True):
                text_part["cache_control"] = {"type": "ephemeral"}
            content.append(text_part)

        img_records: list[dict[str, Any]] = []
        for img_path in request.image_paths:
            blk, rec = encode_claude_page_image(img_path)
            content.append(blk)
            img_records.append(rec)
        if request.dynamic_suffix:
            content.append({"type": "text", "text": request.dynamic_suffix})

        kwargs: dict[str, Any] = {
            "model": self.api_model,
            "max_tokens": output_cap,
            "messages": [{"role": "user", "content": content}],
        }
        sys_inst = request.settings.get("system_instruction")
        if sys_inst:
            kwargs["system"] = [{"type": "text", "text": str(sys_inst)}]
        if thinking_param is not None:
            kwargs["thinking"] = thinking_param

        output_config: dict[str, Any] = {}
        if effort is not None:
            output_config["effort"] = effort
        if use_native_schema and cleaned_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": cleaned_schema}
        if output_config:
            kwargs["output_config"] = output_config

        return kwargs, {"effort": effort, "images": img_records, "structured_format": use_native_schema}

    def generate(self, request: ProviderRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderError(ProviderErrorKind.AUTH, "ANTHROPIC_API_KEY is not set")
        timeout_s = max(45.0, float(request.settings.get("timeout_ms", 300_000)) / 1000.0)
        client = self._get_client(timeout_s)
        use_structured = True
        started = time.time()
        last_err: ProviderError | None = None

        for attempt in range(self.max_retries + 1):
            try:
                kwargs, meta = self.build_payload(request, use_structured_format=use_structured)
                msg = client.messages.create(**kwargs)
                raw = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
                return self._parse_response(request, raw, started, meta)
            except anthropic.BadRequestError as exc:
                msg_str = str(exc)
                if use_structured and ("output_config" in msg_str or "schema" in msg_str):
                    use_structured = False
                    continue
                kind = (
                    ProviderErrorKind.RECITATION
                    if "content filtering policy" in msg_str.lower()
                    else ProviderErrorKind.UNKNOWN
                )
                return ProviderError(kind, self._redact(f"Anthropic 400: {exc}"))
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                return ProviderError(ProviderErrorKind.AUTH, self._redact(f"Anthropic auth error: {exc}"))
            except anthropic.APITimeoutError:
                last_err = ProviderError(ProviderErrorKind.TIMEOUT, "Anthropic request timed out")
            except anthropic.RateLimitError as exc:
                last_err = ProviderError(ProviderErrorKind.RATE_LIMITED, self._redact(str(exc)))
            except anthropic.APIStatusError as exc:
                kind = ProviderErrorKind.PROVIDER_UNAVAILABLE if exc.status_code >= 500 else ProviderErrorKind.UNKNOWN
                last_err = ProviderError(kind, self._redact(f"Anthropic HTTP {exc.status_code}: {exc}"))
                if exc.status_code < 500 and exc.status_code != 408:
                    return last_err
            except (OSError, ValueError, anthropic.APIConnectionError) as exc:
                last_err = ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, self._redact(str(exc)))

            if attempt < self.max_retries:
                time.sleep(self.base_backoff_s * (2**attempt) + random.uniform(0.1, 0.5))
        return last_err or ProviderError(ProviderErrorKind.UNKNOWN, "Exhausted Anthropic retries")

    def submit_message_batch(self, batch_requests: list[tuple[str, ProviderRequest]]) -> dict[str, Any]:
        """Submit a 50%-discounted Anthropic Message Batch (`client.messages.batches.create`)."""
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        client = self._get_client(120.0)
        items: list[dict[str, Any]] = []
        for custom_id, req in batch_requests:
            params, _ = self.build_payload(req, use_structured_format=True)
            items.append({"custom_id": custom_id, "params": params})
        batch = client.messages.batches.create(requests=items)  # type: ignore[arg-type]
        return batch.model_dump() if hasattr(batch, "model_dump") else {"id": getattr(batch, "id", "")}

    def retrieve_message_batch(self, batch_id: str) -> dict[str, Any]:
        """Retrieve live status and request counts for an Anthropic Message Batch (`client.messages.batches.retrieve`)."""
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        batch = self._get_client(60.0).messages.batches.retrieve(batch_id)
        return batch.model_dump(mode="json") if hasattr(batch, "model_dump") else {"id": batch_id}

    def collect_message_batch_results(
        self,
        batch_id: str,
        requests_by_id: dict[str, ProviderRequest],
    ) -> dict[str, ProviderResult]:
        """Stream and validate results for a completed Anthropic Message Batch (`client.messages.batches.results`)."""
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        client = self._get_client(180.0)
        results: dict[str, ProviderResult] = {}
        started = time.time()
        for entry in client.messages.batches.results(batch_id):
            cid = str(getattr(entry, "custom_id", "") or "")
            req = requests_by_id.get(cid)
            if req is None:
                continue
            res_obj = getattr(entry, "result", None)
            rtype = str(getattr(res_obj, "type", "") or "")
            if rtype == "succeeded":
                msg = getattr(res_obj, "message", None)
                raw = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg or {})
                level = str(req.settings.get("thinking_level") or "low").strip().lower()
                effort, _, _ = resolve_claude_thinking_and_effort(self.api_model, level)
                meta = {"effort": effort, "images": [], "structured_format": True}
                results[cid] = self._parse_response(req, raw, started, meta, is_batch=True)
            else:
                err_detail = str(getattr(res_obj, "error", rtype) or rtype)
                kind = (
                    ProviderErrorKind.RECITATION
                    if "content filtering policy" in err_detail.lower()
                    else ProviderErrorKind.UNKNOWN
                )
                results[cid] = ProviderError(
                    kind,
                    self._redact(f"Anthropic batch item {rtype}: {err_detail}"),
                )
        return results

    def _parse_response(
        self,
        request: ProviderRequest,
        raw: dict[str, Any],
        started: float,
        meta: dict[str, Any],
        *,
        is_batch: bool = False,
    ) -> ProviderResult:
        prov = self._provenance(raw, started, meta, is_batch=is_batch)
        stop = raw.get("stop_reason")
        if stop not in ("end_turn", None):
            return ProviderError(
                _STOP_ERRORS.get(str(stop), ProviderErrorKind.UNKNOWN),
                f"Anthropic stopped with stop_reason={stop}",
                raw_response=raw,
                provenance=prov,
            )
        text = _extract_text_from_blocks(raw.get("content") or [])
        data = _parse_json_text(text)
        if data is None:
            return ProviderError(
                ProviderErrorKind.SCHEMA_INVALID,
                "Anthropic response was not valid JSON",
                raw_response=raw,
                provenance=prov,
            )
        schema_obj = request.schema_dict or (load_schema(request.schema_name) if request.schema_name else None)
        data = reconcile_claude_wire_output(data, schema_obj)
        err = validate_wire_if_requested(request, data, raw_response=raw, provenance=prov)
        if err is not None:
            return err
        return ProviderSuccess(data=data, raw_response=raw, provenance=prov)

    def _provenance(
        self,
        raw: dict[str, Any],
        started: float,
        meta: dict[str, Any],
        *,
        is_batch: bool = False,
    ) -> dict[str, Any]:
        usage = raw.get("usage") or {}
        uncached_in = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        total_out = int(usage.get("output_tokens") or 0)
        thinking = int((usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
        visible_out = max(0, total_out - thinking)
        total_in = uncached_in + cache_read + cache_write
        cost = estimate_cost_usd(
            self.pricing_family,
            self.model,
            total_in,
            visible_out,
            thinking,
            cached_input_tokens=cache_read,
            cache_write_input_tokens=cache_write,
            is_batch=is_batch,
        )
        return {
            "provider": self.name,
            "model": self.api_model,
            "harness": "anthropic_batch" if is_batch else self.harness,
            "is_batch": is_batch,
            "effort": meta.get("effort"),
            "structured_format": meta.get("structured_format", False),
            "images": meta["images"],
            "finish_reason": raw.get("stop_reason"),
            "input_tokens": total_in,
            "cached_input_tokens": cache_read,
            "cache_write_input_tokens": cache_write,
            "output_tokens": visible_out,
            "thoughts_tokens": thinking,
            "cost_estimate_usd": cost,
            "latency_ms": int((time.time() - started) * 1000),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "response_hash": sha256_json(raw),
        }

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "[REDACTED]") if self.api_key else text
