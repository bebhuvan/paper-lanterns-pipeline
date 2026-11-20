# Gemini Models, SDK Setup & Interactions API (`google-genai >= 3.2.0`)

## 1. Canonical SDK Installation & Initialization

Paper Lanterns v2 requires `google-genai >= 3.2.0` (verified on `google-genai==3.3.0`).

```python
from google import genai
from google.genai import types

# Standard client (reads GEMINI_API_KEY or GOOGLE_API_KEY if api_key omitted)
client = genai.Client(api_key=api_key)

# Explicit API version preservation alongside timeout (in milliseconds)
client_v1beta = genai.Client(
    api_key=api_key,
    http_options={"api_version": "v1beta", "timeout": 120_000},
)
```

> [!IMPORTANT]
> Always pass `api_version` inside `http_options` when setting `timeout` so custom timeouts never silently reset `v1alpha` / `v1beta` endpoints.

---

## 2. Current 2026 Gemini Model Specifications & Pricing

Rates below reflect official AI Studio Developer API pricing stored in `config/pricing.json`:

| Model Code | Family | Context / Max Decode | Standard Input / Output (per 1M) | Cached Input (per 1M) | Batch Multiplier | Paper Lanterns Role |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `gemini-4-carbon` | Gemini 4 | 1M / 64K–1M | $2.00 / $10.00 *(intro to 2026-11-30; then $4/$20)* | $0.10 *(then $0.20)* | `0.5x` | Heavy scientific / paleographic escalation |
| `gemini-4-argon` | Gemini 4 | 1M / 64K–1M | $2.00 / $10.00 *(intro to 2026-11-30; then $4/$20)* | $0.10 *(then $0.20)* | `0.5x` | Surgical 450-DPI `hotspot_model` |
| `gemini-4-hydrogen` | Gemini 4 | 1M / 64K–1M | $1.50 / $7.50 | $0.075 | `0.5x` | Primary polyglot / Indic / 18c reader & `boundary_model` |
| `gemini-3.8-flash` | Gemini 3 | 1M / 64K | $0.75 / $3.75 *(intro to 2026-12-31; then $1.50/$7.50)* | $0.075 *(then $0.15)* | `0.5x` | Primary `english_epistolary` & `recitation_shielded` reader |
| `gemini-3.7-flash` | Gemini 3 | 1M / 64K | $0.75 / $3.75 | $0.075 | `0.5x` | Same-family Tier-2 fallback |
| `gemini-3.6-flash` | Gemini 3 | 1M / 64K | $0.75 / $3.75 | $0.075 | `0.5x` | Fast Tier-2 fallback for `english_epistolary` |
| `gemini-3.5-flash-lite` | Gemini 3 | 1M / 64K | $0.30 / $2.50 | $0.03 | `0.5x` | High-throughput lightweight tasks |
| `gemini-3.1-flash-lite` | Gemini 3 | 1M / 64K | $0.25 / $1.50 | $0.025 | `0.5x` | Budget auxiliary checks |
| `gemini-decision-1` | Decisions | Multimodal | $0.10 / **$0.00** | $0.01 | `0.5x` | Calibrated `noul` / `choice` / `score` (`v1beta`) |

---

## 3. Interactions API (`client.interactions`) vs `generateContent`

As of June 2026, the **Interactions API** (`client.interactions.create`) is Generally Available and Google's recommended primary interface, while `client.models.generate_content` remains fully supported (and underpins `client.batches` and Gemini 3 explicit `client.caches`).

### 3.1 Stateless Archival Extraction via `client.interactions.create`

By default, `client.interactions.create` stores server-side session state for `previous_interaction_id`. For reproducible, stateless page extraction in Paper Lanterns, pass `store=False`:

```python
interaction = client.interactions.create(
    model="gemini-4-hydrogen",
    store=False,
    input=[
        {"type": "text", "text": static_extraction_prompt},
        {
            "type": "image",
            "data": base64_png_str,
            "mime_type": "image/png",
            "resolution": "ultra_high",
        },
    ],
    response_format={
        "type": "text",
        "mime_type": "application/json",
        "schema": clean_wire_schema,
    },
)
raw_json = interaction.output_text
cached_tokens = getattr(interaction.usage, "total_cached_tokens", 0)
```

Key structural differences in `Interaction` responses:
- Reasoning surfaces as dedicated `thought` steps inside `interaction.steps` (`step.signature` + optional `step.summary`), never mixed into `model_output` parts.
- `interaction.output_text` returns the clean model output text directly.

### 3.2 `client.models.generate_content` (`v1alpha` / `v1beta`)

Used when per-part `types.PartMediaResolution(level=types.PartMediaResolutionLevel.MEDIA_RESOLUTION_ULTRA_HIGH)` inline bytes, explicit `cached_content`, or identical Batch API request payloads (`GenerateContentRequest`) are needed:

```python
response = client.models.generate_content(
    model="gemini-4-hydrogen",
    contents=[
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=static_extraction_prompt),
                types.Part(
                    inline_data=types.Blob(data=png_bytes, mime_type="image/png"),
                    media_resolution=types.PartMediaResolution(
                        level=types.PartMediaResolutionLevel.MEDIA_RESOLUTION_ULTRA_HIGH
                    ),
                ),
            ],
        )
    ],
    config=types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=clean_wire_schema,
        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        safety_settings=archival_safety_settings,
    ),
)
```

---

## 4. Critical Gemini 3 & Gemini 4 Parameter Guardrails

1. **Thinking-Level Floor (`LOW`):**
   - `types.ThinkingLevel` defines `MINIMAL`, `LOW`, `MEDIUM`, `HIGH`, `XHIGH`.
   - **Empirical rule:** `gemini-3.7-*`, `gemini-3.8-*`, and **all `gemini-4-*` models reject `thinking_level="minimal"` with HTTP 400**. Always clamp `"minimal"` up to `"low"` (`_THINKING_LEVEL_FLOORS`) on `3.7`, `3.8`, and `4`.
2. **Never Force `temperature=0.0` on Gemini 3 / Gemini 4:**
   - Thinking models (`gemini-3*`, `gemini-4*`) are calibrated for default sampling (`temperature` omitted / model default `1.0`). Passing `temperature=0.0` degrades reasoning and increases degenerate repetition loops on dense archival apparatus.
