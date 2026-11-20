---
name: epistolary-boundary-tuning
description: >-
  Use this skill when diagnosing or fixing letter segmentation issues (multi-letter pages,
  false splits on enclosures/postscripts/bottom datelines, interleaved editorial bridges,
  or book-specific letter_map.json overrides) without violating AST anti-bloat laws.
---

# Epistolary Boundary Tuning & Override Workflow

Whenever a letter is over-split, under-split, or missing framing metadata, follow the Constitutional Decision Tree before touching code.

## Step 1 — Classify the Root Cause

1. **VLM Misclassified `type` or `epistolary_transition`?**
   - Example: A quoted inner letter was tagged `starts_new_letter` instead of `starts_enclosure`, or a biographical paragraph was tagged `body` instead of `editorial`.
   - **Fix Layer:** Improve structural instructions in `prompts/extract_epistolary_v1.md` (keeping within the global prompt word cap and using zero book-specific proper nouns), OR apply a book-scoped override in `books/<book_id>/letter_map.json` / `corrections/*.json`.
2. **Structural Transition Mishandled in `epistolary/segmentation.py`?**
   - Example: A bottom-placed `dateline` or trailing `postscript`/`address_block` after `signature` caused a premature split.
   - **Fix Layer:** Update `src/lanterns/epistolary/segmentation.py` using **only** `BlockIR.type`, `epistolary_transition`, `alignment`, `bbox`, and continuation flags—**never** natural-language content words (`Dear`, `Yours`, month names).
3. **Genuine Ambiguity Specific to One Volume's Editorial Convention?**
   - **Fix Layer:** Record deterministic block-ID rules in `books/<book_id>/letter_map.json` (validated against `schemas/letter_map.schema.json`):
     - `force_split_before_block_ids`: `["p0014-b0005"]`
     - `forbid_split_before_block_ids`: `["p0022-b0003"]`
     - `enclosure_block_ranges`: `[{"start_block_id": "p0031-b0004", "end_block_id": "p0031-b0007"}]`
     - `explicit_letters`: metadata overrides (`sender_display`, `recipient_display`, `dateline_position`, etc.).

## Step 2 — Verify Generalization Against Tuning & Held-Out Splits

1. Run the AST architecture guard and unit suite:
   ```bash
   ./verify
   ```
2. Run the stratified multi-book epistolary benchmark (`tuning` vs. `held_out` splits):
   ```bash
   uv run lanterns benchmark
   ```
   - Confirm that fixing the target edge case does **not** degrade `held_out` accuracy or increase `generalization_gap`.
