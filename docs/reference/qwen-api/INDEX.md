# QwenCloud & Alibaba Model Studio Reference (`qwen_cloud`)

> Canonical upstream indexes:
> - `https://docs.qwencloud.com/llms.txt`
> - `https://help.aliyun.com/en/model-studio/llms.txt`

## 1. Endpoints & Regional Scopes

| Scope | OpenAI-Compatible Chat Endpoint | Notes |
| --- | --- | --- |
| **Global (Default in Paper Lanterns)** | `https://global.maas.qwencloudapi.com/compatible-mode/v1/chat/completions` | Lowest `qwen4-max` rates (`$1.413 / $4.24` per 1M); Frankfurt, Hong Kong, Tokyo, US (Virginia). |
| **Singapore (International)** | `https://ap-southeast-1.maas.qwencloudapi.com/compatible-mode/v1/chat/completions` | Supports Batch API (`50%` discount); `$1.50 / $4.50` per 1M on `qwen4-max`. |
| **DashScope International** | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions` | Legacy-compatible DashScope endpoint using `DASHSCOPE_API_KEY`. |

Override at runtime via `QWEN_CLOUD_BASE_URL` or `DASHSCOPE_BASE_URL`.

## 2. Qwen 4 vs. Qwen 3 Family Specifications & Official Pricing

| Model ID | Context / Max Out | Input / 1M | Output (incl. Thinking) / 1M | Cache Read / 1M | Key Parameters in `lanterns` |
| --- | --- | ---: | ---: | ---: | --- |
| **`qwen4-max`** | `1M` / `131K` (`262K` w/ thinking) | `$1.413` (Global) / `$1.50` (SG) | `$4.24` (Global) / `$4.50` (SG) | `$0.071` (Global) / `$0.075` (SG) | `reasoning_effort="low"` (`"high"`/`"xhigh"` for hotspots), image `detail="high"`, `cache_control={"type": "ephemeral"}` |
| **`qwen3.8-max`** | `1M` / `128K` | `$1.20` | `$6.00` | `$0.24` | `vl_high_resolution_images=True`, `enable_thinking=False` |
| **`qwen3.7-plus`** | `1M` / `64K` | `$0.40` | `$1.20` | `$0.08` | `vl_high_resolution_images=True`, `enable_thinking=False` |
| **`qwen3.8-flash`** | `1M` / `128K` | `$0.16` | `$0.47` | `$0.016` | `vl_high_resolution_images=True`, `enable_thinking=False` |
| **`qwen3.8-omni-flash`** | `1M` / `64K` | `$0.15` | `$0.47` | `$0.016` | Multimodal fast tier |
| **`qwen3-vl-plus`** | `131K` / `32K` | `$0.28` | `$1.68` | — | Dedicated Qwen3-VL vision model |

## 3. Critical Implementation Rules (`src/lanterns/providers/openai_compat.py`)

1. **`qwen4-max` Default Thinking Trap**: Upstream defaults `qwen4-max` to `reasoning_effort="high"` and `thinking_budget=131072`, billing reasoning tokens at the output rate (`$4.24/1M`). For verbatim page transcription, always explicitly send `reasoning_effort="low"` unless adjudicating complex hotspots.
2. **Multimodal Structured Output Behavior**: Per `docs.qwencloud.com/developer-guides/text-generation/structured-output.md`, multimodal requests (`image_url`) automatically fall back from `json_schema` to `json_object`. Always embed the cleaned `PageExtractWire` schema in the prompt text, set `response_format={"type": "json_object"}`, and validate locally with `PageExtractWire` / `jsonschema`.
3. **Explicit Cache on Qwen 4**: `qwen4-max` supports `cache_control={"type": "ephemeral"}` on Chat Completions content blocks (`$0.071/1M` cache read vs `$1.413/1M` uncached). Place static prompt + schema text at content index `0` before variable page image bytes.
