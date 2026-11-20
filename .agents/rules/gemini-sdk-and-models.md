---
trigger: always_on
description: Enforces google-genai >= 3.2.0 usage, current 2026 Gemini models, media resolution rules, and fail-closed cost governance.
---

# Gemini SDK, Models & Multi-Harness Routing Rule

Before modifying `src/lanterns/providers/`, `config/pricing.json`, or `config/profiles.json`, read `docs/reference/gemini-api/INDEX.md`.

1. **Canonical SDK Only:** Always use `google-genai >= 3.2.0` (`from google import genai`, `from google.genai import types`). Never install or import deprecated `google-generativeai`.
2. **Current 2026 Models Only:**
   - Allowed Gemini models: `gemini-4-carbon`, `gemini-4-argon`, `gemini-4-hydrogen`, `gemini-3.8-flash`, `gemini-3.7-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, and `gemini-decision-1` (`client.decisions.create` on `v1beta`, 0 output tokens).
   - Forbidden legacy models: `gemini-1.5-*`, `gemini-2.0-*`, `gemini-2.5-*`.
3. **SDK Parameter Traps to Avoid:**
   - `MEDIA_RESOLUTION_ULTRA_HIGH` (2,240 tokens/image) exists ONLY on per-item `types.PartMediaResolutionLevel` (or `"resolution": "ultra_high"` in Interactions API), NOT on global `GenerateContentConfig.media_resolution`.
   - Clamp `thinking_level="minimal"` up to `"low"` on `gemini-3.7`, `gemini-3.8`, and `gemini-4-*`. Never pass `temperature=0.0` to Gemini 3/4.
   - Gemini 4 supports automatic Implicit Caching (4,096-token floor; put static prompt at part index `0`), NOT `client.caches.create`.
   - Always pass archival `BLOCK_NONE` safety settings and preserve `api_version` inside `http_options` alongside `timeout`.
4. **Fail-Closed Spend & Cross-Family Fallback:** Every model route must exist in `config/pricing.json` before execution, enforce `--cost-ceiling-usd`, and route persistent `RECITATION`/quota blocks through `[L01]` scanline prefixing and cross-family backups (`openrouter/qwen/*`, `qwen_cloud/*`, `anthropic/*`) before `failed_closed=True`.
