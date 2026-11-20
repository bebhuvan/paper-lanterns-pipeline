# Gemini API Reference Snapshot for Paper Lanterns v2

> **Snapshot Provenance:** Fetched directly from official Google AI Studio machine-readable documentation (`https://aistudio.google.com/llms.txt` and `https://aistudio.google.com/docs/*.md`) and verified against installed `google-genai==3.3.0` (`>= 3.2.0` required).
>
> **Purpose:** Eliminate stale training-cutoff hallucinations when engineers or AI coding agents touch `src/lanterns/providers/gemini.py`, `config/pricing.json`, or `config/profiles.json`.

---

## 1. Non-Negotiable SDK & Model Invariants

1. **Canonical Python SDK Only:** Always import `from google import genai` and `from google.genai import types` (`google-genai >= 3.2.0`). **Never** import or install deprecated `google-generativeai`.
2. **Current 2026 Model Lineup Only:**
   - **Gemini 4 Frontier Family (1M shared context, long decode up to 1M tokens):**
     - `gemini-4-hydrogen` ($1.50 / $7.50 per 1M tokens, $0.075 cached) — Primary archival reader for polyglot, Indic/Orientalist, and 18th-century correspondence, and default 2-page boundary arbiter.
     - `gemini-4-argon` ($2.00 / $10.00 intro through 2026-11-30; $4.00 / $20.00 from 2026-12-01) — Surgical 450-DPI hotspot crop adjudicator.
     - `gemini-4-carbon` ($2.00 / $10.00 intro; $4.00 / $20.00 from 2026-12-01) — Highest-capability frontier reasoning model.
   - **Gemini 3.x High-Throughput Family:**
     - `gemini-3.8-flash` ($0.75 / $3.75 intro through 2026-12-31; $0.075 cached) — Primary reader for standard English correspondence (`english_epistolary`, `recitation_shielded`).
     - `gemini-3.7-flash` / `gemini-3.6-flash` ($0.75 / $3.75) — Same-family Tier-2 fallback readers.
     - `gemini-3.5-flash-lite` ($0.30 / $2.50) & `gemini-3.1-flash-lite` ($0.25 / $1.50) — Lightweight auxiliary triage.
   - **Calibrated Judgment Model:**
     - `gemini-decision-1` ($0.10 / 1M input tokens, **$0.00** output tokens; 0 text tokens emitted) — Used via `client.decisions.create` (`v1beta`) for calibrated yes/no (`noul`), `choice`, and `score` checks on ambiguous epistolary boundaries and hotspot triage.
   - **Forbidden Legacy Models:** Never reference `gemini-1.5-*`, `gemini-2.0-*`, or `gemini-2.5-*` in code, configs, or tests.

---

## 2. How Paper Lanterns Maps Pipeline Stages to Gemini Surfaces

| Pipeline Stage | Preferred API Surface | Model Route | Key Configuration |
| :--- | :--- | :--- | :--- |
| **Interactive / Per-Page Extraction** | `client.interactions.create` (`store=False`) or `client.models.generate_content` (`v1alpha`/`v1beta`) | `gemini-3.8-flash` or `gemini-4-hydrogen` | Static prompt part at index `0`, per-part `MEDIA_RESOLUTION_ULTRA_HIGH` (2,240 image tokens) or `HIGH` (1,120 tokens), `thinking_level="low"`, archival `BLOCK_NONE` safety |
| **Bulk Volume Extraction (50% Discount)** | `client.batches.create` (`generateContent` Batch API) | `gemini-3.8-flash` or `gemini-4-hydrogen` | `batch_discount_multiplier = 0.5`; JSONL Files API upload above 16 MB |
| **Epistolary Boundary Verification** | `client.decisions.create` (`v1beta`) + `boundary_arbiter_v1` | `gemini-decision-1` -> `gemini-4-hydrogen` | Calibrated `noul` (`0.01..0.99`) + `confidence` at 0 output tokens; escalates low-confidence splits to `gemini-4-hydrogen` |
| **450-DPI Hotspot Crop Re-Read** | `client.interactions.create` / `generate_content` | `gemini-4-argon` | Isolated bbox crop PNG at `MEDIA_RESOLUTION_ULTRA_HIGH` |

---

## 3. Quick Lookup Map (`docs/reference/gemini-api/`)

- [llms.txt](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/gemini-api/llms.txt) — Upstream AI Studio complete documentation sitemap and URL index.
- [models-and-interactions.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/gemini-api/models-and-interactions.md) — `google-genai >= 3.2.0` client setup, Gemini 4 & 3.8 model specs, Interactions API (`client.interactions.create`, `store=False`, `steps`) vs `generateContent`, thinking floors, and sampling rules.
- [vision-and-media-resolution.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/gemini-api/vision-and-media-resolution.md) — Per-part `MEDIA_RESOLUTION_ULTRA_HIGH` (2,240 tokens) vs `HIGH` (1,120 tokens), why `ultra_high` MUST be set per content item rather than globally, prefix-ordered parts, normalized `0..1000` bounding boxes, and Files API (`2 GB` cap).
- [structured-output-and-decisions.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/gemini-api/structured-output-and-decisions.md) — JSON Schema Draft 2020-12 structured output on both Interactions API and `generateContent`, schema sanitization, and the `Decisions API` (`client.decisions.create` with `gemini-decision-1` `noul` / `choice` / `score`).
- [batch-caching-and-recitation.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/gemini-api/batch-caching-and-recitation.md) — `client.batches` (50% cost reduction), Gemini 4 automatic Implicit Caching (4,096-token floor) vs Gemini 3 Explicit Caching (`client.caches`), archival `BLOCK_NONE` safety settings, and Paper Lanterns' 4-Tier `RECITATION` defense.
