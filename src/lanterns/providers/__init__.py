"""Multi-model and multi-harness VLM providers for Paper Lanterns v2."""

from lanterns.providers.anthropic import AnthropicProvider, claude_clean_schema, claude_fit_size
from lanterns.providers.base import (
    BatchItemRequest,
    DecisionAnswer,
    DecisionBatchResult,
    MockProvider,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResult,
    ProviderSuccess,
    clamp_wire_bboxes,
)
from lanterns.providers.gemini import (
    GeminiProvider,
    build_ordered_parts_spec,
    check_google_genai_version,
    gemini_resolve_thinking_level,
    gemini_supports_explicit_cache,
    gemini_uses_default_sampling,
    normalize_media_resolution,
    validate_gemini_model_name,
)
from lanterns.providers.gemini_cli_harness import (
    GeminiCLIHarnessProvider,
    load_native_scan_check,
    save_native_scan_check,
    scan_check_path,
)
from lanterns.providers.openai_compat import OpenAICompatProvider
from lanterns.providers.router import (
    SCANLINE_PREFIX_SUFFIX,
    FallbackRouter,
    RouterAttempt,
    RouterOutcome,
    build_provider,
    make_failed_closed_page_ir,
    merge_two_band_wires,
    split_image_two_horizontal_bands,
)

__all__ = [
    "AnthropicProvider",
    "BatchItemRequest",
    "DecisionAnswer",
    "DecisionBatchResult",
    "FallbackRouter",
    "GeminiCLIHarnessProvider",
    "GeminiProvider",
    "MockProvider",
    "OpenAICompatProvider",
    "ProviderError",
    "ProviderErrorKind",
    "ProviderRequest",
    "ProviderResult",
    "ProviderSuccess",
    "RouterAttempt",
    "RouterOutcome",
    "SCANLINE_PREFIX_SUFFIX",
    "build_ordered_parts_spec",
    "build_provider",
    "check_google_genai_version",
    "clamp_wire_bboxes",
    "claude_clean_schema",
    "claude_fit_size",
    "gemini_resolve_thinking_level",
    "gemini_supports_explicit_cache",
    "gemini_uses_default_sampling",
    "load_native_scan_check",
    "make_failed_closed_page_ir",
    "merge_two_band_wires",
    "normalize_media_resolution",
    "save_native_scan_check",
    "scan_check_path",
    "split_image_two_horizontal_bands",
    "validate_gemini_model_name",
]
