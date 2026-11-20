# Paper Lanterns v2

**An Archival Correspondence Digitization Compiler & Distributed Community Proofreading System**

Paper Lanterns v2 transforms historical scanned correspondence—letters, dispatches, telegrams, diaries-in-letters, and *Life and Letters* volumes—into verified, diplomatic public-domain digital editions. A sister project to **Project Akshara** (*"Project Gutenberg for India"*), Paper Lanterns treats letters as a first-class archival medium: simultaneously publishing **Complete Book Editions** (faithful from frontmatter to index) and **Individual Online Letters** (each self-contained with its own framing context and linked scholarly apparatus).

---

## Core Architectural Pillars

1. **100% Diplomatic Archival Fidelity ("Type What the Printer Printed")**
   Every historical spelling (`shew`, `connexion`), printer erratum, medial long-s (`ſ`), lexical ligature (`æ`, `œ`), cipher/redaction dash (`M——`, `***`), editorial bracket (`[? 1848]`, `[torn]`, `[sic]`), marginal shoulder note (`1831. Æt. 39.`), and multilingual script or Romanization scheme (Geneva IAST, Hunterian/Jonesian acutes, Latin, German, French, Greek, Cyrillic, Perso-Arabic, Devanagari, Bengali) survives verbatim. Only running page headers, bare page folios, and scanner detritus are separated from reading prose—and even those remain auditable in `PageIR`.

2. **One `CollectionAST`, Two First-Class Reading Projections**
   - **Complete Book Edition (`web/`, `epub/`, `markdown/`, `tei/`)**: Preserves the volume's full architecture (frontmatter, chapters or unchaptered epistolary parts, interleaved editorial biography, appendices, index) with **Chapter/Section-End Bidirectional Footnotes** (`#fnref-*` <-> `#fn-*`) and TEI P5 `<correspDesc>` metadata.
   - **Individual Online Letters Edition (`letters/`)**: Every `LetterUnit` is compiled into a standalone, linkable letter page (`<slug>.html`, `<slug>.md`, `<slug>.json`, plus interactive `letters/index.html` catalog searchable with diacritic-insensitive matching) carrying its own **Self-Contained Per-Letter Footnote Apparatus**, spatial typography (cascading valedictions, right-aligned or bottom-placed datelines, postscripts, address blocks), interactive page-scan viewer (`#scan-drawer`), and adjacent editorial framing notes.

3. **Five-Plane Storage Discipline & Strict Experiment Isolation**
   ```text
   Paper Lanterns New/
   ├── sources/          # Plane 1: Immutable raw PDFs & Archive.org JP2 downloads (gitignored)
   ├── collections/      # Plane 2a: Declarative collection presets (.toml)
   ├── books/<book_id>/  # Plane 2b: Durable book metadata, corrections/, proof_ledger.json, letter_map.json, chapter_map.json, scan_checks/
   ├── workspace/        # Plane 3: Ephemeral compute (gitignored)
   │   ├── experiments/<book_id>/<exp_id>/   # Canary & multi-model tests (NEVER pollutes runs/ or library/)
   │   ├── runs/<book_id>/<run_id>/          # Canonical volume extraction runs + cost_ledger.jsonl
   │   └── builds/previews/<book_id>/<run>/  # Preview builds for non-active/experiment runs
   ├── library/<coll>/<book_id>/             # Plane 4: Verified canonical multi-format editions
   └── workers/proofing_relay/               # Plane 5: Zero-innerHTML P1->P2 Community Proofing Relay (Cloudflare Worker + D1)
   ```

4. **Multi-Model, Multi-Harness Resilience & 50%-Off Batch Mode (`google-genai >= 3.2.0`, `anthropic >= 2.8.0`)**
   Primary extraction routes through empirically validated 2026 models (`anthropic/claude-opus-5-5`, `anthropic/claude-sonnet-5-5`, `qwen_cloud/qwen4-max`, `gemini/gemini-4-hydrogen`, `gemini/gemini-3.8-flash`, surgical `gemini-4-argon` crop verification, and `gemini-decision-1` calibrated 0-output-token boundary arbitration) backed by a **Cross-Family Fallback Router**, crash-resumable **50%-discounted `--batch` mode** (`25`-page `quality=94` WebP chunks with recursive HTTP `413` auto-halving), automatic **JBIG2-free `.originals.pdf` JP2 camera assembly** (`lanterns fetch-archive`), and native-vision `scan_checks/` overrides.

5. **Distributed Proofreaders (`P1 -> P2`) Community Proofing Studio**
   Static, offline-capable and IIIF-streaming side-by-side scan studio (`proofing/`) built with **zero `innerHTML`** and strict CSP. Two distinct human volunteers (`P1 != P2`) certify pages (`unproofed -> p1_proofed -> verified`), and subsequent AI re-extractions **never** overwrite human-verified text.

---

## Quickstart & Unified CLI (`lanterns`)

```bash
# 1. Install environment (Python >= 3.12, google-genai >= 3.2.0)
uv sync

# 2. Run full quality gate (ruff + pytest + AST architecture_guard + doctor)
./verify

# 3. Discover or fetch public-domain correspondence from Internet Archive
uv run lanterns search-archive "letters of erasmus"
uv run lanterns fetch-archive <archive_identifier>

# 4. Register a book and run optical/JBIG2 PDF triage
uv run lanterns init-book newton-bentley-1756 \
  --title "Four Letters from Sir Isaac Newton to Doctor Bentley" \
  --author "Isaac Newton" --year 1756 \
  --collection scientific-and-philosophical-letters \
  --profile archaic_18c_epistolary \
  --source-pdf sources/newton-bentley-letters-1756.pdf
uv run lanterns triage-pdf newton-bentley-1756

# 5. Run a bounded canary experiment (writes ONLY to workspace/experiments/)
uv run lanterns extract newton-bentley-1756 \
  --kind experiment --run-id exp-canary-01 --pages 1-5 \
  --cost-ceiling-usd 0.25

# 6. Run canonical extraction (interactive or 50%-discounted --batch), hotspot verification, and compilation
uv run lanterns extract voltaire-in-his-letters-1919 \
  --kind canonical --profile claude_sonnet_epistolary \
  --batch --batch-chunk-size 25 --cost-ceiling-usd 14.00
uv run lanterns compile voltaire-in-his-letters-1919
uv run lanterns validate-edition --all

# 7. Ingest community proofing proposals, run benchmark, reclaim render cache & regenerate catalog ledger
uv run lanterns intake newton-bentley-1756 path/to/proposal.json
uv run lanterns benchmark
uv run lanterns gc --apply
uv run lanterns status
uv run lanterns doctor
```

---

## Canonical Documentation Map (Anti-Sprawl Allowlist)

Paper Lanterns enforces a strict documentation cap via `uv run lanterns doctor` so architectural truth never fragments across stale scratch files:

### Repository Root
- [README.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/README.md) — System overview, 5-plane layout, CLI quickstart, and documentation map.
- [AGENTS.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/AGENTS.md) — Constitutional rules for AI agents and human maintainers.
- [DECISIONS.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/DECISIONS.md) — Numbered append-only Architectural Decision Records (`D1`–`D18`).
- `CATALOG_STATUS.md` — Auto-generated master edition & run/experiment spend ledger (`uv run lanterns status`).

### `docs/` Specification Suite
- [docs/FOUNDER_INTENT_AND_MANIFESTO.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/FOUNDER_INTENT_AND_MANIFESTO.md) — Philosophical, historical, and civilizational vision behind Paper Lanterns.
- [docs/ARCHITECTURE.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/ARCHITECTURE.md) — End-to-end compiler specification, dataflow diagrams, and subsystem contracts.
- [docs/ARCHIVAL_STANDARDS_AND_RUBRIC.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/ARCHIVAL_STANDARDS_AND_RUBRIC.md) — Diplomatic transcription rules, Historical Correspondence Peculiarities Taxonomy, and the 100-Point Rubric.
- [docs/COMMUNITY_PROOFING_DESIGN.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/COMMUNITY_PROOFING_DESIGN.md) — Distributed Proofreaders (`P1 -> P2`) workflow, volunteer rulebook, zero-`innerHTML` studio, and Cloudflare/D1/Git sync.
- [docs/ADVERSARIAL_REVIEW_FRAMEWORK.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/ADVERSARIAL_REVIEW_FRAMEWORK.md) — Pixel-level native-vision audit protocol and empirical adversarial review log.
- [docs/LAB_NOTEBOOK.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/LAB_NOTEBOOK.md) — Chronological empirical experiment, model comparison, and API spend log.
- [docs/reference/gemini-api/INDEX.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/gemini-api/INDEX.md) — Durable 2026 Google AI Studio / `google-genai >= 3.2.0` reference snapshot.
