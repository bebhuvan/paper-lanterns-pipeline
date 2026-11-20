# Batch API, Context Caching, Archival Safety & 4-Tier `RECITATION` Defense

## 1. Gemini Batch API (`client.batches` — 50% Cost Reduction)

For canonical full-book extraction runs, `client.batches.create` processes `GenerateContentRequest` batches asynchronously at **50% off standard input, output, and thinking token rates** (`batch_discount_multiplier = 0.5` in `config/pricing.json`, same discount as `service_tier="flex"`).

- **Inline vs Files API Threshold:** Submit inline batch requests up to `16 MB` (`INLINE_BATCH_MAX_BYTES`); above `16 MB`, write requests as JSONL, upload via `client.files.upload`, and pass the file resource to `client.batches.create`.
- **Ledger Accounting:** Always pass `is_batch=True` (or `service_tier="flex"`) to `estimate_cost_usd(...)` so `cost_ledger.jsonl` records exact discounted spend.

---

## 2. Implicit Caching (Gemini 4 & 3) vs Explicit Caching (`client.caches`)

Official AI Studio caching behavior differs sharply between **Gemini 4** and **Gemini 3**:

| Capability | Gemini 4 (`carbon`, `argon`, `hydrogen`) | Gemini 3 (`3.8-flash`, `3.7-flash`, `3.6-flash`) |
| :--- | :--- | :--- |
| **Implicit Prefix Caching** | **Enabled automatically** (4,096 min input tokens; `0.05x` input rate = `$0.075/1M` on Hydrogen) | **Enabled automatically** (4,096 min input tokens; `0.10x` input rate = `$0.075/1M` on 3.8 Flash) |
| **Explicit Caching (`client.caches.create`)** | **NOT supported** (raises API error if called) | Supported on `generateContent` API (`v1alpha`/`v1beta`), NOT on Interactions API |
| **Usage Field** | `usage.total_cached_tokens` / `usage_metadata.cached_content_token_count` | `usage.total_cached_tokens` / `usage_metadata.cached_content_token_count` |

**Implementation Rule (`gemini_supports_explicit_cache`):**
- Never call `client.caches.create` when `model.startswith("gemini-4")` or when calling `client.interactions.create`.
- Always place the static prompt prefix at part index `0` so automatic Implicit Caching triggers seamlessly once prompt + image exceed 4,096 tokens.

---

## 3. Archival Safety Settings (`BLOCK_NONE`)

18th-, 19th-, and 20th-century historical correspondence regularly discusses wars, revolutions, famines, executions (e.g., Dostoevsky's mock-execution letter to Mikhail), religious controversies, colonial violence, and medical/anatomical pathology. Standard consumer moderation thresholds falsely block public-domain historical letters.

Always attach archival `BLOCK_NONE` across the four configurable harm categories:

```python
ARCHIVAL_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)

safety_settings = [
    types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE)
    for cat in ARCHIVAL_SAFETY_CATEGORIES
]
```

---

## 4. Four-Tier Anti-`RECITATION` & Multi-Family Fallback Router

Famous public-domain letters (Goethe, Napoleon, Lincoln, Gandhi, Nietzsche, Erasmus, Faraday) appear in pretraining corpora and occasionally trigger `FinishReason.RECITATION` or `FinishReason.IMAGE_RECITATION` on Gemini models—returning zero output tokens even though the work is out of copyright.

Paper Lanterns v2 (`src/lanterns/providers/router.py`) resolves this deterministically without ever losing a page or fabricating text:

1. **Tier 1 — Primary Profile Model (with Optional Proactive `[L01]` Scanline Prefixing):**
   - Runs the profile's primary route (`gemini/gemini-3.8-flash` or `gemini/gemini-4-hydrogen`).
   - When `use_scanline_prefix=True` (default on `recitation_shielded`, or enabled on retry), the prompt instructs the VLM to prefix each block's transcription with `[L01]` breaking verbatim n-gram memorization triggers; `extraction/normalize.py` deterministically strips `^\[L\d+\]\s*` before emitting `BlockIR` (verified by `control_char_or_mojibake` validator).
2. **Tier 2 — Same-Family Alternate Model or 2-Band Horizontal Image Split:**
   - On `RECITATION` or `MAX_TOKENS`, retries with `[L01]` scanline prefixing on the alternate Gemini model (`gemini-3.6-flash` / `gemini-4-hydrogen`) or splits the full-page scan into two overlapping horizontal bands (`top` + `bottom` with y-offset remapping back to `0..1000`) so each call sees half the page's memorized sequence.
3. **Tier 3 — Cross-Family VLM Escalation (`OpenRouter` / `Qwen Cloud` / `Anthropic`):**
   - Because recitation filters and tokenizer failure modes correlate within a single model family and decorrelate across families, persistent `RECITATION`, `SAFETY`, or quota errors automatically route to the profile's cross-family `fallback_chain`:
     - `openrouter/qwen/qwen3-vl-235b-a22b-instruct` or `openrouter/qwen/qwen3.8-flash`
     - `qwen_cloud/qwen3.8-flash` / `qwen_cloud/qwen3-vl-plus` (DashScope)
     - `anthropic/claude-sonnet-5-5`
   - Every `PageIR` and `BlockIR` records exact `provenance_provider`, `provenance_model`, `provenance_harness`, and `rescued=True`.
4. **Tier 4 — Explicit Fail-Closed (`PageIR.failed_closed = True`):**
   - If every configured tier fails, the router emits `PageIR(page_number=N, failed_closed=True, failure_reason=...)`.
   - Failed pages **never** silently vanish: `CollectionAST` records them in `BookSection.unavailable_pages`, `rubric.py` forces `provenance_tier = "blocked"`, and all renderers emit a visible `.page-unavailable` banner (`[Page N unavailable — inspect source scan]`).
