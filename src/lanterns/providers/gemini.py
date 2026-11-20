"""Native Google GenAI SDK (`google-genai >= 3.2.0`) adapter.

Supports:
  - Current 2026 models (`gemini-4-hydrogen`, `gemini-4-argon`, `gemini-4-carbon`,
    `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-decision-1`)
  - Both `generate_content` (`v1alpha`) and `Interactions API` (`client.interactions.create` on `v1`)
  - `Decisions API` (`client.decisions.create` with `models/gemini-decision-1` on `v1beta`)
  - `Batch API` (`client.batches`) and `Explicit Context Caching` (`client.caches` for non-Gemini-4 models)
  - Prefix-ordered implicit caching (static prompt text FIRST before image bytes)
  - Archival `BLOCK_NONE` safety settings, `MEDIA_RESOLUTION_ULTRA_HIGH`/`HIGH`, and thinking floors
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import random
import threading
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from lanterns.core.config import gemini_clean_schema, load_schema
from lanterns.core.env import load_env
from lanterns.core.hashes import sha256_json
from lanterns.core.pricing import estimate_cost_usd
from lanterns.providers.base import (
    BatchItemRequest,
    DecisionAnswer,
    DecisionBatchResult,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResult,
    ProviderSuccess,
    validate_wire_if_requested,
)

_DEPRECATED_GEMINI_PREFIXES: tuple[str, ...] = ("gemini-1.0", "gemini-1.5", "gemini-2.0", "gemini-2.5")

_THINKING_LEVEL_FLOORS: dict[str, str] = {
    "gemini-3.7": "low",
    "gemini-3.8": "low",
    "gemini-4": "low",
}

_VALID_SERVICE_TIERS: frozenset[str] = frozenset({"standard", "flex", "priority"})

_ARCHIVAL_SAFETY_CATEGORIES: tuple[str, ...] = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)

_MEDIA_RES_ALIASES: dict[str, str] = {
    "ultra_high": "MEDIA_RESOLUTION_ULTRA_HIGH",
    "high": "MEDIA_RESOLUTION_HIGH",
    "medium": "MEDIA_RESOLUTION_MEDIUM",
    "low": "MEDIA_RESOLUTION_LOW",
    "unspecified": "MEDIA_RESOLUTION_UNSPECIFIED",
}

_INTERACTIONS_RES_MAP: dict[str, str] = {
    "MEDIA_RESOLUTION_ULTRA_HIGH": "ultra_high",
    "MEDIA_RESOLUTION_HIGH": "high",
    "MEDIA_RESOLUTION_MEDIUM": "medium",
    "MEDIA_RESOLUTION_LOW": "low",
}


def check_google_genai_version() -> tuple[bool, str]:
    """Verify `google-genai >= 3.2.0` is installed at runtime."""
    try:
        from google import genai
    except ImportError as exc:
        return False, f"google-genai not installed: {exc}"
    ver_str = str(getattr(genai, "__version__", "0.0.0"))
    parts = [int(p) for p in ver_str.split(".")[:3] if p.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    ok = tuple(parts[:3]) >= (3, 2, 0)
    return ok, ver_str


def validate_gemini_model_name(model: str) -> str:
    """Reject deprecated Gemini 1.x / 2.x model IDs."""
    clean = (model or "").removeprefix("models/").strip()
    if not clean:
        raise ValueError("Gemini model name must not be empty")
    low = clean.lower()
    if low.startswith(_DEPRECATED_GEMINI_PREFIXES):
        raise ValueError(
            f"Deprecated Gemini model {model!r} is forbidden; use gemini-3.8-flash, gemini-3.6-flash, "
            "gemini-3.5-flash-lite, gemini-4-hydrogen, gemini-4-argon, gemini-4-carbon, or gemini-decision-1."
        )
    return clean


def normalize_media_resolution(val: str | None) -> str | None:
    if not val:
        return None
    clean = str(val).strip()
    if not clean:
        return None
    lower = clean.lower()
    if lower in _MEDIA_RES_ALIASES:
        return _MEDIA_RES_ALIASES[lower]
    upper = clean.upper()
    return upper if upper.startswith("MEDIA_RESOLUTION_") else clean


def gemini_uses_default_sampling(model: str | None) -> bool:
    clean = (model or "").removeprefix("models/")
    return clean.startswith(("gemini-3", "gemini-4"))


def gemini_supports_explicit_cache(model: str | None) -> bool:
    clean = (model or "").removeprefix("models/").strip().lower()
    return bool(clean) and not clean.startswith("gemini-4")


def gemini_resolve_thinking_level(model: str | None, level: str | None) -> str | None:
    if not level or not model:
        return level
    clean = model.removeprefix("models/").strip().lower()
    norm_level = level.strip().lower()
    for prefix, floor in _THINKING_LEVEL_FLOORS.items():
        if clean.startswith(prefix) and norm_level == "minimal":
            return floor
    return norm_level


def normalize_service_tier(val: str | None) -> str | None:
    if not val:
        return None
    clean = str(val).strip().lower()
    if not clean:
        return None
    if clean not in _VALID_SERVICE_TIERS:
        raise ValueError(f"Unsupported Gemini service_tier {val!r}; expected {sorted(_VALID_SERVICE_TIERS)}")
    return clean


def build_ordered_parts_spec(request: ProviderRequest) -> list[dict[str, Any]]:
    """Build cache-friendly ordered parts: static prompt text FIRST, then images, then dynamic suffix."""
    specs: list[dict[str, Any]] = []
    if request.prompt:
        specs.append({"kind": "text", "text": request.prompt})
    media_res = request.settings.get("media_resolution")
    for image_path in request.image_paths:
        mime_type = mimetypes.guess_type(image_path)[0] or "image/png"
        specs.append(
            {
                "kind": "image",
                "path": str(image_path),
                "mime_type": mime_type,
                "media_resolution": media_res,
            }
        )
    if request.dynamic_suffix:
        specs.append({"kind": "text", "text": request.dynamic_suffix})
    return specs


class GeminiProvider:
    """Native `google-genai >= 3.2.0` vision & decision provider."""

    name = "gemini"
    pricing_family = "gemini"
    harness = "sdk"

    def __init__(
        self,
        model: str = "gemini-3.8-flash",
        *,
        api_key: str | None = None,
        api_version: str = "v1alpha",
        max_retries: int = 3,
        base_backoff_s: float = 1.5,
    ) -> None:
        load_env()
        self.model = validate_gemini_model_name(model)
        self.api_key = os.getenv("GEMINI_API_KEY") if api_key is None else api_key
        self.api_version = api_version
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self.last_cache_error: str | None = None
        self._thread_clients = threading.local()

    def generate(self, request: ProviderRequest) -> ProviderResult:
        if request.settings.get("use_interactions_api"):
            return self.generate_interaction(request)
        if not self.api_key:
            return ProviderError(ProviderErrorKind.AUTH, "GEMINI_API_KEY is not set")
        try:
            from google import genai
            from google.genai import errors as genai_errors
            from google.genai import types
        except ImportError as exc:
            return ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, f"google-genai unavailable: {exc}")

        client = self._get_client(genai, self.api_version)
        contents = _materialize_contents(types, request)
        requested_tier = normalize_service_tier(request.settings.get("service_tier"))
        config_dict = self._config_args(types, request)
        config = types.GenerateContentConfig(**config_dict)
        started = time.time()
        last_err: ProviderError | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = client.models.generate_content(model=self.model, contents=contents, config=config)
                return self._parse_sdk_response(request, response, started, is_batch=False, service_tier=requested_tier)
            except genai_errors.APIError as exc:
                last_err = _error_from_api_exception(exc)
                if _is_retryable_kind(last_err.kind) and attempt < self.max_retries:
                    time.sleep(self.base_backoff_s * (2**attempt) + random.uniform(0.05, 0.5))
                    continue
                break
            except Exception as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                last_err = ProviderError(_exception_kind(exc), _safe_message(exc))
                if last_err.kind == ProviderErrorKind.TIMEOUT and attempt < self.max_retries:
                    time.sleep(self.base_backoff_s * (2**attempt))
                    continue
                break

        return last_err or ProviderError(ProviderErrorKind.UNKNOWN, "Exhausted Gemini retries")

    def generate_interaction(self, request: ProviderRequest) -> ProviderResult:
        """Execute vision/text generation via Gemini `Interactions API` (`client.interactions.create` on `v1`)."""
        if not self.api_key:
            return ProviderError(ProviderErrorKind.AUTH, "GEMINI_API_KEY is not set")
        try:
            from google import genai
            from google.genai import errors as genai_errors
        except ImportError as exc:
            return ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, f"google-genai unavailable: {exc}")

        client = self._get_client(genai, "v1")
        started = time.time()
        kwargs = self._build_interaction_kwargs(request)

        try:
            interaction = client.interactions.create(api_version="v1", **kwargs)
        except genai_errors.APIError as exc:
            return _error_from_api_exception(exc)
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return ProviderError(_exception_kind(exc), _safe_message(exc))

        return self._parse_interaction_response(request, interaction, started)

    def _build_interaction_kwargs(self, request: ProviderRequest) -> dict[str, Any]:
        input_items: list[dict[str, Any]] = []
        for spec in build_ordered_parts_spec(request):
            if spec["kind"] == "text":
                input_items.append({"type": "text", "text": spec["text"]})
            elif spec["kind"] == "image":
                raw_bytes = Path(spec["path"]).read_bytes()
                norm_res = normalize_media_resolution(spec.get("media_resolution"))
                img_item: dict[str, Any] = {
                    "type": "image",
                    "data": base64.b64encode(raw_bytes).decode("ascii"),
                    "mime_type": spec["mime_type"],
                }
                if norm_res and norm_res in _INTERACTIONS_RES_MAP:
                    img_item["resolution"] = _INTERACTIONS_RES_MAP[norm_res]
                input_items.append(img_item)

        gen_cfg: dict[str, Any] = {}
        thinking_level = gemini_resolve_thinking_level(self.model, request.settings.get("thinking_level"))
        if thinking_level:
            gen_cfg["thinking_level"] = thinking_level
        if request.settings.get("max_output_tokens"):
            gen_cfg["max_output_tokens"] = int(request.settings["max_output_tokens"])

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "safety_settings": [
                {"category": cat.removeprefix("HARM_CATEGORY_").lower(), "threshold": "block_none"}
                for cat in _ARCHIVAL_SAFETY_CATEGORIES
            ],
        }
        if gen_cfg:
            kwargs["generation_config"] = gen_cfg
        if request.settings.get("system_instruction"):
            kwargs["system_instruction"] = str(request.settings["system_instruction"])
        schema_obj = request.schema_dict or (load_schema(request.schema_name) if request.schema_name else None)
        if schema_obj is not None:
            kwargs["response_format"] = {
                "type": "text",
                "mime_type": "application/json",
                "schema": gemini_clean_schema(schema_obj),
            }
        return kwargs

    def _parse_interaction_response(
        self, request: ProviderRequest, interaction: Any, started: float
    ) -> ProviderResult:
        raw = interaction.model_dump(mode="json") if hasattr(interaction, "model_dump") else {}
        status = str(getattr(interaction, "status", raw.get("status", "")) or "")
        prov = _interaction_provenance(self.pricing_family, self.model, raw, status, started)
        if "recitation" in status.lower():
            return ProviderError(ProviderErrorKind.RECITATION, f"Interaction halted: {status}", raw_response=raw, provenance=prov)
        if status.lower() == "incomplete":
            return ProviderError(ProviderErrorKind.MAX_TOKENS, "Interaction incomplete (max_output_tokens)", raw_response=raw, provenance=prov)

        text = str(getattr(interaction, "output_text", None) or raw.get("output_text") or "").strip()
        try:
            data = json.loads(text)
        except ValueError:
            return ProviderError(
                ProviderErrorKind.SCHEMA_INVALID,
                "Interaction response was not valid JSON",
                raw_response=raw,
                provenance=prov,
            )
        err = validate_wire_if_requested(request, data, raw_response=raw, provenance=prov)
        return err if err is not None else ProviderSuccess(data=data, raw_response=raw, provenance=prov)

    def evaluate_decisions(
        self,
        input_text: str,
        questions: dict[str, dict[str, Any]],
        *,
        image_paths: list[str] | None = None,
        model: str = "gemini-decision-1",
    ) -> DecisionBatchResult | ProviderError:
        """Evaluate calibrated `noul` / `choice` questions via `client.decisions.create` (`v1beta`, 0 output tokens)."""
        if not self.api_key:
            return ProviderError(ProviderErrorKind.AUTH, "GEMINI_API_KEY is not set")
        clean_model = validate_gemini_model_name(model)
        try:
            from google import genai
            from google.genai import errors as genai_errors
        except ImportError as exc:
            return ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, f"google-genai unavailable: {exc}")

        client = self._get_client(genai, "v1beta")
        started = time.time()
        input_payload = _build_decision_input(input_text, image_paths)

        try:
            resp = client.decisions.create(
                api_version="v1beta",
                model=f"models/{clean_model}",
                input=input_payload,
                questions=questions,
            )
        except genai_errors.APIError as exc:
            return _error_from_api_exception(exc)
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return ProviderError(_exception_kind(exc), _safe_message(exc))

        return _parse_decision_batch_response(resp, clean_model, started)

    def create_context_cache(
        self,
        *,
        system_instruction: str,
        static_prompt_text: str,
        ttl_seconds: int = 3600,
        display_name: str = "lanterns-epistolary-cache",
    ) -> str | None:
        """Create an Explicit Context Cache for non-Gemini-4 models; fall back cleanly to implicit prefix cache."""
        if not self.api_key or not gemini_supports_explicit_cache(self.model):
            self.last_cache_error = None
            return None
        from google import genai
        from google.genai import types

        client = self._get_client(genai, self.api_version)
        model_ref = f"models/{self.model}"
        self.last_cache_error = None
        try:
            cache = client.caches.create(
                model=model_ref,
                config=types.CreateCachedContentConfig(
                    display_name=display_name,
                    system_instruction=system_instruction,
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text=static_prompt_text)])],
                    ttl=f"{int(ttl_seconds)}s",
                ),
            )
            return str(cache.name)
        except Exception as exc:
            self.last_cache_error = _safe_message(exc)
            return None

    def delete_context_cache(self, cache_name: str | None) -> None:
        if not cache_name or not self.api_key:
            return
        try:
            from google import genai

            self._get_client(genai, self.api_version).caches.delete(name=cache_name)
        except Exception as exc:
            self.last_cache_error = _safe_message(exc)

    def build_batch_jsonl_line(self, item: BatchItemRequest) -> dict[str, Any]:
        """Serialize a `BatchItemRequest` into Gemini Batch JSONL format (50% token discount)."""
        parts_json: list[dict[str, Any]] = []
        for spec in build_ordered_parts_spec(item.request):
            if spec["kind"] == "text":
                parts_json.append({"text": spec["text"]})
            elif spec["kind"] == "image":
                eff_mime, img_bytes = _encode_batch_image(Path(spec["path"]), spec["mime_type"])
                part_entry: dict[str, Any] = {
                    "inline_data": {
                        "mime_type": eff_mime,
                        "data": base64.b64encode(img_bytes).decode("ascii"),
                    }
                }
                norm_res = normalize_media_resolution(spec.get("media_resolution"))
                if norm_res:
                    part_entry["media_resolution"] = {"level": norm_res}
                parts_json.append(part_entry)

        gen_cfg: dict[str, Any] = {}
        schema_obj = item.request.schema_dict or (
            load_schema(item.request.schema_name) if item.request.schema_name else None
        )
        if schema_obj is not None:
            gen_cfg["response_mime_type"] = "application/json"
            gen_cfg["response_json_schema"] = gemini_clean_schema(schema_obj)
        thinking_level = gemini_resolve_thinking_level(self.model, item.request.settings.get("thinking_level"))
        if thinking_level:
            gen_cfg["thinking_config"] = {"thinking_level": thinking_level}
        if item.request.settings.get("max_output_tokens"):
            gen_cfg["max_output_tokens"] = int(item.request.settings["max_output_tokens"])

        req_body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts_json}],
            "generation_config": gen_cfg,
            "safety_settings": [
                {"category": cat, "threshold": "BLOCK_NONE"} for cat in _ARCHIVAL_SAFETY_CATEGORIES
            ],
        }
        if item.request.settings.get("cached_content") and gemini_supports_explicit_cache(self.model):
            req_body["cached_content"] = item.request.settings["cached_content"]
        elif item.request.settings.get("system_instruction"):
            req_body["system_instruction"] = {"parts": [{"text": str(item.request.settings["system_instruction"])}]}
        return {"key": item.key, "request": req_body}

    def submit_batch_job(
        self,
        items: list[BatchItemRequest],
        jsonl_staging_path: str | Path,
        *,
        display_name: str = "lanterns-batch",
    ) -> str:
        """Write JSONL batch file, upload via Files API, and launch a 50%-off Gemini Batch job."""
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        from google import genai
        from google.genai import types

        staging = Path(jsonl_staging_path)
        staging.parent.mkdir(parents=True, exist_ok=True)
        with staging.open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(self.build_batch_jsonl_line(item), ensure_ascii=False) + "\n")

        client = self._get_client(genai, self.api_version)
        uploaded = client.files.upload(
            file=str(staging),
            config=types.UploadFileConfig(display_name=display_name, mime_type="jsonl"),
        )
        job = client.batches.create(
            model=self.model,
            src=uploaded.name,
            config={"display_name": display_name},
        )
        return str(job.name)

    def retrieve_batch_job(self, job_name: str) -> dict[str, Any]:
        """Retrieve live status of a Gemini Batch job (`client.batches.get`)."""
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        from google import genai

        job = self._get_client(genai, self.api_version).batches.get(name=job_name)
        state_obj = getattr(job, "state", "")
        state_str = str(getattr(state_obj, "name", state_obj) or "")
        dest = getattr(job, "dest", None)
        return {
            "id": str(getattr(job, "name", job_name)),
            "state": state_str,
            "dest_file": getattr(dest, "file_name", None),
        }

    def collect_batch_job_results(
        self,
        job_name: str,
        requests_by_key: dict[str, ProviderRequest],
    ) -> dict[str, ProviderResult]:
        """Download and validate JSONL output of a succeeded Gemini Batch job at 50% batch rates."""
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        from google import genai

        client = self._get_client(genai, self.api_version)
        job = client.batches.get(name=job_name)
        dest = getattr(job, "dest", None)
        dest_file = getattr(dest, "file_name", None)
        if not dest_file:
            raise RuntimeError(f"Gemini batch job {job_name!r} has no output file_name")
        raw_bytes = client.files.download(file=dest_file)
        text = raw_bytes.decode("utf-8") if isinstance(raw_bytes, (bytes, bytearray)) else str(raw_bytes)
        results: dict[str, ProviderResult] = {}
        started = time.time()
        for line in text.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = str(row.get("key") or "")
            req = requests_by_key.get(key)
            if req is None:
                continue
            if row.get("error"):
                results[key] = ProviderError(ProviderErrorKind.UNKNOWN, f"Gemini batch error: {row['error']}")
            else:
                resp_dict = row.get("response") or {}
                results[key] = self._parse_sdk_response(req, resp_dict, started, is_batch=True)
        return results

    def _get_client(self, genai: Any, api_version: str) -> Any:
        cache: dict[str, Any] = getattr(self._thread_clients, "clients", {})
        if api_version not in cache:
            cache[api_version] = genai.Client(
                api_key=self.api_key,
                http_options={"api_version": api_version, "timeout": 90_000},
            )
            self._thread_clients.clients = cache
        return cache[api_version]

    def _config_args(self, types: Any, request: ProviderRequest) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        if (
            "temperature" in request.settings
            and request.settings["temperature"] is not None
            and not gemini_uses_default_sampling(self.model)
        ):
            cfg["temperature"] = float(request.settings["temperature"])
        schema_obj = request.schema_dict or (load_schema(request.schema_name) if request.schema_name else None)
        if schema_obj is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_json_schema"] = gemini_clean_schema(schema_obj)
        if request.settings.get("cached_content") and gemini_supports_explicit_cache(self.model):
            cfg["cached_content"] = str(request.settings["cached_content"])
        elif request.settings.get("system_instruction"):
            cfg["system_instruction"] = str(request.settings["system_instruction"])
        thinking_level = gemini_resolve_thinking_level(self.model, request.settings.get("thinking_level"))
        if thinking_level:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=r".*is not a valid ThinkingLevel.*", category=UserWarning)
                cfg["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
        service_tier = normalize_service_tier(request.settings.get("service_tier"))
        if service_tier:
            cfg["service_tier"] = service_tier
        if request.settings.get("max_output_tokens"):
            cfg["max_output_tokens"] = int(request.settings["max_output_tokens"])
        if not request.settings.get("disable_archival_safety"):
            cfg["safety_settings"] = [
                types.SafetySetting(category=cat, threshold="BLOCK_NONE") for cat in _ARCHIVAL_SAFETY_CATEGORIES
            ]
        if request.settings.get("timeout_ms"):
            cfg["http_options"] = types.HttpOptions(
                api_version=self.api_version,
                timeout=int(request.settings["timeout_ms"]),
            )
        return cfg

    def _parse_sdk_response(
        self,
        request: ProviderRequest,
        response: Any,
        started: float,
        *,
        is_batch: bool,
        service_tier: str | None = None,
    ) -> ProviderResult:
        if isinstance(response, dict):
            raw = response
        else:
            raw = (
                response.model_dump(mode="json")
                if hasattr(response, "model_dump")
                else {"text": getattr(response, "text", "")}
            )
        finish_reason = _extract_finish_reason(raw)
        prov = self._provenance(request, raw, started, finish_reason=finish_reason, is_batch=is_batch, service_tier=service_tier)
        finish_err = _classify_finish_error(finish_reason)
        if finish_err is not None:
            return ProviderError(
                finish_err,
                f"Gemini halted with finish_reason={finish_reason}",
                raw_response=raw,
                provenance=prov,
            )
        text = _extract_text(response, raw)
        try:
            data = json.loads(text)
        except ValueError:
            return ProviderError(
                ProviderErrorKind.SCHEMA_INVALID,
                "Gemini response was not valid JSON",
                raw_response=raw,
                provenance=prov,
            )
        err = validate_wire_if_requested(request, data, raw_response=raw, provenance=prov)
        if err is not None:
            return err
        return ProviderSuccess(data=data, raw_response=raw, provenance=prov)

    def _provenance(
        self,
        request: ProviderRequest,
        raw: dict[str, Any],
        started: float,
        *,
        finish_reason: str | None,
        is_batch: bool,
        service_tier: str | None = None,
    ) -> dict[str, Any]:
        usage = raw.get("usage_metadata") or raw.get("usageMetadata") or {}
        in_tok = int(usage.get("prompt_token_count") or usage.get("promptTokenCount") or 0)
        cached_tok = int(usage.get("cached_content_token_count") or usage.get("cachedContentTokenCount") or 0)
        out_tok = int(usage.get("candidates_token_count") or usage.get("candidatesTokenCount") or 0)
        thought_tok = int(usage.get("thoughts_token_count") or usage.get("thoughtsTokenCount") or 0)
        eff_tier = service_tier or normalize_service_tier(request.settings.get("service_tier"))
        cost = estimate_cost_usd(
            self.pricing_family,
            self.model,
            in_tok,
            out_tok,
            thought_tok,
            cached_input_tokens=cached_tok,
            is_batch=is_batch,
            service_tier=eff_tier,
        )
        return {
            "provider": self.name,
            "model": self.model,
            "harness": self.harness,
            "api_version": self.api_version,
            "is_batch": is_batch,
            "service_tier": "batch" if is_batch else (eff_tier or "standard"),
            "cached_content": request.settings.get("cached_content"),
            "thinking_level": gemini_resolve_thinking_level(self.model, request.settings.get("thinking_level")),
            "media_resolution": normalize_media_resolution(request.settings.get("media_resolution")),
            "finish_reason": finish_reason,
            "input_tokens": in_tok,
            "cached_input_tokens": cached_tok,
            "output_tokens": out_tok,
            "thoughts_tokens": thought_tok,
            "cost_estimate_usd": cost,
            "latency_ms": int((time.time() - started) * 1000),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "response_hash": sha256_json(raw),
        }


def _encode_batch_image(img_path: Path, declared_mime: str, max_png_bytes: int = 1_800_000) -> tuple[str, bytes]:
    raw_bytes = img_path.read_bytes()
    if declared_mime != "image/png" or len(raw_bytes) <= max_png_bytes:
        return declared_mime, raw_bytes
    buf = io.BytesIO()
    with Image.open(io.BytesIO(raw_bytes)) as img:
        mode = img.mode if img.mode in ("L", "RGB", "RGBA") else "RGB"
        img.convert(mode).save(buf, format="WEBP", lossless=True, method=4)
    webp_bytes = buf.getvalue()
    return ("image/webp", webp_bytes) if len(webp_bytes) < len(raw_bytes) else (declared_mime, raw_bytes)


def _materialize_contents(types: Any, request: ProviderRequest) -> Any:
    specs = build_ordered_parts_spec(request)
    if len(specs) == 1 and specs[0]["kind"] == "text":
        return specs[0]["text"]
    parts = []
    for spec in specs:
        if spec["kind"] == "text":
            parts.append(types.Part.from_text(text=spec["text"]))
        elif spec["kind"] == "image":
            data = Path(spec["path"]).read_bytes()
            kwargs: dict[str, Any] = {"data": data, "mime_type": spec["mime_type"]}
            norm_res = normalize_media_resolution(spec.get("media_resolution"))
            if norm_res:
                kwargs["media_resolution"] = norm_res
            parts.append(types.Part.from_bytes(**kwargs))
    return types.Content(role="user", parts=parts)


def _extract_finish_reason(raw: dict[str, Any]) -> str | None:
    candidates = raw.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return None
    reason = candidates[0].get("finish_reason") or candidates[0].get("finishReason")
    return str(reason) if reason else None


def _classify_finish_error(reason: str | None) -> ProviderErrorKind | None:
    if not reason:
        return None
    upper = reason.upper()
    if upper in {"STOP", "FINISH_REASON_UNSPECIFIED"}:
        return None
    if "RECITATION" in upper:
        return ProviderErrorKind.RECITATION
    if "MAX_TOKENS" in upper:
        return ProviderErrorKind.MAX_TOKENS
    if any(tok in upper for tok in ("SAFETY", "BLOCKLIST", "PROHIBITED", "SPII")):
        return ProviderErrorKind.SAFETY
    return ProviderErrorKind.UNKNOWN


def _extract_text(sdk_response: Any, raw: dict[str, Any]) -> str:
    candidates = raw.get("candidates") or []
    if candidates and isinstance(candidates[0], dict):
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or [] if isinstance(content, dict) else []
        text_parts = [
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and not part.get("thought") and part.get("text")
        ]
        if text_parts:
            joined = "".join(text_parts)
            return joined
    if sdk_response is not None:
        txt = getattr(sdk_response, "text", "")
        if isinstance(txt, str) and txt:
            return txt
    return ""


def _is_retryable_kind(kind: ProviderErrorKind) -> bool:
    return kind in {ProviderErrorKind.RATE_LIMITED, ProviderErrorKind.PROVIDER_UNAVAILABLE, ProviderErrorKind.TIMEOUT}


def _error_from_api_exception(exc: Any) -> ProviderError:
    code = int(getattr(exc, "code", 0) or 0)
    msg = _safe_message(exc)
    if code in {401, 403}:
        kind = ProviderErrorKind.AUTH
    elif code == 429:
        kind = ProviderErrorKind.RATE_LIMITED
    elif code in {408, 504}:
        kind = ProviderErrorKind.TIMEOUT
    elif code >= 500:
        kind = ProviderErrorKind.PROVIDER_UNAVAILABLE
    else:
        kind = ProviderErrorKind.UNKNOWN
    return ProviderError(kind, msg)


def _exception_kind(exc: Exception) -> ProviderErrorKind:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return ProviderErrorKind.TIMEOUT if ("timeout" in text or "timed out" in text) else ProviderErrorKind.UNKNOWN


def _interaction_provenance(
    pricing_family: str, model: str, raw: dict[str, Any], status: str, started: float
) -> dict[str, Any]:
    usage = raw.get("usage") or {}
    in_tok = int(usage.get("total_input_tokens", usage.get("input_tokens")) or 0)
    cached_tok = int(usage.get("total_cached_tokens", usage.get("cached_input_tokens")) or 0)
    out_tok = int(usage.get("total_output_tokens", usage.get("output_tokens")) or 0)
    thought_tok = int(usage.get("total_thought_tokens", usage.get("thoughts_tokens")) or 0)
    cost = estimate_cost_usd(pricing_family, model, in_tok, out_tok, thought_tok, cached_input_tokens=cached_tok)
    return {
        "provider": "gemini",
        "model": model,
        "harness": "sdk_interactions_v1",
        "api_version": "v1",
        "finish_reason": status,
        "input_tokens": in_tok,
        "cached_input_tokens": cached_tok,
        "output_tokens": out_tok,
        "thoughts_tokens": thought_tok,
        "cost_estimate_usd": cost,
        "latency_ms": int((time.time() - started) * 1000),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "response_hash": sha256_json(raw),
    }


def _build_decision_input(input_text: str, image_paths: list[str] | None) -> Any:
    if not image_paths:
        return input_text
    parts_list: list[dict[str, Any]] = []
    if input_text:
        parts_list.append({"type": "text", "text": input_text})
    for img_p in image_paths:
        mime = mimetypes.guess_type(img_p)[0] or "image/png"
        b64 = base64.b64encode(Path(img_p).read_bytes()).decode("ascii")
        parts_list.append({"type": "image", "data": b64, "mime_type": mime, "resolution": "high"})
    return parts_list


def _parse_single_decision_answer(qid: str, ans: dict[str, Any]) -> DecisionAnswer:
    atype = str(ans.get("type") or "noul")
    if atype == "noul":
        noul_val = float(ans.get("noul") or 0.0)
        return DecisionAnswer(
            question_id=qid,
            kind="noul",
            verdict="yes" if noul_val >= 0.5 else "no",
            score=noul_val,
            confidence=float(ans.get("confidence") or 0.0),
            probabilities={"yes": noul_val, "no": round(1.0 - noul_val, 6)},
        )
    if atype == "choice":
        choice_val = str(ans.get("choice") or "")
        probs = {str(k): float(v) for k, v in (ans.get("probabilities") or {}).items()}
        return DecisionAnswer(
            question_id=qid,
            kind="choice",
            verdict=choice_val,
            score=float(probs.get(choice_val, 0.0)),
            confidence=float(ans.get("confidence") or 0.0),
            probabilities=probs,
        )
    sc = float(ans.get("score") or 0.0)
    return DecisionAnswer(
        question_id=qid,
        kind=atype,
        verdict=str(sc),
        score=sc,
        confidence=float(ans.get("confidence") or 0.0),
    )


def _parse_decision_batch_response(resp: Any, clean_model: str, started: float) -> DecisionBatchResult:
    raw = resp.model_dump(mode="json") if hasattr(resp, "model_dump") else {}
    usage = raw.get("usage") or {}
    in_tok = int(usage.get("input_tokens") or usage.get("total_input_tokens") or 0)
    cached_tok = int(usage.get("cached_input_tokens") or 0)
    cost = estimate_cost_usd("gemini", clean_model, in_tok, 0, 0, cached_input_tokens=cached_tok)
    prov = {
        "provider": "gemini",
        "model": clean_model,
        "harness": "sdk_decisions_v1beta",
        "input_tokens": in_tok,
        "cached_input_tokens": cached_tok,
        "output_tokens": 0,
        "thoughts_tokens": 0,
        "cost_estimate_usd": cost,
        "latency_ms": int((time.time() - started) * 1000),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    parsed = {
        qid: _parse_single_decision_answer(qid, ans)
        for qid, ans in (raw.get("answers") or {}).items()
        if isinstance(ans, dict)
    }
    return DecisionBatchResult(answers=parsed, provenance=prov)


def _safe_message(exc: Exception) -> str:
    msg = str(exc)
    key = os.getenv("GEMINI_API_KEY")
    if key:
        msg = msg.replace(key, "[REDACTED]")
    return msg or exc.__class__.__name__
