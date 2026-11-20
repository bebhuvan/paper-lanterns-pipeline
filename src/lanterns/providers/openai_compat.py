"""Multi-provider OpenAI-compatible vision adapter via `httpx` (`openrouter`, `qwen_cloud`, `deepseek`, `openai`).

Supports Qwen 4 (`qwen4-max` on QwenCloud Global `global.maas.qwencloudapi.com` / Singapore `ap-southeast-1`)
with `reasoning_effort` control, high-detail image blocks, and ephemeral prompt caching alongside Qwen 3.x models.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from lanterns.core.config import gemini_clean_schema, load_schema
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

ENDPOINTS: dict[str, tuple[str, str]] = {
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions"),
    "qwen_cloud": ("DASHSCOPE_API_KEY", "https://global.maas.qwencloudapi.com/compatible-mode/v1/chat/completions"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/chat/completions"),
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions"),
}

_QWEN4_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "low",
    "high": "high",
    "xhigh": "xhigh",
}

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def is_qwen4_model(model: str) -> bool:
    """Return True when `model` belongs to the Qwen 4 family (`qwen4-max`, `qwen/qwen4*`)."""
    tail = (model or "").strip().lower().rsplit("/", 1)[-1]
    return tail.startswith("qwen4")


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = _FENCE_RE.sub("", raw_text or "").strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if 0 <= start < end:
            parsed = json.loads(cleaned[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
    raise ValueError("Response did not contain a valid JSON object")


def _resolve_api_key(provider_name: str, env_var: str, explicit_key: str | None) -> str | None:
    if explicit_key is not None:
        return explicit_key
    if provider_name == "qwen_cloud":
        return os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_CLOUD_API_KEY")
    return os.getenv(env_var)


def _resolve_endpoint(provider_name: str, default_url: str, explicit_url: str | None) -> str:
    if explicit_url:
        return explicit_url
    if provider_name == "qwen_cloud":
        custom = os.getenv("QWEN_CLOUD_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL")
        if custom:
            base = custom.rstrip("/")
            return base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    return default_url


def _apply_qwen_cloud_params(body: dict[str, Any], model: str, settings: dict[str, Any]) -> None:
    """Configure Qwen 4 (`reasoning_effort`) vs Qwen 3 (`vl_high_resolution_images`, `enable_thinking`) parameters."""
    if is_qwen4_model(model):
        level = str(settings.get("thinking_level") or "low").strip().lower()
        body["reasoning_effort"] = str(settings.get("reasoning_effort") or _QWEN4_EFFORT_MAP.get(level, "low"))
        if "max_tokens" not in body:
            body["max_tokens"] = 16384
    else:
        body["vl_high_resolution_images"] = bool(settings.get("vl_high_resolution_images", True))
        body["enable_thinking"] = bool(settings.get("enable_thinking", False))
        if "max_tokens" not in body:
            body["max_tokens"] = 12288


class OpenAICompatProvider:
    """Unified `httpx`-backed OpenAI-compatible VLM adapter for OpenRouter, QwenCloud (Qwen 4 & 3), DeepSeek, and OpenAI."""

    harness = "httpx_openai_compat"

    def __init__(
        self,
        provider_name: str = "openrouter",
        model: str = "qwen/qwen3.8-flash",
        *,
        api_key: str | None = None,
        endpoint_url: str | None = None,
        max_retries: int = 3,
        http_client: httpx.Client | None = None,
    ) -> None:
        load_env()
        if provider_name not in ENDPOINTS and endpoint_url is None:
            raise ValueError(f"Unknown OpenAI-compatible provider: {provider_name!r}")
        env_var, default_url = ENDPOINTS.get(provider_name, ("OPENAI_API_KEY", ""))
        self.name = provider_name
        self.pricing_family = provider_name
        self.model = model
        self.api_key = _resolve_api_key(provider_name, env_var, api_key)
        self.endpoint_url = _resolve_endpoint(provider_name, default_url, endpoint_url)
        self.max_retries = max_retries
        self._http_client = http_client

    def generate(self, request: ProviderRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderError(ProviderErrorKind.AUTH, f"API key missing for {self.name}")

        started = time.time()
        payload = self._build_payload(request, include_response_format=True)
        headers = self._build_headers()
        timeout_s = max(30.0, float(request.settings.get("timeout_ms", 240_000)) / 1000.0)

        for attempt in range(self.max_retries + 1):
            resp_or_err = self._post_once(headers, payload, timeout_s)
            if isinstance(resp_or_err, ProviderError):
                if resp_or_err.kind == ProviderErrorKind.TIMEOUT and attempt < self.max_retries:
                    time.sleep(1.5 * (2**attempt))
                    continue
                return resp_or_err

            resp = resp_or_err
            if resp.status_code in {401, 403}:
                return ProviderError(ProviderErrorKind.AUTH, f"{self.name} auth error ({resp.status_code})")
            if resp.status_code == 400 and "response_format" in payload:
                payload = self._build_payload(request, include_response_format=False)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < self.max_retries:
                    time.sleep(1.5 * (2**attempt) + random.uniform(0.1, 0.5))
                    continue
                kind = (
                    ProviderErrorKind.RATE_LIMITED
                    if resp.status_code == 429
                    else ProviderErrorKind.PROVIDER_UNAVAILABLE
                )
                return ProviderError(kind, f"{self.name} HTTP {resp.status_code}")
            if resp.status_code >= 400:
                return ProviderError(
                    ProviderErrorKind.UNKNOWN,
                    self._redact(f"{self.name} HTTP {resp.status_code}: {resp.text[:300]}"),
                )
            return self._parse_response(request, resp.json(), started)

        return ProviderError(ProviderErrorKind.UNKNOWN, f"Exhausted retries for {self.name}")

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.name == "openrouter":
            headers["HTTP-Referer"] = "https://paperlanterns.org"
            headers["X-Title"] = "Paper Lanterns v2 Archival Digitization"
        return headers

    def _post_once(
        self, headers: dict[str, str], payload: dict[str, Any], timeout_s: float
    ) -> httpx.Response | ProviderError:
        try:
            if self._http_client is not None:
                return self._http_client.post(self.endpoint_url, headers=headers, json=payload, timeout=timeout_s)
            with httpx.Client(timeout=timeout_s) as client:
                return client.post(self.endpoint_url, headers=headers, json=payload)
        except httpx.TimeoutException:
            return ProviderError(ProviderErrorKind.TIMEOUT, f"{self.name} request timed out")
        except httpx.HTTPError as exc:
            return ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, self._redact(str(exc)))

    def _build_payload(self, request: ProviderRequest, *, include_response_format: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        sys_inst = request.settings.get("system_instruction")
        if sys_inst:
            messages.append({"role": "system", "content": str(sys_inst)})

        prompt_text = request.prompt
        schema_obj = request.schema_dict or (load_schema(request.schema_name) if request.schema_name else None)
        if schema_obj is not None:
            schema_json = json.dumps(gemini_clean_schema(schema_obj), ensure_ascii=False)
            prompt_text = f"{prompt_text}\n\nReturn ONLY a single valid JSON object matching this schema:\n{schema_json}"

        text_part: dict[str, Any] = {"type": "text", "text": prompt_text}
        if self.name == "qwen_cloud" and is_qwen4_model(self.model) and request.settings.get("enable_context_cache", True):
            text_part["cache_control"] = {"type": "ephemeral"}
        user_parts: list[dict[str, Any]] = [text_part]

        for image_path in request.image_paths:
            mime = mimetypes.guess_type(image_path)[0] or "image/png"
            b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
            img_obj: dict[str, Any] = {"url": f"data:{mime};base64,{b64}"}
            if self.name == "qwen_cloud" and is_qwen4_model(self.model):
                img_obj["detail"] = "high"
            user_parts.append({"type": "image_url", "image_url": img_obj})
        if request.dynamic_suffix:
            user_parts.append({"type": "text", "text": request.dynamic_suffix})

        messages.append({"role": "user", "content": user_parts})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if include_response_format and (request.schema_name or request.schema_dict):
            body["response_format"] = {"type": "json_object"}
        if "temperature" in request.settings and request.settings["temperature"] is not None:
            body["temperature"] = float(request.settings["temperature"])
        if request.settings.get("max_output_tokens"):
            body["max_tokens"] = int(request.settings["max_output_tokens"])
        if self.name == "qwen_cloud":
            _apply_qwen_cloud_params(body, self.model, request.settings)
        return body

    def _parse_response(self, request: ProviderRequest, raw: dict[str, Any], started: float) -> ProviderResult:
        choices = raw.get("choices") or []
        finish = choices[0].get("finish_reason") if choices else None
        prov = self._build_provenance(raw, finish, started)
        if finish == "length":
            return ProviderError(
                ProviderErrorKind.MAX_TOKENS, f"{self.name} truncated output", raw_response=raw, provenance=prov
            )
        if finish == "content_filter":
            return ProviderError(
                ProviderErrorKind.SAFETY, f"{self.name} content filter triggered", raw_response=raw, provenance=prov
            )
        msg_content = (choices[0].get("message") or {}).get("content", "") if choices else ""
        if isinstance(msg_content, list):
            msg_content = "".join(str(p.get("text", "")) for p in msg_content if isinstance(p, dict))
        try:
            data = _extract_json_object(str(msg_content))
        except ValueError as exc:
            return ProviderError(
                ProviderErrorKind.SCHEMA_INVALID,
                f"{self.name} returned non-JSON: {exc}",
                raw_response=raw,
                provenance=prov,
            )

        err = validate_wire_if_requested(request, data, raw_response=raw, provenance=prov)
        if err is not None:
            return err
        return ProviderSuccess(data=data, raw_response=raw, provenance=prov)

    def _build_provenance(self, raw: dict[str, Any], finish: Any, started: float) -> dict[str, Any]:
        usage = raw.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        total_out_tok = int(usage.get("completion_tokens") or 0)
        thought_tok = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        visible_out_tok = max(0, total_out_tok - thought_tok)
        prompt_details = usage.get("prompt_tokens_details") or {}
        cached_tok = int(prompt_details.get("cached_tokens") or 0)
        cache_write_tok = int(prompt_details.get("cache_creation_input_tokens") or 0)
        cost = estimate_cost_usd(
            self.pricing_family,
            self.model,
            in_tok,
            visible_out_tok,
            thought_tok,
            cached_input_tokens=cached_tok,
            cache_write_input_tokens=cache_write_tok,
        )
        return {
            "provider": self.name,
            "model": self.model,
            "harness": self.harness,
            "endpoint": self.endpoint_url,
            "finish_reason": finish,
            "input_tokens": in_tok,
            "cached_input_tokens": cached_tok,
            "cache_write_input_tokens": cache_write_tok,
            "output_tokens": visible_out_tok,
            "thoughts_tokens": thought_tok,
            "cost_estimate_usd": cost,
            "latency_ms": int((time.time() - started) * 1000),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "response_hash": sha256_json(raw),
        }

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "[REDACTED]") if self.api_key else text
