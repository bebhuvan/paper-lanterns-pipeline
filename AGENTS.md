# AGENTS.md — Paper Lanterns Constitutional Reference

Read this file completely before modifying any prompt, schema, compiler pass, renderer, or test. Every rule below is enforced mechanically by `./verify`, `uv run lanterns doctor`, and `lanterns.validation.architecture_guard`. Do **not** create `.agents/rules/*.md` files (`AGENTS.md` is the single normative constitution).

---

## 1. The Archival North Star: 100% Diplomatic Fidelity

Paper Lanterns is archival preservation work—**never** modernization, copy-editing, or summarization:

1. **Preserve Every Printed Character & Mark:**
   - Retain period spellings (`shew`, `connexion`, `publick`, `Mahomedan`), original printer errata, `[sic]`, editorial square brackets (`[? May 1849]`, `[torn]`), and cipher/redaction dashes (`M——`, `Count ***`) verbatim.
   - Preserve lexical/historical ligatures and letterforms (`æ`, `œ`, `Æ`, `Œ`, medial long-s `ſ`) while decomposing ONLY pure presentation ligatures `U+FB00..U+FB06` (`ﬀ`, `ﬁ`, `ﬂ`, `ﬃ`, `ﬄ`) during NFC normalization (`core/unicode_norm.py`).
   - Preserve exact historical Romanization schemes (Hunterian `Rájput`, `Kâshmîr` vs. Geneva IAST `Rājput`, `Śiva`, `ṛ`, `ṣ`, `ṃ`, `ḥ`), exact printed European diacritics (`depôt` vs. `dépôt`), and native inline scripts (Devanagari, Bengali, Gujarati, Tamil, Perso-Arabic, Greek, Cyrillic).
2. **Preserve Epistolary Spatial Layouts in Exact Source Order:**
   - Never reorder blocks to fit a modern template. Bottom-placed datelines, cascading valedictions (`alignment="cascading"`, `line_breaks_preserved=True`), postscripts (`P.S.`), superscriptions (`address_block`), nested enclosures (`ChildSpan`), and outer-margin shoulder notes (`margin_note`) remain in exact printed sequence.
3. **Separate Only Page Furniture—Conserve 100% of Content:**
   - Only `running_header`, `folio`, `scanner_residue`, and `noise` are excluded from reading prose (while remaining auditable in `PageIR`).
   - Every non-furniture `BlockIR` (including `facsimile_plate`) is verified by `source_block_coverage` across both `BookSection.items` and `BookSection.units` for **100% mathematical block conservation**.

---

## 2. Five-Plane Storage & Experiment Isolation Law

Never mix test/canary runs with canonical book editions:

| Plane | Path | Git Tracked? | Rule |
| :--- | :--- | :---: | :--- |
| **1. Sources** | `sources/` | Gitignored | Raw PDFs & JBIG2-free `.originals.pdf` JP2 assemblies; immutable once hashed (`source_pdf_sha256`). |
| **2. Book State** | `collections/`, `books/<book_id>/` | **Tracked** | `book.toml`, `structure_overrides.json` (plus legacy `letter_map.json`/`chapter_map.json`), `corrections/*.json`, `proof_ledger.json`, `scan_overrides/page_NNNN.json`, `audit_checks.jsonl`. |
| **3a. Experiments** | `workspace/experiments/` | Gitignored | All `--kind experiment` canaries & benchmarks. Preview compiles go to `workspace/builds/previews/`. |
| **3b. Canonical Runs** | `workspace/runs/<book_id>/<run_id>/` | Gitignored | Only `--kind canonical` runs eligible for `--set-active` (`ir/pages.json`, `ir/structure_auto.json`, `cost_ledger.jsonl`). |
| **4. Library** | `library/<collection>/<book_id>/` | **Tracked** | Compiled Web, Individual Online Letters, EPUB 3.3, Scholarly Markdown, TEI P5 XML, and Proofing Studio. |
| **5. Edge Relay** | `workers/proofing_relay/` | **Tracked** | Stateless Cloudflare Worker + D1 schema for lease-enforced `P1 -> P2` community proofing & daily Git sync. |

---

## 3. Conveyor-Belt Compiler Law & AST Anti-Bloat Gate (`architecture_guard.py`)

`epistolary/` and `renderers/` are deterministic compilers over typed `BlockIR` / `CollectionAST` structures:

1. **Zero Content-Word Regexes or Book Branching:** Never match natural-language words (`"dear"`, `"yours"`, `"sincerely"`, month/correspondent names) in `epistolary/` or `renderers/`, and never write `if book_id == ...` anywhere in `src/lanterns/`. Route strictly on `BlockIR.type`, `epistolary_transition`, `alignment`, `bbox`, continuation flags, `heading_level`, `note_label`, `<fnref>`, and anchored structure plans/overrides.
2. **Strict Acyclic Layer DAG:** `core -> source -> providers -> extraction -> epistolary -> validation -> renderers -> ops -> cli`. Only `providers/` imports VLM SDKs (`google.genai`, `anthropic`, `httpx`). Whole-book LLM structure passes (`Pass 2A/2B/2C`) live in `extraction/structure_pass.py`; `epistolary/` stays 100% pure and `$0` offline.
3. **Ratcheted Complexity & Branch Budgets:** Per-function McCabe complexity `<= 18`; subsystem branch budgets `epistolary <= 160`, `renderers <= 230`, `ops <= 100`; global prompts `<= 2,000` words/file (`<= 4,500` total) with zero book-specific proper nouns; zero silent `except Exception: pass`; tuning vs. held-out parity on `benchmarks/epistolary_exam/`.

---

## 4. Dual Footnotes, Sacred Human Provenance & Zero-`innerHTML` Security

- ** Wire `<fnref>` vs. `<sup>` & Dual Apparatus:** Pass-1 tags footnote/endnote callouts as `<fnref>...</fnref>` and typographic superscripts (`yᵉ`, `Sʳ`, `dº`, `ˡⁱ`) as `<sup>...</sup>` (with a deterministic attested-footnote fallback for legacy `<sup>`). `compiler.py` populates `BookSection.footnote_ids`, `StructuralUnit.footnote_ids`, and `LetterUnit.footnote_ids` without dropping orphans (`is_orphan=True`), and never suppresses `footnote_parity` warnings via machine `scan_check`/`machine_reread` records (`human_only=True`).
- **Human Work Is Sacred:** Pages marked `p1_proofed` or `verified` (`p2_verifier_id != p1_reviewer_id`) in `proof_ledger.json` are never overwritten by model runs, and `BoundaryAnchor` rebasing preserves human `structure_overrides.json` across single-page re-extractions.
- **Zero-`innerHTML` DOM & Strict CSP:** `renderers/` and `proofing_studio.py` never use `innerHTML`, `outerHTML`, or `insertAdjacentHTML`; all interactive scripts build DOM nodes with `document.createElement` + `textContent` under strict Content-Security-Policy headers.

---

## 5. SDK, Model Routing & Spend Discipline

Before modifying `src/lanterns/providers/`, `config/pricing.json`, or `config/profiles.json`, consult [docs/reference/gemini-api/INDEX.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/gemini-api/INDEX.md) and [docs/reference/claude-api/INDEX.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/claude-api/INDEX.md):

1. **Modern SDKs & Locked v3 Models:** Require `google-genai >= 3.2.0` (`from google import genai`) and `anthropic >= 2.8.0`. Never use `google-generativeai`, legacy `gemini-1.5/2.0/2.5-*`, or `gemini-4-argon` in active profiles. Primary Pass-1 Batch uses `gemini/gemini-4-hydrogen` + symmetric `anthropic/claude-sonnet-5-5` fallback (`[L01]` reserved strictly for Tier-2 `RECITATION`/content-filter rescue); Pass 2A/2B uses `gemini/gemini-3.8-flash`; Pass 2C uses `gemini/gemini-decision-1` (`client.decisions.create` on `v1beta` with `"criteria"` dict, escalating `< 0.85` confidence to `gemini-4-hydrogen`).
2. **SDK Parameter Traps:** Set `MEDIA_RESOLUTION_ULTRA_HIGH` per-part only (never on global `GenerateContentConfig`); clamp `thinking_level="minimal"` up to `"low"` on Gemini `3.7/3.8/4-*`; never pass `temperature=0.0` to Gemini 3/4; always pass `BLOCK_NONE` archival safety settings and explicit millisecond `timeout` alongside `api_version` inside `http_options`; enforce `config/pricing.json` + `--cost-ceiling-usd` via `CostLedger` before spending a token.

---

## 6. Documentation Philosophy: Four Jobs, One Owner Per Fact, Telemetry in JSON

Documentation sprawl and drift are treated as architectural defects enforced by `uv run lanterns doctor`:

1. **Root (`4` files only):** `README.md` (quickstart & map), `AGENTS.md` (single constitution), `DECISIONS.md` (concise ADRs for architectural trade-offs & falsified hypotheses only), and `CATALOG_STATUS.md` (`100%` auto-generated via `uv run lanterns status`).
2. **`docs/` (`3` core files + `docs/reference/{gemini-api,claude-api}/`):**
   - `docs/FOUNDER_INTENT_AND_MANIFESTO.md` — Philosophical mission.
   - `docs/ARCHITECTURE.md` — Single unified technical specification (pipeline, schemas, diplomatic/rubric standards, `P1 -> P2` proofing, audit checklist). Link to it; never copy its tables elsewhere.
   - `docs/LAB_NOTEBOOK.md` — Reproducible cross-book R&D benchmarks & falsification experiments **only**. Per-book run telemetry belongs strictly in JSON (`edition_manifest.json`, `cost_ledger.jsonl`) and auto-generated `CATALOG_STATUS.md`—never manual prose essays per book, and never leave handoff/scratch `.md` files behind after implementation lands.
