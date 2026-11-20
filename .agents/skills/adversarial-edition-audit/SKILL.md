---
name: adversarial-edition-audit
description: >-
  Use this skill when performing an end-to-end adversarial audit of a compiled Paper Lanterns
  edition across Web HTML, Individual Online Letters, EPUB 3, Markdown, TEI P5 XML, Proofing
  Studio, and pixel-level source scans before certifying publication readiness.
---

# Adversarial Edition Audit Runbook

Never trust administrative status flags alone. Execute every check below before certifying an edition.

## Step 1 — Automated Code, Security & Edition Gates

1. Run the repository-wide quality and AST architecture gate:
   ```bash
   ./verify
   uv run lanterns doctor
   ```
2. Validate the compiled edition directory (`library/<collection>/<book_id>/`):
   ```bash
   uv run lanterns validate-edition <book_id>
   ```
   - Confirm 0 blockers across `source_block_coverage` (100% non-furniture block conservation), `control_char_or_mojibake`, `footnote_parity`, EPUB 3 OPF/XHTML structure, TEI P5 XML well-formedness (`<correspDesc>` + unique `xml:id`s), standalone `letters/<slug>.{html,md,json}` bidirectional `#fn`/`#fnref` links, and `proofing/app.js` zero-`innerHTML` static scan.

## Step 2 — Pixel-for-Pixel Native-Vision Spot Audit

1. Open at least 6 rendered `.png` page scans with `read_file` across hard pages (multi-letter leaf, dense footnotes, archaic long-s/ligatures, marginalia, cross-page continuation, non-English/Indic diacritics).
2. Compare the scan pixels directly against:
   - `library/<collection>/<book_id>/web/` (chapter stream + chapter-end footnotes)
   - `library/<collection>/<book_id>/letters/<slug>.html` (standalone letter + self-contained footnotes + spatial alignment classes `.align-cascading`, `.align-right`, `line_breaks_preserved`)
   - `library/<collection>/<book_id>/tei/<book_id>.tei.xml` and `markdown/<book_id>.md`.
3. Verify donor safety: confirm `git status` or mtimes in any external reference directories remain completely untouched.

## Step 3 — Record Evidence & Regenerate Catalog Ledger

1. Record per-book audit checks in `books/<book_id>/audit_checks.jsonl` (and cross-book R&D findings, if any, in `docs/LAB_NOTEBOOK.md`).
2. Regenerate `CATALOG_STATUS.md` and re-run `./verify`:
   ```bash
   uv run lanterns status
   ./verify
   ```
