# Paper Lanterns

**An Archival Correspondence Digitization Compiler & Distributed Community Proofreading System**

Paper Lanterns transforms historical scanned correspondence—letters, dispatches, telegrams, diaries-in-letters, and *Life and Letters* volumes—into verified, diplomatic public-domain digital editions. A sister project to **Project Akshara** (*"Project Gutenberg for India"*), Paper Lanterns treats correspondence as a first-class archival medium: simultaneously publishing **Complete Book Editions** (faithful from frontmatter through interleaved biography to index) and **Individual Online Letters** (each self-contained with its own framing context and linked scholarly apparatus).

---

## Core Architectural Pillars

1. **100% Diplomatic Archival Fidelity ("Type What the Printer Printed")**
   Every historical spelling (`shew`, `connexion`), printer erratum, medial long-s (`ſ`), lexical ligature (`æ`, `œ`), cipher dash (`M——`, `***`), editorial bracket (`[? 1848]`, `[torn]`, `[sic]`), outer-margin shoulder note (`1831. Æt. 39.`), and multilingual script or Romanization scheme (Geneva IAST, Hunterian acutes, European diacritics, Greek, Cyrillic, Perso-Arabic, Devanagari, Bengali) survives verbatim with `100%` mathematical non-furniture block conservation (`source_block_coverage`).

2. **Model-Native Structure + Pure Offline Compiler (`CollectionAST`)**
   - **Pass 1 (`extract`):** Extracts page blocks with `<fnref>` vs. `<sup>` separation and `facsimile_plate` preservation via 50%-discounted `gemini/gemini-4-hydrogen` and `anthropic/claude-sonnet-5-5` Batch routes over JBIG2-free `.originals.pdf` camera scans.
   - **Pass 2 (`structure`):** Hierarchical `Pass 2A` (`SectionPlan`) -> `Pass 2B` (parallel compact-tuple `UnitPlan` separating `letter`, `letter_excerpt`, `narrative`, `editorial_bridge`, `document`, `apparatus`, and nested `ChildSpan` enclosures) -> `Pass 2C` (multimodal `gemini-decision-1` boundary verification escalating `< 0.85` confidence to `gemini-4-hydrogen`).
   - **Offline Compile (`compile`, `$0` API spend):** Deterministically merges `structure_auto.json` with human-anchored `books/<id>/structure_overrides.json` into **Complete Book Web HTML**, **Individual Online Letters**, **EPUB 3.3**, **Scholarly Markdown**, **TEI P5 XML (`tei_all.rng` + CMIF `<correspDesc>`)**, and **Proofing Studio**.

3. **Five-Plane Storage Discipline**
   ```text
   Paper Lanterns New/
   ├── sources/          # Plane 1: Immutable raw PDFs & JBIG2-free .originals.pdf (gitignored)
   ├── collections/      # Plane 2a: Declarative collection presets (.toml)
   ├── books/<book_id>/  # Plane 2b: book.toml, structure_overrides.json, corrections/, proof_ledger.json, scan_overrides/
   ├── workspace/        # Plane 3: Ephemeral compute (gitignored: experiments/, runs/, builds/previews/)
   ├── library/<coll>/<book_id>/  # Plane 4: Published Web, Letters, EPUB 3.3, Markdown, TEI P5, Proofing Studio
   └── workers/proofing_relay/    # Plane 5: Lease-enforced P1->P2 Community Proofing Relay (Cloudflare Worker + D1)
   ```

4. **Distributed Proofreaders (`P1 -> P2`) Community Proofing Studio**
   Static, zero-`innerHTML` side-by-side scan studio (`proofing/`) under strict CSP, streaming Archive.org BookReader scans with zero hosting cost. Two distinct human volunteers (`P1 != P2`) certify pages (`unproofed -> p1_proofed -> verified`), and future AI runs never overwrite human-verified text.

---

## Quickstart & Unified CLI (`lanterns`)

```bash
# 1. Install environment & run full verification gate (ruff + pytest + AST guard + doctor)
uv sync
./verify

# 2. Discover & fetch JBIG2-free camera scans from Internet Archive
uv run lanterns search-archive "letters of erasmus"
uv run lanterns fetch-archive <archive_identifier>

# 3. Register a volume & run optical/JBIG2 PDF triage
uv run lanterns init-book newton-bentley-1756 \
  --title "Four Letters from Sir Isaac Newton to Doctor Bentley" \
  --author "Sir Isaac Newton" --year 1756 \
  --collection scientific-and-philosophical-letters \
  --profile archaic_18c_epistolary \
  --source-pdf sources/newton-bentley-letters-1756.pdf
uv run lanterns triage-pdf newton-bentley-1756

# 4. Run a bounded canary experiment (isolated in workspace/experiments/)
uv run lanterns extract newton-bentley-1756 \
  --kind experiment --run-id exp-canary-01 --pages 1-5 --cost-ceiling-usd 0.25

# 5. Run canonical Batch extraction, Pass 2A-2C structure pass, $0 compile & validation
uv run lanterns extract toru-dutt-life-letters-1921 \
  --kind canonical --profile polyglot_epistolary --batch --batch-chunk-size 25 --cost-ceiling-usd 12.00
uv run lanterns structure toru-dutt-life-letters-1921 --cost-ceiling-usd 0.20
uv run lanterns compile toru-dutt-life-letters-1921 --set-active
uv run lanterns validate-edition toru-dutt-life-letters-1921

# 6. Community proofing intake, benchmark, cache GC & catalog status
uv run lanterns intake newton-bentley-1756 path/to/proposal.json
uv run lanterns benchmark
uv run lanterns gc --apply
uv run lanterns status
uv run lanterns doctor
```

---

## Canonical Documentation Map (Single-Source-of-Truth Allowlist)

Enforced mechanically by `uv run lanterns doctor`:

- [README.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/README.md) — Overview, 5-plane layout, CLI quickstart, and documentation map.
- [AGENTS.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/AGENTS.md) — Single constitutional rulebook and documentation philosophy.
- [DECISIONS.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/DECISIONS.md) — Architectural Decision Records (`D1`+) and falsified hypotheses.
- [CATALOG_STATUS.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/CATALOG_STATUS.md) — Auto-generated master edition & spend ledger (`uv run lanterns status`).
- [docs/FOUNDER_INTENT_AND_MANIFESTO.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/FOUNDER_INTENT_AND_MANIFESTO.md) — Philosophical and historical mission of Paper Lanterns.
- [docs/ARCHITECTURE.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/ARCHITECTURE.md) — Unified technical specification (compiler pipeline, diplomatic standards, 100-Point Rubric, `P1 -> P2` proofing, and adversarial audit checklist).
- [docs/LAB_NOTEBOOK.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/LAB_NOTEBOOK.md) — Reproducible cross-book R&D benchmarks and empirical falsification experiments.
- [docs/reference/gemini-api/INDEX.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/gemini-api/INDEX.md) & [docs/reference/claude-api/INDEX.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/docs/reference/claude-api/INDEX.md) — Active VLM SDK reference snapshots.
