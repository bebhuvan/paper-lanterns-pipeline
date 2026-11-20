"""Provider request/response contracts, bbox clamping, and deterministic MockProvider."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from lanterns.core.config import SchemaValidationError, validate_data


class ProviderErrorKind(str, Enum):
    AUTH = "auth"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    RECITATION = "recitation"
    SAFETY = "safety"
    MAX_TOKENS = "max_tokens"
    SCHEMA_INVALID = "schema_invalid"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNKNOWN = "unknown"


@dataclass
class ProviderRequest:
    prompt: str
    schema_name: str | None = None
    schema_dict: dict[str, Any] | None = None
    image_paths: list[str] = field(default_factory=list)
    dynamic_suffix: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchItemRequest:
    key: str
    request: ProviderRequest


@dataclass
class DecisionAnswer:
    question_id: str
    kind: str
    verdict: str
    score: float
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass
class DecisionBatchResult:
    answers: dict[str, DecisionAnswer]
    provenance: dict[str, Any]


@dataclass
class ProviderSuccess:
    data: dict[str, Any]
    raw_response: dict[str, Any]
    provenance: dict[str, Any]


@dataclass
class ProviderError:
    kind: ProviderErrorKind
    message: str
    raw_response: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None


ProviderResult = ProviderSuccess | ProviderError


def clamp_wire_bboxes(data: Any) -> int:
    """Clamp out-of-range block bounding box ints into 0..1000 in place; return count clamped."""
    moved = 0
    if not isinstance(data, dict):
        return moved
    for block in data.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        box = block.get("bbox")
        if not isinstance(box, list):
            continue
        for idx, value in enumerate(box):
            if isinstance(value, int) and not isinstance(value, bool) and not (0 <= value <= 1000):
                box[idx] = min(1000, max(0, value))
                moved += 1
    return moved


def validate_wire_if_requested(
    request: ProviderRequest,
    data: dict[str, Any],
    *,
    raw_response: dict[str, Any],
    provenance: dict[str, Any],
) -> ProviderError | None:
    if request.schema_name == "page_extract_wire":
        clamped = clamp_wire_bboxes(data)
        if clamped:
            provenance["bbox_values_clamped"] = clamped
        data.setdefault("printed_folio", None)
        data.setdefault("warning_codes", [])
        data.setdefault("blocks", [])
    if request.schema_name:
        try:
            validate_data(request.schema_name, data)
        except SchemaValidationError as exc:
            return ProviderError(
                ProviderErrorKind.SCHEMA_INVALID,
                f"Schema validation failed: {exc}",
                raw_response=raw_response,
                provenance=provenance,
            )
    return None


class MockProvider:
    """Deterministic spend-free provider for offline testing and pipeline dry runs."""

    name = "mock"
    pricing_family = "mock"
    harness = "mock"

    def __init__(
        self,
        model: str = "mock-v1",
        responder: Callable[[ProviderRequest], ProviderResult | dict[str, Any]] | None = None,
    ) -> None:
        self.model = model
        self._responder = responder

    def generate(self, request: ProviderRequest) -> ProviderResult:
        prov = {
            "provider": self.name,
            "model": self.model,
            "harness": self.harness,
            "input_tokens": 100,
            "cached_input_tokens": 0,
            "output_tokens": 50,
            "thoughts_tokens": 0,
            "cost_estimate_usd": 0.0,
        }
        if self._responder is not None:
            out = self._responder(request)
            if isinstance(out, (ProviderSuccess, ProviderError)):
                return out
            raw_mock = {"mock": True, "parsed": out}
            err = validate_wire_if_requested(request, out, raw_response=raw_mock, provenance=prov)
            if err is not None:
                return err
            return ProviderSuccess(data=out, raw_response=raw_mock, provenance=prov)

        default_payload: dict[str, Any] = {
            "printed_folio": "1",
            "page_orientation": "upright",
            "warning_codes": [],
            "blocks": [
                {
                    "type": "body",
                    "bbox": [100, 120, 900, 300],
                    "text": "Mock diplomatic transcription of historical correspondence.",
                }
            ],
        }
        return ProviderSuccess(data=default_payload, raw_response={"mock": True}, provenance=prov)
