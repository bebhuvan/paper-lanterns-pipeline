# AGENTS.md — Paper Lanterns v2 Constitutional Reference

Read this file completely before modifying any prompt, schema, compiler pass, renderer, or test in this repository. Every rule below is enforced mechanically by `./verify`, `uv run lanterns doctor`, and `lanterns.validation.architecture_guard`.

---

## 1. The Archival North Star: 100% Diplomatic Fidelity

Paper Lanterns is archival preservation work, **never** modernization, copy-editing, or summarization:

1. **Preserve Every Printed Character & Mark:**
   - Retain period spellings (`shew`, `connexion`, `publick`, `Mahomedan`), original printer errata, `[sic]`, editorial square brackets (`[? May 1849]`, `[torn]`), and cipher/redaction dashes (`M——`, `Count ***`) verbatim.
   - Preserve lexical/historical ligatures and letterforms (`æ`, `œ`, `Æ`, `Œ`, medial long-s `ſ`) while decomposing ONLY pure presentation ligatures `U+FB00..U+FB06` (`ﬀ`, `ﬁ`, `ﬂ`, `ﬃ`, `ﬄ`) during NFC normalization (`core/unicode_norm.py`).
   - Preserve exact historical Romanization schemes (19th-century Hunterian/Jonesian `Rájput`, `Kâshmîr` vs. Geneva IAST `Rājput`, `Śiva`, `ṛ`, `ṣ`, `ṃ`, `ḥ`), European accents (`depôt` vs. `dépôt` exactly as printed on the scan!), Greek, Cyrillic, Perso-Arabic (Urdu/Farsi/Arabic), Devanagari, Bengali, and regional Indic scripts.
2. **Preserve Unusual Epistolary Spatial Layouts in Source Order:**
   - Never reorder blocks to fit a modern template. Bottom-placed datelines (printed after `body` or `signature`, as in Erasmus, Goethe, or 18th-century letters), cascading multi-line valedictions (`alignment="cascading"`, `line_breaks_preserved=True`), postscripts (`P.S.`), superscriptions/endorsements (`address_block`), and outer-margin shoulder notes (`margin_note`, e.g., `1831. Æt. 39.`) must remain in their exact printed sequence and visual alignment.
3. **Separate Only Page Furniture—Drop Zero Content:**
   - Only `running_header`, `folio`, `scanner_residue`, and `noise` are excluded from reading prose (while remaining auditable in `PageIR`).
   - Every non-furniture `BlockIR` (`body`, `editorial`, `letter_header`, `dateline`, `salutation`, `valediction`, `signature`, `postscript`, `address_block`, `footnote`, `margin_note`, `verse`, `blockquote`, `heading`, `table_row`, etc.) is verified by `source_block_coverage` for **100% mathematical block conservation**.

---

## 2. Five-Plane Storage & Experiment Isolation Law

Never mix test/canary runs with canonical book editions:

| Plane | Path | Git Tracked? | Rule |
| :--- | :--- | :--- | :--- |
| **1. Sources** | `sources/` | Gitignored (`.gitkeep` only) | Raw PDFs and Archive.org JP2 downloads; immutable once hashed (`source_pdf_sha256`). |
| **2. Book & Collection State** | `collections/`, `books/<book_id>/` | **Tracked** | `book.toml`, `letter_map.json`, `chapter_map.json`, `corrections/*.json`, `proof_ledger.json`, `scan_checks/page_NNNN.json`. |
| **3a. Experiments** | `workspace/experiments/<book_id>/<exp_id>/` | Gitignored | All `--kind experiment` canaries, prompt bake-offs, and multi-model comparisons. Preview compiles go to `workspace/builds/previews/`. |
| **3b. Canonical Runs** | `workspace/runs/<book_id>/<run_id>/` | Gitignored | Only `--kind canonical` runs eligible for `--set-active` promotion. Every API call appends to crash-durable `cost_ledger.jsonl`. |
| **4. Published Library** | `library/<collection>/<book_id>/` | **Tracked** | Compiled Complete Book Web, Individual Online Letters, EPUB 3, Markdown, TEI P5 XML, and Proofing Studio. |
| **5. Community Relay** | `workers/proofing_relay/` | **Tracked** | Stateless Cloudflare Worker + D1 schema for `P1 -> P2` community proofing checkout & daily Git export. |

---

## 3. Conveyor-Belt Compiler Law & AST Anti-Bloat Gate (`architecture_guard.py`)

`epistolary/` and `renderers/` are deterministic compilers over typed `BlockIR` / `CollectionAST` structures—**never** natural-language guessers. `architecture_guard.py` parses the codebase with Python's `ast` module and blocks `./verify` and `lanterns doctor` on any violation of these seven laws:

1. **Zero Content-Word Regexes or Prose Heuristics:** Never match natural-language words (`"dear"`, `"yours"`, `"sincerely"`, month names, author/correspondent names) in `epistolary/` or `renderers/`. Route strictly on `BlockIR.type`, `epistolary_transition`, `alignment`, `bbox`, `continues_from_prev`/`continues_to_next`, `heading_level`, `note_label`, and `letter_map.json`.
2. **Zero Book or Collection Branching:** Never write `if book_id == ...`, author conditionals, or book-specific code branches anywhere in `src/lanterns/`.
3. **Bounded Cyclomatic & Subsystem Branch Complexity:** Per-function McCabe complexity must stay `<= 18`, and total AST branch nodes + module counts across `epistolary/` and `renderers/` must stay within subsystem budgets (splitting hacky `if/else` trees across tiny files is caught and rejected).
4. **Strict Acyclic Layer DAG:**
   `core -> source -> providers -> extraction -> epistolary -> validation -> renderers -> cli`
   Only `providers/` may import `google.genai` or VLM HTTP clients. `epistolary/` never imports `providers/` or `renderers/`. `renderers/` never re-implement boundary segmentation, dehyphenation, or footnote linking.
5. **Bounded Prompt Budget & Zero Proper-Noun Patches:** Global prompts (`prompts/*.md`) stay within attention word caps and never accumulate book-specific proper nouns.
6. **Zero Silent Exception Swallowing:** `except Exception: pass` and bare `except:` without recording typed errors/findings or `failed_closed=True` are forbidden.
7. **Tuning vs. Held-Out Generalization:** Every boundary or normalization change must pass `benchmarks/epistolary_exam/` across both `tuning` and `held_out` splits without overfitting.

---

## 4. Dual Footnote Projection & Human-First Provenance

- **Dual Footnote Projection & Multi-Paragraph Coalescing:** `compiler.py` resolves cross-page split footnotes (`continues_from_prev=True` and adjacent unlabelled continuations), same-page multi-paragraph footnote continuations (`note_label=None` immediately following an active note on the same page), backmatter `Page N.` endnotes via `folio_to_pnum`, single/double-spaced side-by-side footnotes, sub-footnote symbols (`*`, `||`), shared TOC/volume legend symbols, `margin_note` callouts, and per-page resetting markers (`*`, `†`, `‡`, `§`, `‖`, `a`, `1`), populating **both** `BookSection.footnote_ids` (for Chapter-End Apparatus in the Complete Book Edition) and `LetterUnit.footnote_ids` (for Self-Contained Apparatus in Individual Online Letters). Unmatched footnotes remain preserved with `is_orphan=True` (`[Unlinked note, p. <folio>]`)—never dropped, and `footnote_parity` warnings are never suppressed by machine `scan_check`/`machine_reread` records (`human_only=True`).
- **Human Work Is Sacred:** Pages marked `p1_proofed` or `verified` in `books/<book_id>/proof_ledger.json` are **never** overwritten by subsequent model runs (`epistolary/corrections.py`), empty `accepted_blocks: []` snapshots never erase extracted blocks, and `intake_proposal` hashes post-`resolve_page_ir_line_wraps` text with `witness_dir`. `verified` status requires two distinct human reviewers (`p2_verifier_id != p1_reviewer_id`).
- **Zero-`innerHTML` Security:** `proofing_studio.py` and all interactive scripts construct DOM nodes exclusively with `document.createElement` and `textContent` under a strict Content-Security-Policy.

---

## 5. SDK, Model & Anti-Sprawl Discipline

1. **Modern Gemini SDK Only:** Require `google-genai >= 3.2.0` (`from google import genai`). Consult [docs/reference/gemini-api/INDEX.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/gemini-api/INDEX.md) before touching provider code. Never use `google-generativeai` or deprecated `gemini-1.5-*` / `gemini-2.0-*` / `gemini-2.5-*` models.
2. **Fail-Closed Cost Governance:** Every model route in `config/profiles.json` or CLI flags must exist in `config/pricing.json` before a single token is spent, and `--cost-ceiling-usd` is enforced thread-safely via `CostLedger`.
3. **Evidence Before Claims:** Never claim a book, page, or fallback path works without executing it, inspecting the rendered scan PNG with native vision (`read_file`), and recording spend and pixel-level findings in `docs/LAB_NOTEBOOK.md` and `docs/ADVERSARIAL_REVIEW_FRAMEWORK.md`.
4. **Strict Documentation Cap:** Do not create extra `.md` files in the repository root or `docs/` outside the allowlist enforced by `uv run lanterns doctor`. Record architectural decisions in `DECISIONS.md` and empirical runs in `docs/LAB_NOTEBOOK.md`.
