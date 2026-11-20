# Paper Lanterns — Empirical R&D & Falsification Lab Notebook

> **Scope Rule (`AGENTS.md` §6):** This notebook records **only** cross-cutting R&D experiments, model evaluations, and architectural falsification benchmarks. Routine per-book canonical extraction counts, rubric scores, and USD spend live strictly in machine-readable `books/*/`, `library/*/*/edition_manifest.json`, and auto-generated [CATALOG_STATUS.md](file:///home/bebhuvan/Documents/Akshara%20wokspace/Paper%20Lanterns%20New/CATALOG_STATUS.md).

---

## 1. Cross-Era 9-Model VLM Benchmark & Optical Witness Law (`2026-11-16`, `D12–D14`)

- **Optical Triage Law (`triage-pdf` & `fetch-archive`):** Audit across Internet Archive derivatives revealed `86.5%` bitonal `/JBIG2Decode` or heavy MRC mask risk (causing silent glyph substitutions on faded 16th–18th c. type). Streaming raw `*_jp2.zip` + `scandata.xml` into JBIG2-free `.originals.pdf` (`300–600 PPI` continuous-tone `/JPXDecode`) eliminated compression-induced OCR hallucinations at `$0` extra API cost.
- **9-Model × 4-Archetype Bake-Off (`newton-bentley-1756` 18c long-s `ſ`, `erasmus` polyglot Latin/Greek, `hooker-1848` foxed botanical, `tolstoy-1903` Russian acutes/TOC; `$1.84` total):**
  - **All-Era / Pre-1800 Long-s (`ſ`) & Polyglot Leaders:** `gemini/gemini-4-hydrogen` and `anthropic/claude-opus-5-5` achieved `17/17` exact medial `ſ`, `Labiatæ`, and polytonic Greek preservation.
  - **Post-1800 Workhorse:** `anthropic/claude-sonnet-5-5` Batch (`~$0.011–$0.012/page`) preserved European/Russian diacritics and two-column indices cleanly (with Tier-2 `[L01]` scanline rescue recovering `100%` of HTTP 400 content-filtered pages), but drops medial `ſ` on pre-1800 texts.
  - **Disqualified Routes:** `claude-haiku-5-5` dropped Russian tonic acutes and collapsed `CONTENTS` tables; `qwen3.7-plus` (`0/4` valid wire JSON) and `qwen3.8-flash` (`1/4` valid wire JSON) failed structured-output reliability.

---

## 2. 25-Book Zero-Override Ablation & Cross-Family Verification (`2026-11-18`, `D18–D19`)

- **Zero-`letter_map.json` Corpus Ablation (`25` books, `8,949` pages, `2,816` letters, `6,193` footnotes, `$0.00` spend):**
  - Raw Pass-1 VLM extraction achieved **`99.8%` letter-start recall (`2,811/2,816`)** and **`0` cross-page seam footnote drops** once `CONTINUABLE_PROSE_TYPES` + `dedup_or_join_cross_page_tagged` + `Page N.` backmatter endnotes were wired.
  - Exposed why v2 still needed heavy manual `chapter_map.json`/`letter_map.json` curation on full 39-book expansion (`13,385` pages, `5,048` letters, `$168.70` historical canonical spend): `42.4%` raw missing senders came from volume-implicit correspondent omission, and anthology/*Life & Letters* books swallowed 3rd-person biographies or enclosed reports into adjacent letters.

---

## 3. Gemini 4 Carbon Two-Session Falsification Lab (`2026-11-20`, `83` Calls, `$1.239225`)

Reproducible scripts, raw wires, crop PNGs, and `cost_ledger.jsonl` preserved in gitignored `workspace/experiments/_carbon_falsification/`:

### 3.1 Blind 12-Page Uncorrected Pixel Audit (`random.Random(20261120)`)
- Across `12` uncorrected pages (`51` non-furniture blocks, `3,299` words, `17,640` chars): **Lexical WER = `0 / 3,299 (0.00%)`**, **Alphanumeric/Diacritic CER = `0.000%`**, strict punctuation/thin-space CER = `0.266%` (`47 / 17,640`), proving Pass-1 OCR is already `99.73–100%` glyph-accurate while structural/renderer compliance held `33.3%` page-level defects.

### 3.2 Hypothesis Falsification Matrix (`E1–E4`)

| Exp | Hypothesis Tested | Calls / Spend | Verdict & Binding v3 Design Constraint |
| :--- | :--- | ---: | :--- |
| **E1** | Pointer-only whole-book LLM structure pass (`gemini-3.8-flash`) vs. raw Python | `9` / `$0.3677` | **HARDENED -> ADOPTED:** Jumped autonomous metadata `0–55% -> 98.6–100%`, `0.967–0.993` `IoU>=0.5` F1, `0` hallucinated IDs, and beat human `letter_map.json` on `bapu-mira` `#357/#358` (Orissa report) & `fifty-famous` p. 29 (Socrates bio). **Killed monolithic JSON** (`52,309 / 65,536` out-tokens at 390 letters) and `110p` sliding windows (`0.8889 -> 0.8387` F1) -> split **Pass 2A** (`SectionPlan`) from parallel section-aligned **Pass 2B** compact 10-tuples (`~31` tok/unit) and never compress quoted or `:`-preceded `body` blocks (`toru-dutt` p. 348). |
| **E2** | Wire `<fnref>` vs. `<sup>` separation without `_ABBREV_SUP_PAIRS` (`8` hardest pages: `17` callouts, `23` abbrevs `yᵉ`, `Sʳ`, `dº`, `ˡⁱ`) | `24` / `$0.5447` | **HARDENED -> ADOPTED:** `0 / 23 (0.0%)` abbreviation false positives across all models; `gemini-4-hydrogen` hit **`17/17 (100% F1)`**, whereas `claude-sonnet-5-5` hit `82.35%` recall (`14/17`). Deleted name-mutilating `_ABBREV_SUP_PAIRS` (`Robᵗ -> Rob`) while keeping a 10-line attested-page-footnote `<sup>` fallback in `compiler.py`. |
| **E3** | Dual-witness (`3.8-flash [L01]` + `haiku-5-5` + 450-DPI crops) for `$1.55/350p` | `28` / `$0.1734` | **KILLED:** Haiku paragraph merges drove `39.24%` block disagreement (`$12.05/350p` crop explosion), correlated false consensus collapsed Sydney Smith's 3 paragraphs on `nineteenth_c_p60` (which single-pass `gemini-4-hydrogen` split cleanly), and `2027-01-01` Flash pricing (`$1.50/$7.50`) equals `gemini-4-hydrogen` (`~$4.35/350p` Batch, no 2027 hike). |
| **E4a** | Padding static prompt `> 4,096` tokens for `gemini-4-hydrogen` Implicit Caching | `5` / `$0.1347` | **KILLED:** `0/2` cache hits on `5,222`-token padded prompt (`+22.2%` input cost), while canonical Batch logs proved unpadded `3,881`-token prompts already cache when total input `>= 4,096`. Keep prompts lean. |
| **E4b** | Global `2,500` visual-token cap on `claude-sonnet-5-5` | `4` / `$0.0170` | **KILLED GLOBALLY:** Saved `$0.80/350p` Batch but corrupted diplomatic cap-accent fidelity on `toru_p75` (`Evêque -> Évêque`). Keep `4,784` vt default. |
| **E4c** | Multimodal `gemini-decision-1` (`v1beta`, `0` out-tokens) + cropped boundary strips | `12` / `$0.0018` | **PROVEN (`12/12 = 100%`):** `$0.000146`/boundary (`1.5¢` / 100 seams); wire `< 0.85` confidence (`2/12` cases) to escalate to `gemini-4-hydrogen`. |
| **IA** | Archive.org BookReader `/page/n{k}` vs. IIIF `${leaf_index}` across `38/38` IA books | `$0.0000` | **BookReader PROVEN (`0` leaf drift, pixel MAD `0.3–2.2`); IIIF `${leaf_index}` BROKEN** (`+1..+11` leaf drift on `34/38` books, HTTP 500 on `11/38` `_0001.jp2` books). |
