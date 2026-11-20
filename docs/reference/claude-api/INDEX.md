# Anthropic Claude Platform Reference (`anthropic`)

> Canonical upstream index: `https://platform.claude.com/llms.txt`
> Official Python SDK floor enforced by `lanterns doctor`: `anthropic >= 2.8.0`

## 1. Current Claude Models & Official Pricing (USD per 1M Tokens)

| Tier | Model ID (Pinned Dateless Snapshot) | Base Input | 5m Cache Write | Cache Hit / Refresh | Output (incl. Thinking) | Batch Discount |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| **Opus 5.5** | `claude-opus-5-5` | `$4.00` | `$5.00` | `$0.20` (`0.05x`) | `$20.00` | `50%` |
| **Opus 5 / 4.8 / 4.7 / 4.6** | `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6` | `$5.00` | `$6.25` | `$0.50` (`0.10x`) | `$25.00` | `50%` |
| **Sonnet 5.5** | `claude-sonnet-5-5` | `$2.00` | `$2.50` | `$0.10` (`0.05x`) | `$10.00` | `50%` |
| **Sonnet 5** | `claude-sonnet-5` | `$2.00` | `$2.50` | `$0.20` (`0.10x`) | `$10.00` | `50%` |
| **Sonnet 4.6 / 4.5** | `claude-sonnet-4-6`, `claude-sonnet-4-5-20250929` | `$3.00` | `$3.75` | `$0.30` (`0.10x`) | `$15.00` | `50%` |
| **Haiku 5.5** | `claude-haiku-5-5` *(<=100K prompt)* | `$0.10` | `$0.125` | `$0.01` (`0.10x`) | `$0.50` | `50%` |
| **Haiku 4.5** | `claude-haiku-4-5-20251001` | `$1.00` | `$1.25` | `$0.10` (`0.10x`) | `$5.00` | `50%` |

## 2. Critical Claude 5.5 Breaking Changes & Rules (`src/lanterns/providers/anthropic.py`)

1. **Constrained JSON Decoding (`output_config.format`)**:
   - Send `output_config={"effort": effort, "format": {"type": "json_schema", "schema": cleaned_schema}}`.
   - Every object in `cleaned_schema` must set `"additionalProperties": false` and list all properties in `"required"` (wrapping optional fields in `anyOf: [T, {"type": "null"}]`).
   - **Never use forced tool use** (`tool_choice={"type": "tool"}`) on Claude Opus 5.5 or Sonnet 5.5 — forced tool use returns HTTP 400 on 5.5 models.
2. **Thinking & Effort Control (Never Send `"disabled"` to 5.5!)**:
   - **`claude-opus-5-5`**: Adaptive thinking cannot be disabled; sending `thinking: {"type": "disabled"}` or manual `budget_tokens` returns HTTP 400. Omit `thinking` and control depth via `output_config.effort` (`"low"` default for transcription; `max_tokens >= 20480` so thinking tokens never starve JSON output).
   - **`claude-sonnet-5-5`**: Sending `thinking: {"type": "disabled"}` returns HTTP 400. Use `thinking: {"type": "between_tools"}` at `low`/`medium`/`high` effort to eliminate upfront thinking latency/cost on non-tool vision extraction.
   - **`claude-haiku-5-5`**: Supports `1M` context, `128K` output, and `output_config.effort`.
3. **Vision Patch Sizing & Lossless WebP**:
   - Claude vision tiles images in `28x28` patches (`MAX_EDGE_PX = 2576`, `MAX_VISUAL_TOKENS = 4784`). `encode_claude_page_image` preserves exact aspect ratio within the patch ceiling and encodes lossless WebP (`quality=100, lossless=True`) under the 10 MB base64 cap.
4. **Prompt Caching & Message Batches**:
   - Static prompt instructions carry `cache_control={"type": "ephemeral"}` (`0.05x` cache hit rate on Opus 5.5 and Sonnet 5.5!).
   - Bulk runs can submit 50%-discounted batches via `AnthropicProvider.submit_message_batch` (`client.messages.batches.create`).
