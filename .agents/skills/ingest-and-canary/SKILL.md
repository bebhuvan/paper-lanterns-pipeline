---
name: ingest-and-canary
description: >-
  Use this skill when onboarding a new scanned correspondence PDF or Internet Archive
  identifier into Paper Lanterns, running optical/JBIG2 triage, selecting an extraction
  profile, and executing a bounded canary experiment before canonical extraction.
---

# Ingest & Stratified Canary Workflow

Follow these steps whenever registering or testing a new correspondence volume so `workspace/runs/` and `library/` remain clean until canary quality is proven.

## Step 1 — Source Acquisition & Optical Triage

1. Place or symlink the source PDF in `sources/` (or search/fetch from Internet Archive):
   ```bash
   uv run lanterns search-archive "<title or correspondent>"
   uv run lanterns fetch-archive <archive_id>
   ```
2. Register the volume with the matching collection and correspondence profile (`english_epistolary`, `polyglot_epistolary`, `indic_and_oriental_epistolary`, `archaic_18c_epistolary`, or `recitation_shielded`):
   ```bash
   uv run lanterns init-book <book_id> \
     --title "<Exact Title>" --author "<Author or Editor>" --year <YYYY> \
     --collection <collection-slug> --profile <profile_name> \
     --source-pdf sources/<file>.pdf
   ```
3. Run optical and JBIG2 lossy-compression triage:
   ```bash
   uv run lanterns triage-pdf <book_id>
   ```
   - Inspect reported DPI, page dimensions, and any `jbig2_risk` warning. If a bitonal PDF exhibits JBIG2 glyph substitution risk and an Internet Archive `_jp2.zip` exists, prefer continuous-tone JP2 scans.

## Step 2 — Run a Bounded Stratified Canary (`--kind experiment`)

1. Select 4–8 hard pages spanning frontmatter/title, multi-letter pages, dense footnotes, and polyglot/archaic orthography.
2. Execute extraction strictly in `experiment` mode with an explicit USD ceiling:
   ```bash
   uv run lanterns extract <book_id> \
     --kind experiment --run-id exp-canary-01 \
     --pages <page_spec> --cost-ceiling-usd 0.30
   ```
3. Compile a preview build into `workspace/builds/previews/<book_id>/exp-canary-01/`:
   ```bash
   uv run lanterns compile <book_id> --run-id exp-canary-01
   ```

## Step 3 — Pixel Verification & Promotion Gate

1. Open at least 2 rendered scan `.png` files under `workspace/experiments/<book_id>/exp-canary-01/renders/` with `read_file` and compare pixel-for-pixel against the normalized `PageIR` JSON (`pages/page_NNNN.json`) and preview HTML.
2. Confirm:
   - `0` dropped content blocks (`source_block_coverage`).
   - Exact preservation of diacritics, long-s `ſ`, ligatures `æ`/`œ`, marginalia, and footnote callouts.
   - Clean letter splits (`starts_new_letter` vs. `starts_enclosure`).
3. Log the canary run and exact spend from `cost_ledger.jsonl` in `docs/LAB_NOTEBOOK.md`. Only after canary verification passes should you run `--kind canonical` and `compile --set-active`.
