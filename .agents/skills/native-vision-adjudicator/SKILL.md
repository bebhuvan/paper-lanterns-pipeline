---
name: native-vision-adjudicator
description: >-
  Use this skill when inspecting rendered page scan PNGs directly with multimodal native
  vision to verify VLM extractions pixel-for-pixel, resolve faded or ambiguous hotspots,
  or author canonical scan_checks/page_NNNN.json overrides for gemini_cli_harness.
---

# Native-Vision Scan Adjudicator Workflow

Paper Lanterns supports orthogonal harness verification where an agent inspects the rendered scan image directly (`read_file` on `.png`) and records ground-truth diplomatic observations in `books/<book_id>/scan_checks/page_NNNN.json`.

## Step 1 — Inspect the High-Resolution Scan Image Directly

1. Locate the rendered page image (`workspace/runs/<book_id>/<run_id>/renders/page_NNNN.png` or `workspace/experiments/<book_id>/<exp_id>/renders/page_NNNN.png`).
2. Call `read_file` directly on the `.png` path so you examine the actual ink pixels—never rely solely on OCR text or VLM transcripts.
3. Check the current extracted `PageIR` JSON alongside the scan for:
   - Archaic spellings, printer errata, medial long-s (`ſ`), and ligatures (`æ`, `œ`, `Æt.`).
   - Fine diacritics: Hunterian acutes (`Rájput`), IAST underdots/macrons (`ṛ`, `ṣ`, `ṃ`, `ā`), French circumflexes (`depôt` vs. `dépôt`), German umlauts, Greek breathings, or Perso-Arabic/Indic glyphs.
   - Tiny 6-point footnote symbols (`*`, `†`, `‡`, `§`, `a`, `1`) and cross-page split notes (`continues_from_prev`).
   - Outer-margin shoulder chronology (`margin_note`), cascading valedictions (`alignment="cascading"`), bottom datelines, and enclosed inner letters (`starts_enclosure`).

## Step 2 — Author or Update `books/<book_id>/scan_overrides/page_NNNN.json`

1. Write a schema-valid `PageExtractWire` JSON (matching `schemas/page_extract_wire.schema.json`) to `books/<book_id>/scan_overrides/page_NNNN.json` (zero-padded 4-digit 1-based page number; pure audit sign-offs without wire block overrides go in `books/<book_id>/audit_checks.jsonl`), or create an anchor-matched `correction-v2` proposal for `lanterns intake`.
2. Validate your JSON against `schemas/page_extract_wire.schema.json`:
   ```bash
   uv run python -c '
   import json, jsonschema
   from pathlib import Path
   schema = json.loads(Path("schemas/page_extract_wire.schema.json").read_text())
   data = json.loads(Path("books/<book_id>/scan_overrides/page_NNNN.json").read_text())
   jsonschema.validate(instance=data, schema=schema)
   print("Valid scan_override JSON")
   '
   ```

## Step 3 — Re-Extract / Compile & Verify Conservation

1. Run extraction with the `gemini_cli_harness` scan-check path (or `uv run lanterns intake <book_id> <proposal.json>` followed by `uv run lanterns compile <book_id> --run-id <run_id>`).
2. Run `uv run lanterns validate-edition <book_id>` and confirm `provenance_harness` / `proof_ledger.json` reflects the `scan_check` evidence with 0 blockers.
