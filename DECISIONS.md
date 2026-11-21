# Architectural Decision Records (DECISIONS.md)

Append-only record of foundational architectural choices in **Paper Lanterns**, why they were made, and what alternatives were rejected. To revise a decision, append a new numbered ADR referencing the superseded entry.

---

## D1 — Five-Plane Storage & Strict Experiment vs. Canonical Run Isolation (2026-11-16)

- **Decision:** Partition all repository state into five non-overlapping planes: (1) `sources/` (immutable raw PDFs/JP2s, gitignored), (2) `collections/` + `books/<book_id>/` (durable tracked config, human corrections, `proof_ledger.json`, `letter_map.json`, `scan_checks/`), (3) `workspace/` (gitignored ephemeral compute split strictly into `workspace/experiments/<book_id>/<exp_id>/`, `workspace/runs/<book_id>/<run_id>/`, and `workspace/builds/previews/`), (4) `library/<collection>/<book_id>/` (tracked canonical editions), and (5) `workers/proofing_relay/` (Cloudflare Worker + D1 community proofing queue).
- **Why:** Earlier digitization pipelines stored human fixes inside run directories and allowed quick single-page prompt tests to overwrite or pollute canonical book shelves and status ledgers. Separating `experiments/` from `runs/` lets operators and QA agents run aggressive canary tests freely while keeping `library/` and `CATALOG_STATUS.md` spotless and provenance-pure.
- **Rejected:** A single `data/books/<id>/extractions/` mutable folder shared by tests and production runs; storing human edits inside ephemeral run folders.

---

## D2 — Lean Epistolary Observation Wire Schema with Constrained Inline Tags (2026-11-16)

- **Decision:** VLMs emit `PageExtractWire` (`schemas/page_extract_wire.schema.json`) containing visual observations only: `printed_folio`, `page_orientation`, `warning_codes`, and ordered `WireBlock` items with 28 semantic `BlockType`s, `[x0, y0, x1, y1]` `0..1000` `bbox`, `alignment`, `epistolary_transition` (`none`, `starts_new_letter`, `starts_enclosure`, `ends_enclosure`), `line_breaks_preserved`, `note_label`, `continues_from_prev`/`continues_to_next`, and verbatim `text` marked up exclusively with six constrained tags (`<i>`, `<b>`, `<sc>`, `<sup>`, `<sub>`, `<unclear>`). `extraction/normalize.py` deterministically parses tags into `(plain_text, list[StyleSpan])` on `BlockIR`.
- **Why:** Asking VLMs to emit 0-based Unicode codepoint offsets (`start`, `end`) wastes ~50% of output tokens and breaks constantly on multi-byte IAST/Indic/European diacritics. Constrained inline tags preserve exact typography at minimal token cost while deterministic Python code computes exact spans and SHA-256 hashes.
- **Rejected:** Character-offset style annotations on the VLM wire; two-pass separate italic re-extraction (`format.py`) which doubled VLM spend and risked text drift between passes.

---

## D3 — Unified `CollectionAST` Driving Both Complete Books and Individual Online Letters (2026-11-16)

- **Decision:** Compile every volume once into a single validated `CollectionAST` (`schemas/collection_ast.schema.json`) containing both the sequential `BookSection` stream (`frontmatter`, `chapter`, `epistolary_part`, `appendix`, `index`, `backmatter`) and the indexed `LetterUnit` sequence (with `preceding_editorial_block_ids` and `following_editorial_block_ids`). All five output formats—Whole-Book Web HTML, Individual Online Letters (`letters/index.html` + `<slug>.{html,md,json}`), EPUB 3.3, Scholarly Markdown, and Archival TEI P5 XML (`<correspDesc>`)—render purely from `CollectionAST`.
- **Why:** Correspondence is read in two distinct ways: cover-to-cover as an edited historical book (where biographer narrative between letters matters), and letter-by-letter as searchable historical artifacts (by sender, recipient, date, or topic). Divergent assembly pipelines inevitably drift in text, numbering, or footnotes.
- **Rejected:** Separate assembly code paths for EPUB vs. Web/Astro; dropping interleaved `editorial` blocks or merging biographer commentary silently into letter bodies.

---

## D4 — Dual Footnote Projection: Chapter-End in Books, Self-Contained in Standalone Letters (2026-11-16)

- **Decision:** `epistolary/compiler.py` stitches cross-page split footnotes (`continues_from_prev=True`), resolves per-page resetting symbols (`*`, `†`, `‡`, `§`, `‖`, `¶`), variant letters (`a`, `b`, `c`), and numbers (`1`, `2`, `3`) into globally unique `FootnoteItem`s (`fn-p0012-1`), and projects references onto **two** surfaces simultaneously:
  1. `BookSection.footnote_ids` — ordered section/chapter-end apparatus for Complete Book Web, EPUB 3 (`epub:type="footnote"`), and Markdown.
  2. `LetterUnit.footnote_ids` — exact subset of footnotes referenced inside that letter (and its attached framing blocks), rendered directly at the bottom of `<slug>.html` and `<slug>.md` with working bidirectional `#fnref-*` <-> `#fn-*` links.
  Unmatched footnotes are retained with `is_orphan=True` and an explicit `[Unlinked note, p. <folio>]` warning—never dropped.
- **Why:** In a standalone online letter, a footnote link pointing to a separate chapter file breaks self-contained reading; in a full book chapter containing 40 short letters, repeating apparatus 40 times interrupts narrative flow unless aggregated cleanly at section end.
- **Rejected:** Global end-of-book footnote dumps; dropping unlinked footnotes when lead type wore away a superscript marker.

---

## D5 — Diplomatic Source-Order Preservation of Unusual Epistolary Layouts (2026-11-16)

- **Decision:** Preserve blocks inside every `LetterUnit` in exact physical reading order alongside spatial metadata (`alignment` in `left`, `center`, `right`, `indented`, `cascading`, `inline`, `two_column_left`, `two_column_right`; `line_breaks_preserved`; `dateline_position` in `top`, `bottom`, `inline`, `absent`).
- **Why:** Historical correspondence across centuries places datelines at bottom-left after the signature (Erasmus, Goethe, 18th-c. English), cascades polite valedictions across four stepped lines (*"I am, / Sir, / Your most obedient / humble Servant"*), prints marginal age/date shoulder notes (`1831. Æt. 39.`), and appends postscripts (`P.S.`) or postal superscriptions (`address_block`) after the signature. Normalizing letters into a rigid `[Header -> Date -> Salutation -> Body -> Close]` template falsifies archival typography.
- **Rejected:** Reordering bottom datelines to the top of the letter; flattening cascading valedictions and multi-line signatures into left-aligned body paragraphs.

---

## D6 — Three-Stage Block-Level Letter Boundary Compiler + `letter_map.json` (2026-11-16)

- **Decision:** Segment correspondence at **`BlockIR` granularity** (never coarse page granularity) across three stages without a single content-word regex:
  1. **Structural & Transition Routing:** Split at `epistolary_transition == "starts_new_letter"` and structural block transitions (`letter_header`, `ornamental_break` preceding opener, new opener after completed `signature`/`postscript`/`address_block`) while keeping `starts_enclosure`..`ends_enclosure` inside the parent `LetterUnit` (`has_enclosure=True`).
  2. **Selective Ambiguity Adjudication:** Escalate ambiguous transitions to `gemini-decision-1` (`client.decisions.create` `noul`/`choice` with 0 output tokens) and `gemini-4-hydrogen` (`boundary_arbiter_v1.md`).
  3. **Deterministic Human Override (`books/<book_id>/letter_map.json`):** Apply `force_split_before_block_ids`, `forbid_split_before_block_ids`, and `explicit_letters` metadata overrides last so human judgment always wins at `$0` re-compile cost.
- **Why:** Pages frequently hold three short letters/telegrams on a single leaf or one letter spanning six pages with a quoted inner letter on page 3. Page-level segmentation cannot represent multi-letter pages, and English regexes (`Dear`, `Yours`) fail immediately on Latin, German, French, or Sanskritized letters.
- **Rejected:** Page-level boundary indices; sliding-window text-LLM re-reading of whole books; regex matching on salutation/valediction words.

---

## D7 — Multi-Model, Multi-Harness Resilience & 4-Tier Anti-`RECITATION` Defense (2026-11-16)

- **Decision:** Concentrate all VLM calls behind `src/lanterns/providers/` supporting native `google-genai >= 3.2.0` (`gemini-4-hydrogen`, `gemini-4-argon`, `gemini-4-carbon`, `gemini-3.8-flash`, `gemini-decision-1`), OpenAI-compatible vision endpoints (`openrouter/qwen/*`, `qwen_cloud/*`, `deepseek/*`), Anthropic (`claude-sonnet-5-5`, `claude-haiku-5-5`), and `gemini_cli_harness` (supporting committed native-vision `books/<book_id>/scan_checks/page_NNNN.json` overrides and optional CLI subprocess execution). Route failures through four tiers: (1) Primary model (with optional `[L01]` scanline prefixing), (2) Same-family alternate model or 2-band horizontal image split on `RECITATION`/`MAX_TOKENS`, (3) Cross-family VLM fallback (`Qwen` / `Claude`), and (4) Explicit `PageIR(failed_closed=True)` banner—never silent page loss.
- **Why:** Famous public-domain letters (Goethe, Napoleon, Gandhi, Newton) can trigger VLM memorization/recitation stops on one model family while succeeding cleanly on `[L01]` scanline-prefixed retries or an independent model family.
- **Rejected:** Single-provider lock-in; retrying the identical model+prompt in a loop; silently skipping failed pages.

---

## D8 — Distributed Proofreaders (`P1 -> P2`) Community Edition & Sacred Human Text (2026-11-16)

- **Decision:** Follow the Distributed Proofreaders / Wikisource two-round page verification model (`unproofed -> p1_proofed -> verified` requiring two distinct human reviewers `p2_verifier_id != p1_reviewer_id`), backed by a lightweight Cloudflare Worker + D1 work-in-progress queue that exports daily to Git. In `epistolary/corrections.py`, pages with human `p1_proofed` or `verified` status in `proof_ledger.json` are **never** overwritten by subsequent model extractions. Edition provenance badges (`blocked < community < machine_checked < source_certified`) report exact machine vs. human page counts honestly.
- **Why:** Losing human volunteer corrections on a model re-run (the historical Trove failure mode) destroys community trust. Conversely, blocking public access for years awaiting 100% manual keyboarding defeats public-domain liberation when a `community` or `machine_checked` edition with co-present scan links is already 99%+ accurate.
- **Rejected:** Per-edit GitHub PR spam from an internet-facing Worker holding repo write tokens; single-reviewer self-verification; overwriting human-proofed text during re-extraction.

---

## D9 — Zero-`innerHTML` DOM Construction & Strict CSP in Proofing Studio and Readers (2026-11-16)

- **Decision:** Forbid `innerHTML`, `outerHTML`, and `insertAdjacentHTML` across `proofing_studio.py` and interactive client scripts (enforced statically by `doctor.py` and `compiled_gate.py`). Build all dynamic UI elements via `document.createElement` and `textContent`, ship page payloads as pure JSON shards (`proofing/pages/pNNNN.json`), and emit a strict `<meta http-equiv="Content-Security-Policy">` header.
- **Why:** Historical transcriptions and community proposals contain raw angle brackets, archaic markup, and untrusted user input. Structural elimination of HTML sink APIs prevents stored/DOM XSS by construction rather than relying on fragile regex sanitizers.
- **Rejected:** Hand-rolled JS HTML sanitizers; embedding page data inside `<script>` string literals.

---

## D10 — Strict Anti-Sprawl Documentation Cap & Evidence-Only Claims (2026-11-16)

- **Decision:** Enforce a fixed allowlist of Markdown files via `uv run lanterns doctor`: four root files (`README.md`, `AGENTS.md`, `DECISIONS.md`, auto-generated `CATALOG_STATUS.md`) and six `docs/` files (`FOUNDER_INTENT_AND_MANIFESTO.md`, `ARCHITECTURE.md`, `ARCHIVAL_STANDARDS_AND_RUBRIC.md`, `COMMUNITY_PROOFING_DESIGN.md`, `ADVERSARIAL_REVIEW_FRAMEWORK.md`, `LAB_NOTEBOOK.md`) plus the durable `docs/reference/gemini-api/` snapshot. Every empirical claim must cite actual executed runs, native-vision pixel checks, and `cost_ledger.jsonl` spend.
- **Why:** Multi-agent and multi-sprint repositories rapidly decay when scratch notes, redundant status summaries, and unverified claims proliferate across dozens of `.md` files.
- **Rejected:** Unbounded `docs/*.md` scratch files; qualitative accuracy claims without page/pixel citations.

---

## D11 — AST-Based Structural Anti-Bloat & Complexity Budget (`architecture_guard.py`) (2026-11-16)

- **Decision:** Enforce architectural cleanliness via Python `ast` analysis (`src/lanterns/validation/architecture_guard.py`) rather than naive per-file `wc -l` line-count tripwires alone. Specifically enforce: (1) AST ban on natural-language content-word regexes/comparisons in `epistolary/` and `renderers/`, (2) ban on `book_id`/author conditionals across `src/lanterns/`, (3) per-function McCabe cyclomatic complexity ceiling (`<= 18`) **plus** total subsystem AST branch-node and module-count budgets across `epistolary/` and `renderers/`, (4) strict acyclic layer imports (`core -> source -> providers -> extraction -> epistolary -> validation -> renderers -> cli`), (5) global prompt word-count & proper-noun hygiene guards on `prompts/*.md`, and (6) ban on silent `except Exception: pass`.
- **Why:** Per-file line-count limits are trivially gamed by splitting tangled special-case branching across 20 tiny helper modules, and naive substring greps miss AST-level regex compilations or layer-inversion imports. Measuring cyclomatic complexity + subsystem-wide branch budgets stops architectural rot at its root.
- **Rejected:** Relying solely on `wc -l` line tripwires; allowing `epistolary/` or `renderers/` to accumulate ad-hoc fallback branches per book.

---

## D12 — Stratified Tuning vs. Held-Out Epistolary Generalization Exam (2026-11-16)

- **Decision:** Maintain a permanent, pixel-verified 12-page multi-book benchmark in `benchmarks/epistolary_exam/` partitioned strictly into `split: "tuning"` (6 pages) and `split: "held_out"` (6 pages) spanning diverse historical correspondence archetypes (multi-letter pages, 18th-c. long-s & cascading closes, Latin dense footnotes, polyglot unchaptered letters, marginalia `Æt.`, Indian correspondence), evaluated automatically by `evaluate_epistolary_exam()` and `uv run lanterns benchmark`.
- **Why:** Tuning prompts or segmentation rules solely on the pages where a bug was discovered invites silent regressions and overfitting. Measuring generalization gap between `tuning` and `held_out` splits guarantees that structural fixes generalize across centuries, scripts, and printers.
- **Rejected:** Single-book smoke testing; tuning rules directly on the entire benchmark without a held-out split.


## D13. Empirical Multi-Provider Routing Law: Qwen 4 Max & Official Anthropic Claude 5.5 Tiers (2026-11-16)
- **Decision:** Integrate the official `anthropic >= 2.8.0` Python SDK (`output_config.format` constrained JSON decoding, `output_config.effort`, `0.05x` prompt caching, Message Batches) across Opus, Sonnet, and Haiku tiers, and `qwen_cloud/qwen4-max` via QwenCloud Global (`reasoning_effort="low"`, `$0.071/1M` cache read). Enforce era-specific routing in `config/profiles.json` based on our 36-call pixel-scored bake-off:
  1. **All-Era / Pre-1800 Long-s (`ſ`) & Polyglot Tiers:** `gemini-4-hydrogen`, `claude-opus-5-5`, and `qwen4-max` (all verified `17/17` exact medial `ſ` + `Labiatæ` + `Carrotte` preservation).
  2. **19th/20th-Century Modern-Orthography Tiers:** `claude-sonnet-5-5` (`~$0.0107/page` in `--batch`) is the proven workhorse for post-1800 non-long-s volumes (preserving Russian tonic acutes, polytonic Greek, French/German diacritics, `toc_entry` small-caps, and two-column indices across Hooker 1848, Broughton 1892, Voltaire 1919, and Tolstoy 1903). By contrast, `claude-haiku-5-5` (`$0.0014/page`) is restricted to pure ASCII/plain-English experiments because it dropped Russian tonic acute accents (`VERIGIN`, `DOUKHOBOR`, `BONDAREF`, `Desyatina`) and collapsed `CONTENTS` into a single `table_row` on the Tolstoy 1903 canary (`docs/LAB_NOTEBOOK.md`). Both Sonnet 5.5 and Haiku 5.5 are excluded from pre-1800 long-s (`ſ`) books.
  3. **Disqualified Primary Wire Models:** `qwen3.7-plus` (`0/4` valid Wire JSON) and `qwen3.8-flash` (`1/4` valid Wire JSON) are excluded from primary extraction routes.
- **Reason:** Live pixel-verified evidence across 9 models × 4 historical archetypes (`docs/LAB_NOTEBOOK.md`) exposed exact orthographic and schema failure boundaries that synthetic benchmarks miss.
- **Rejected:** Treating all Qwen 3/4 or Claude tiers as interchangeable drop-ins across 18th-century vs. 19th-century typography; relying on unverified prompt-only JSON without `output_config.format` on Claude.

---

## D14. Crash-Resumable 50%-Discount Batch Extraction (`--batch`), JBIG2-Free `.originals.pdf` Assembly & Multi-Witness Collation (2026-11-16)
- **Decision:**
  1. `lanterns fetch-archive <id>` automatically streams `*_jp2.zip` + `scandata` (both `.xml` and `scandata.zip`) to assemble a JBIG2-free `<id>.originals.pdf` (`/JPXDecode`, preserving full 400–600 PPI optical camera resolution without 1-bit `/JBIG2Decode` glyph-substitution masks).
  2. Support end-to-end 50%-discounted VLM batch extraction (`lanterns extract --batch`) via `src/lanterns/extraction/batch_runner.py`, encoding images as `quality=94` WebP in bounded `25`-page chunks (`~28 MB`/batch, with recursive HTTP `413` payload auto-halving), checkpointing every `batch_id` atomically in `run_dir/batch_jobs.json` before polling so `--resume` re-attaches without double-submitting, and automatically routing any single failed batch item through `FallbackRouter`. Note: pass `--cost-ceiling-usd 14.00` on 350–400 page books so pre-flight slot reservations (`~$0.0268/page`) submit in one wave while actual billed batch spend settles at `~$3.60–$4.55`.
  3. When a primary scan witness suffers physical page mutilation (e.g., torn fore-edge on UCLA `letterswrittenin00brou` pp. 175–176), never allow VLM contextual guessing; wrap damaged spans in `<unclear>` and collate verbatim against a second archival witness of the identical edition via IIIF slice streaming into Plane-2 `books/<book_id>/corrections/`.
- **Why:** Batch mode cuts 300–600 page extraction spend in half (`0.5x` token multiplier) without sacrificing per-page cross-family rescue or crash durability, while `.originals.pdf` eliminates Archive.org derivative JBIG2 corruption at the source.
- **Rejected:** Single monolithic 300+ page batch POSTs or uncompressed lossless WebP chunks >50 pages (exceeding Anthropic's `256 MB` HTTP cap); re-submitting uncollected batches on process restart; transcribing derivative Text PDFs containing JBIG2 `/SMask` layers.

---

## D15. Hybrid Essay + Correspondence Volumes, Cross-Page Paragraph Coalescing & Multi-Paragraph Footnotes (2026-11-16)
- **Decision:**
  1. **Hybrid Volumes (`Essays and Letters`, `Life, Letters, and Speeches`):** Non-epistolary essays/treatises on Arabic folios keep `SectionKind = "chapter"` (`segmentation.py`: `_classify_h1_slices`, `_is_trailing_prose_date`), pre-TOC signed Roman-folio Prefaces remain `"frontmatter"`, and `"starts_enclosure"` only nests inside an already-active `LetterUnit` (never turning a speech quoted inside an essay into a spurious letter). `books/<book_id>/chapter_map.json` maps exact 1-to-1 Whole-Book chapter boundaries when numbered essays and numbered letters share a single sequence (`I–XXVI`).
  2. **Cross-Page Split Paragraphs (`renderers/html_utils.py`):** Renderers coalesce `continues_to_next` / `continues_from_prev` prose blocks across page turns into a single continuous paragraph with inline `[p. N]` / `<pb n="N"/>` folio milestones and FIFO footnote callout queues.
  3. **Same-Page Multi-Paragraph Footnotes & Shared TOC Legends (`compiler.py`, `validators.py`):** Unlabelled `footnote` blocks (`note_label: null`) immediately following an active footnote on the same page merge as `\n\n` continuation paragraphs of that footnote rather than orphaning, and shared legend symbols (`*`) on `toc_entry` lines do not raise false `unmatched_callout` warnings.
- **Why:** Verified across Voltaire (`334` pp., `88` letters) and Tolstoy (`388` pp., `26` chapters, `10` standalone letters, `100/100` footnotes linked), eliminating orphan multi-paragraph apparatus notes and broken cross-page paragraphs across all 5 output formats without a single content-word heuristic.
- **Rejected:** Forcing non-epistolary essays into `LetterUnit`s; leaving cross-page split sentences fragmented into separate `<p>` tags; dropping or orphaning second paragraphs of long footnotes.

---

## D16. Type-Drift & Float-Plate Coalescing, Roman-Folio Frontmatter Guards & Anthropic Content-Filter Retry (2026-11-16)
- **Decision:**
  1. **Cross-Page Coalescing Across `body <-> editorial` Drift & Float Plates (`epistolary/compiler.py`, `renderers/html_utils.py`):** Treat `illustration`, `caption`, and `margin_note` as boundary float plates when finding cross-page prose continuations, and coalesce `continues_to_next` -> `continues_from_prev` across compatible prose types (`body`, `editorial`), preventing split-sentence paragraphs from breaking at page turns or inline plate inserts.
  2. **Generalizable Biographical-Memoir & Frontmatter Segmentation (`epistolary/segmentation.py`):**
     - Guard `_slice_has_true_epistolary_start` on Roman-folio pre-Arabic sections and restrict opener initiation in `_handle_inactive_block` to `sec_kind in {"chapter", "epistolary_part"}` so signed Roman-folio forewords/introductions (`FOREWORD`, `INTRODUCTION`) after TOC always remain `frontmatter`.
     - Only increment `enclosure_depth` on `starts_enclosure` when `len(active_blocks) > 1`, decrement `enclosure_depth` on `ends_enclosure` prior to checking `has_closer`, ignore editorial-introduced `blockquote`s before `has_dateline` for `has_body`, and extend `_editorial_run_precedes_boundary` to `blockquote`/`verse`.
  3. **Individual Online Letters Fallback & Bare Callout Promotion (`render_letters.py`, `metadata.py`, `normalize.py`):** Fall back to `sender_display → recipient_display` when `header_tagged` is absent (eliminating `— → —`), filter `&c. &c.` noise signatures, parse `LETTER I` numerals, and promote optional-space bare `*`/`**` callouts after terminal punctuation (`; *`).
  4. **Anthropic `content filtering policy` -> `RECITATION` Retry (`anthropic.py`, `router.py`, `batch_runner.py`):** Classify Anthropic HTTP `400` / batch item `"content filtering policy"` rejections as `ProviderErrorKind.RECITATION` and pass `initial_error` into `FallbackRouter.execute_page`, bypassing duplicate un-prefixed retries and going straight to `[L01]` scanline-prefixed rescue.
- **Why:** Verified across all `6` canonical editions (`100.0 / 100.0` rubric, `0` validation errors on `validate-edition --all`), recovering `40/40` content-filtered Nietzsche-Wagner pages cleanly on `claude-sonnet-5-5` at batch rates (`$4.2495` total for `342` pages) without violating `epistolary/` AST complexity caps or prose-word rules.
- **Rejected:** Re-trying raw un-prefixed primary calls on content-filtered batch items; adding book-specific `book_id` branches or prose-keyword matchers inside `src/lanterns/`.

---

## D17. Pre-Flight Corpus Hardening for 17th–19th Century Indian Epistolary Series (2026-11-17)

- **Decision:** Harden ingest, segmentation, fallback routing, and operational safety ahead of the 19-volume *Letters Received* / *English Factories* / *Forrest State Papers* / *Select Letters* digitization waves (verified via `./verify` with `61 passed`, `0` architecture/doctor issues, and `7/7` editions valid):
  1. **Archive.org Ingest & OCR Witness Wiring (`cli.py`, `core/config.py`, `source/archive_org.py`):** `lanterns fetch-archive` automatically syncs `source_pdf` and `source_pdf_sha256` to `books/<book_id>/book.toml`, `BookConfig.resolve_source_pdf_path()` falls back to `sources/<archive_id>/<archive_id>.originals.pdf`, `_pick_jp2_zip` supports `_tif.zip` Google scans, `scandata` handles gzip `.zip` XML, `download_archive_source` downloads `meta["pdf"]` when assembling `.originals.pdf` so the Archive.org OCR witness text layer and rotation flags are preserved (`preserved_ocr_witness=True`), `Image.MAX_IMAGE_PIXELS` is raised to `250_000_000` for large foldouts, and `access_leaves()` aligns 0-based `scandata.xml` vs `_0001.jp2` only when `len(xml_nums) == len(leaf_nums)` so missing `leafNum="0"` cards never drop `_0001.jp2`.
  2. **Leading Marginalia Attachment (`epistolary/segmentation.py`):** Update `_StreamState.pop_leading_margin_notes` and its callers in `_handle_active_block` and `_segment_section_blocks` to detach trailing `margin_note` blocks (e.g., `"1617. May 12."`) on page 1, after same-page closers (`signature`), or across page breaks and attach them to the opening of the new `LetterUnit` instead of leaking into the preceding letter's tail.
  3. **Fallback Tiering, Indic Scripts & Operational Guards (`providers/router.py`, `epistolary/metadata.py`, `renderers/catalog_status.py`, `renderers/doctor.py`, `prompts/extract_epistolary_v1.md`):** Escalate `16384 -> 32768 -> 65536` tokens and attempt the same-tier 2-band horizontal split on `primary_provider` **before** `tier2_same_family_alt` in `FallbackRouter`; recognize `Oriya`, `Sinhala`, and `Modi` scripts in `metadata.py`; add explicit 17th-century orthography (`u/v`, `i/j`, `ye`/`yt`/`ff`, `long-s`), apothecary/weight symbols, and Roman-numeral preservation instructions to `extract_epistolary_v1.md`; guard active `run.lock` and non-terminal `batch_jobs.json` runs in `lanterns gc` unless `--force` is passed; and enforce pricing-table coverage across all `config/profiles.json` routes in `lanterns doctor`.
- **Why:** Pre-flight code audit and independent verification exposed missing `book.toml` source wiring on `fetch-archive`, dropped OCR witness layers when fetching JP2 zips, `margin_note` leakage into preceding letters when a `signature` left `active_blocks` open, and `FallbackRouter` downgrading models before attempting `65k` or 2-band primary retries.
- **Rejected:** Re-extracting any of the 7 already-completed editions (none had leading shoulder-note boundaries affected in `ir/blocks.json`); allowing `lanterns gc` to delete active batch renders without `--force`.


---

## D18. Two-Pass Zero-Trust Corpus Hardening, Multiline Tag Balancing, Backmatter Endnotes & Honest Structural Parity (2026-11-18)

- **Decision:** Harden archival conservation, multiline inline rendering, footnote resolution, epistolary framing, and structural grading across all 25 canonical editions (zero VLM spend, `$0.00` recompile):
  1. **Sacred Human Proof Ledger Guards & Anchor Slice Matching (`epistolary/corrections.py`, `cli.py`):** Guard `_restore_human_snapshot_if_present` and `record_page_proof` so empty `accepted_blocks: []` snapshots never wipe non-empty `PageIR.blocks` (restoring Page 13 of `newton-bentley-1756`); forward active-run `pages` and `witness_dir` through `lanterns intake` -> `intake_proposal` -> `resolve_page_ir_line_wraps` so `page_text_sha256` matches compiled `PageIR`s; and match 120-char Proofing Studio `anchor_text` against `blk.plain_text[:len(anchor) + 40]` on `>360`-char paragraphs.
  2. **Multiline Inline Tag Balancing & Escaped-Tag Gate (`core/tags.py`, `renderers/html_utils.py`, `renderers/render_markdown_tei.py`, `validation/compiled_gate.py`):** Split multiline `tagged_text` with `split_tagged_lines_balanced` (closing/reopening active `StyleSpan`s on each line before `tokenize_tagged_text` instead of raw `.split("\n")`), and gate `&lt;/?(?:i|b|sc|sup|sub|unclear)&gt;` in `_LEAKED_PLACEHOLDER_RE`.
  3. **Archive.org `/page/n` 0-Based Leaf URLs, Image-Only Scan Bundling & Standalone Scan Drawer (`source/archive_org.py`, `renderers/proofing_studio.py`, `renderers/render_book_web.py`, `renderers/render_letters.py`):** Use `/page/n<leaf_idx>_w<width>.jpg` for Archive.org BookReader leaf scaling, restrict `proofing/scans/` to image extensions (`.png`, `.jpg`, `.jpeg`, `.webp`), and embed `<dialog id="scan-drawer">` + `../web/reader.js` in standalone `letters/<slug>.html` pages.
  4. **Post-Letter Commentary Framing, Bottom Datelines & Postscript State (`epistolary/segmentation.py`):** Track `in_postscript`, keep bottom-placed datelines after `has_closer=True`, and in `_attach_framing_side` split on `ornamental_break` only when non-heading `editorial` runs appear on both sides and `blocks[i + 1].type != "heading"` so post-letter `OBSERVATIONS.` headings attach exclusively to `prev_lu.following_editorial_block_ids`.
  5. **Backmatter `"Page N."` Endnotes, Early-Modern Superscripts, Header Fallback & Glued-Digit Stripping (`core/tags.py`, `core/unicode_norm.py`, `epistolary/compiler.py`, `epistolary/metadata.py`):** Backfill opening folio `"1"` before `"2"`, route backmatter `Page N.` endnotes via `folio_to_pnum[N]`, split single- and double-spaced two-column footnotes, link volume-wide `*` legends and sub-footnote symbols (`*`, `||`), recognize `margin_note` callouts, merge unlabelled adjacent cross-page continuations, strip redundant `(1)` prefixes, exclude early-modern accounting/abbreviation superscripts (`d<sup>o</sup>`, `537<sup>li</sup>`, `3/.<sup>s</sup>`, `2<sub>2</sub><sup>1</sup>`, `26<sup>en</sup>`, `a<sup>o</sup>`, `Sig<sup>r</sup>`, `Geograph<sup>l</sup>`, `Estab<sup>t</sup>`, `wors<sup>m</sup>`, `I<sup>s</sup>`, multi-word glosses) from callout extraction, bound `is_binder_signature` length to `<= 12` chars (blocking British Library shelfmarks), strip glued footnote digits (`strip_glued_footnote_digits`), and fall back to `parse_header_metadata_lines` (`_COMMA_DATE_RE` / `_PERIOD_DATE_RE`).
  6. **Witness Dehyphenation, Cross-Page Catchwords, TEI `<closer><dateline>` & Honest `scan_check` Scoping (`extraction/line_wraps.py`, `epistolary/compiler.py`, `renderers/render_markdown_tei.py`, `validation/validators.py`):** Ingest `witness_dir/*.txt` into `count_corpus_word_forms`, recover inline soft-wraps (`_resolve_inline_soft_wraps`), prune ambiguous syllable prefixes from `_PRODUCTIVE_COMPOUND_PREFIXES`, deduplicate cross-page catchwords (`dedup_or_join_cross_page_tagged` with inline tag stripping and a `>=4`-char guard that preserves grammatical `if I had | had none`), emit TEI `<closer><dateline>` on bottom datelines and `<pb/>` before `<list>`/`<table>`, and restrict `footnote_parity` suppression strictly to human-protected pages (`human_only=True`).
- **Why:** Whole-corpus audit across the 25 compiled editions and an independent post-hardening zero-trust review (`Findings A-12`–`A-17` in `docs/ADVERSARIAL_REVIEW_FRAMEWORK.md`) uncovered multiline tag-escape leaks (`145` files -> `0`), Page 13 snapshot wipe, Archive.org URL/JSON bundling bugs, `107` misattached Tipu `OBSERVATIONS.`, `624` unlinked `eic-vol-3` backmatter endnotes, and inflated rubric scores under machine `scan_check` confirmations.
- **Rejected:** Leaving machine `scan_check` IDs to mask unresolved `footnote_parity` warnings; stripping grammatical duplicate prose words (`had | had`) without structural catchword guards; re-extracting already-valid pages (`$0.00` API spend achieved).

---

## D19. Zero-Override Corpus Ablation, Evidence-Driven Superscript/Boundary Cleanup & Two-Model Cross-Era Verification (2026-11-18)

- **Decision:** Replace heuristic gaps exposed by an independent two-agent falsification audit and zero-`letter_map.json` ablation across all `25` canonical editions (`8,949` pages, `2,816` letters, `6,193` footnotes) and validate across `5` uncompiled cross-era volumes (`16th–20th` centuries) with independent `anthropic/claude-sonnet-5-5` vs. `gemini/*` extractions:
  1. **Leader-Dot & Digit-Attached Non-Callout Superscripts (`core/tags.py`):** Include `"m"` (`50<sup>m</sup>`) in `_ORDINAL_SUP_SUFFIXES`, allow leader-dot stems (`....d<sup>o</sup>`) and multi-letter attached contractions (`len(low) >= 2`) in `_is_non_callout_superscript` (eliminating `16` false-positive `unmatched_callout` warnings across `tipu-sultan-select-letters-1811` p. 644 and `eic-letters-received-vol-6-1617b` p. 151 while preserving single-letter callout detection when a footnote is missing).
  2. **Margin-Note Callout Parity (`validation/validators.py`):** Extract `_page_margin_labels` and include same-page `margin_note` labels in `footnote_parity` (matching `compiler.py` line `370` and clearing `eic-letters-received-vol-4-1616` p. 211 `13th<sup>*</sup>`, keeping `footnote_parity` McCabe complexity `<= 18`).
  3. **Complete Cross-Page Prose Dehyphenation & Catchwords (`epistolary/compiler.py`, `extraction/line_wraps.py`):** Expand `CONTINUABLE_PROSE_TYPES` with `postscript`, `list_item`, `index_entry`, and `salutation` (eliminating all `6` remaining lowercase-following cross-page broken hyphens across the 25 books), and support leading running quotation marks (`“ `) and space-separated trailing punctuation on cross-page catchwords (`1165;` / `1165 ;`) in `dedup_or_join_cross_page_tagged`.
  4. **Simplified Bottom-Dateline, Post-Closer Postscript & Plate Handling (`epistolary/segmentation.py`):** Collapse `_is_bottom_dateline` into a single boolean expression (reducing `epistolary/` AST branch count from `303` to `301`), treat `illustration`/`caption` as post-letter boundary bridges in `_editorial_run_precedes_boundary` and `is_post_head`, keep single-block post-closer `body`/`verse` postscripts in the active letter, and prevent `postscript` blocks tagged `starts_new_letter` from splitting active letters.
  5. **Header Recipient Precedence & EIC Metadata Cleanup (`epistolary/metadata.py`, `books/eic-letters-received-vol-{1,3,4,6}-*/letter_map.json`):** Prioritize `hdr_recipient` over `address_block` docketing endorsements in `extract_epistolary_metadata` and clean `83` noisy script-generated `sender_display`/`recipient_display` overrides across the EIC volumes.
- **Why:** Zero-`letter_map.json` ablation proved that primary VLM extraction already achieves `99.8%` letter-start recall (`2,811/2,816`) and `0` validation warnings stem from cross-page seam myopia (`16/22` came from `tags.py` superscript contractions, `1/22` from `validators.py` missing `margin_note` labels, and `5/22` are pixel-verified historical printer errata). Meanwhile, `2-model` verification across `Erasmus`, `Goethe–Zelter`, `Faraday`, `Bapu–Mira`, and `Gandhi–Patel` (`$0.3505` experiment spend) confirmed strong cross-family agreement while surfacing exact model-specific edge cases (such as `bapu-mira-letters` p. 26 unnumbered post-signature `B.` block and `faraday-letters` p. 88 `1813` shoulder note labelling).
- **Rejected:** Suppressing `word<sup>alpha</sup>` callouts unconditionally when no same-page footnote matches (which would hide genuinely dropped alphabetic footnotes); adding a separate VLM page-turn seam pass when deterministic `CONTINUABLE_PROSE_TYPES` + `dedup_or_join_cross_page_tagged` resolved all `6` remaining cross-page broken words at `$0.00` spend.

---

## D20. v3 Phase 0 Security, Provenance Split, Archive.org BookReader & Documentation Consolidation (2026-11-20)

- **Decision:** Superseding `D7`, `D8`, and `D10` on specific operational details:
  1. **Strict CSP, Partial `--pages` Conservation & Relay Lease Enforcement:** Ship `<meta http-equiv="Content-Security-Policy">` on Proofing Studio, merge partial `--pages` runs into existing `ir/pages.json` + `ir/blocks.json` without wiping untouched pages, enforce matching `lease_token` on `P1` relay submissions, and prohibit `P1` overwrites of existing `p1_proofed`/`verified` rows in `workers/proofing_relay/src/index.js`.
  2. **Model & Provenance Hygiene:** Retire `gemini-4-argon` from all active profiles (`gemini/gemini-4-hydrogen` Batch + symmetric `anthropic/claude-sonnet-5-5` fallback, reserving `[L01]` strictly for Tier-2 `RECITATION`/content-filter rescue), split full-page Wire overrides (`books/<id>/scan_overrides/page_NNNN.json`) from append-only audit logs (`books/<id>/audit_checks.jsonl`), and switch Archive.org scan URLs to BookReader `/page/n{k}_w{width}.jpg` (`0` leaf drift across `38/38` IA books vs. broken IIIF `${leaf_index}`).
  3. **Single-Owner Documentation Consolidation (`ops/doctor.py`):** Consolidate `docs/` to 3 core files (`FOUNDER_INTENT_AND_MANIFESTO.md`, unified `ARCHITECTURE.md`, and cross-book R&D-only `LAB_NOTEBOOK.md` + `docs/reference/{gemini-api,claude-api}/`) and forbid `.agents/rules/*.md` so `AGENTS.md` is the sole normative constitution.
- **Why:** Pre-v3 adversarial audit and two-session Gemini 4 Carbon falsification (`$1.2392` lab spend) exposed silent partial-run wipes, IIIF leaf drift on `34/38` IA volumes, mixed `scan_checks/` schemas, and `52%` documentation duplication.
- **Rejected:** DualWitness `3.8-flash + haiku-5-5` (`E3` falsified: `39.24%` block disagreement and correlated paragraph-collapse errors); padding static prompts `> 4,096` tokens (`E4a` falsified); global `2,500` visual-token cap on Sonnet (`E4b` falsified: corrupted `Evêque -> Évêque`).

---

## D21. v3 Phase 1 `<fnref>` vs `<sup>` Wire Separation, Conserved `StructuralUnit` AST & 3-Tier Structure Pass (2026-11-20)

- **Decision:** Superseding `D2` and `D6`:
  1. **Wire `<fnref>` vs `<sup>` + `facsimile_plate`:** Add `<fnref>` for footnote/endnote callouts and `facsimile_plate` for photographed manuscript leaves on `PageExtractWire`; delete `_ABBREV_SUP_PAIRS` and `_ORDINAL_SUP_SUFFIXES` from `core/tags.py` (which mutilated `Robᵗ -> Rob`), retaining a deterministic attested-page-footnote `<sup>` fallback in `epistolary/compiler.py` for legacy runs.
  2. **Dual Conserved `StructuralUnit` + `ChildSpan` AST & `BoundaryAnchor` Rebaser:** Extend `CollectionAST` with `BookSection.units` (`letter`, `letter_excerpt`, `narrative`, `editorial_bridge`, `document`, `apparatus`), `ChildSpan` (`enclosure`, `postscript`, `quoted_verse`, `attachment`, `endorsement`), typed `CorrespondentRef` (`person` | `org`), `language`/`text_direction`/`series_id`/`volume_number`, and `rebase_boundary_anchor` (`(page_number, block_id, bbox_y0, anchor_text_prefix)` surviving single-page re-extractions and block splits/merges). Enforce 100% block conservation across both `sec.items` and `sec.units` (`39/39` Wave-1 books conserved at `$0.00`). Only standalone `unit.kind == "letter"` projects into `ast.letters`, keeping inline *Life & Letters* `letter_excerpt` quotes in-place inside `sec.units`.
  3. **Three-Tier Whole-Book Structure Pass (`extraction/structure_pass.py`):** Implement `lanterns structure` executing Pass 2A (`gemini-3.8-flash` `SectionPlan`), Pass 2B (parallel `60`-page sub-batched `gemini-3.8-flash` compact 10-tuples preserving quoted/`:`-preceded body blocks, Flex -> Standard timeout fallback), and Pass 2C (`gemini-decision-1` `v1beta` `criteria` dict over 450-DPI cropped boundary strips, escalating `< 0.85` confidence to `gemini-4-hydrogen`), keeping `epistolary/` 100% VLM-free (`epistolary: 158 <= 160` branches).
- **Why:** Verified live across `4` adversarial gate volumes (`1,704` pages: `toru-dutt`, `fifty-famous`, `bapu-mira`, `tipu-sultan`) with `0` coverage blockers, `max_call_output_tokens <= 6,440 < 65,536` on 670p `tipu-sultan`, autonomous separation of Mira's Orissa report (`document` `unit-0382`), Socrates's biography (`editorial_bridge` `unit-0007`), and `toru-dutt` p. 348 bereavement quotes (`letter_excerpt`).
- **Rejected:** Monolithic whole-book JSON output (overflows `65,536` output tokens on `400+` letter books); `110`-page unanchored sliding windows; promoting short `letter_excerpt` quotes into standalone `LetterUnit`s.

---

## D22. v3 Phase 2 Standards-Valid TEI P5, EPUB 3.3, DPUB-ARIA, WCAG & `compiled_gate` Enforcement (2026-11-20)

- **Decision:** Overhaul `src/lanterns/renderers/` (`199 <= 230` branches, `McCabe <= 18`) and `src/lanterns/validation/compiled_gate.py`:
  1. **TEI P5 (`tei_all.rng`) + CMIF `<correspDesc>` (`render_markdown_tei.py`):** Enforce strict `<div>` tessellation across `sec.units`, unified `<opener>` and `<closer>` (including post-body `<dateline>` and `<address><addrLine>`), multi-paragraph/signed `<postscript>`, per-line `<lg><l>` (zero `<lb/>` inside `<l>`), `<floatingText type="enclosure">` for `ChildSpan`, multi-paragraph `<note><p>...</p></note>`, dynamic `xml:lang`, and CMIF `<correspDesc>` (`<persName>`/`<orgName>`, `<placeName>`, validated W3C `@when`, zero literal `"Unknown"`).
  2. **EPUB 3.3, DPUB-ARIA 1.1, WCAG & `NAV-011` (`html_utils.py`, `render_epub.py`, `render_book_web.py`, `render_letters.py`):** Emit intermediate float-page `[[FOLIO:...]]` markers in `coalesce_continued_blocks` plus page-turn anchors inside multi-page `<li>` and `<tr>` cells and extract `nav.xhtml` `page-list` in exact DOM order (eliminating `NAV-011` inversions); preserve mid-table `table_role="header"` subheaders inside `<tbody>`; emit `<h2>` apparatus headings, clamped `<h1> -> <h2> -> <h3>` hierarchy, multi-paragraph footnotes, and per-citation `role="doc-backlink"` links (`↩¹`, `↩²`) without invalid `<li role="doc-footnote">`; ship dark-mode-safe `EDITORIAL_EPUB_CSS` (zero hardcoded `#faf7f2`), OPF `schema:accessibility*` + `belongs-to-collection` + RTL `dir="rtl"`/`page-progression-direction="rtl"`, diplomatic roman `.block-verse`/`.block-salutation`/`.block-signature` CSS, and full `sec.units` Web search indexing.
  3. **Automated `compiled_gate.py` Enforcement:** Gate v3 editions (`renderer_version == "3.0.0"`) on deterministic TEI tessellation/`divTop`/`divBottom`, EPUB `NAV-011`/OPF a11y/dark-theme CSS, and HTML WCAG/ARIA rules, plus subprocess `xmllint --relaxng tei_all.rng` and `EPUBCheck v5.1.0` via `--external-tools`.
- **Why:** Verified with **`0` errors and `0` warnings** across all `5` compiled flagship volumes (`toru-dutt-life-letters-1921`, `newton-bentley-1756`, `tipu-sultan-select-letters-1811`, `bapu-mira-letters`, `mill-letters-vol1-1910` — `1,900` pages, `1,402` units, `1,070` letters, `1,639` footnotes) under official `tei_all.rng` and `EPUBCheck v5.1.0`.
- **Rejected:** Regex scraping of `#page-N` IDs inside `renderers/`; hoisting mid-table subheaders into `<thead>`; shipping hardcoded day-theme `#faf7f2` backgrounds inside EPUB `OEBPS/styles.css`.





---

## D23. Hugo Website Content Renderer (`render_hugo.py`, `lanterns render-hugo`) (2026-11-21)

- **Decision:** Add an offline, `$0` renderer that projects a compiled `collection_ast.json` into Hugo content for the new Paper Lanterns website (`library/<collection>/<book_id>/hugo/`): one book `_index.md` and one `<letter.slug>.md` per standalone letter, each with JSON front matter and an empty body, plus `manifest.json` (page list and EPUB/Markdown/TEI download files with byte sizes and SHA-256) for the site's thin sync script.
  1. **Runs, never HTML:** text travels as lines of `{t, c, tag, fn}` style runs built from `tokenize_tagged_text`; the Hugo templates escape every run, so the site needs no `unsafe` Markdown and inserts no raw HTML. Every block keeps `page`, `leaf` (`page_number - 1`, the BookReader `/page/n{leaf}` index of D20) and `bbox` so the letter page can light a block on its scan.
  2. **Attested callouts only:** a `<fnref>` or legacy `<sup>` links to a note only when that block is listed in the note's `referenced_in_block_ids`; anything else stays an unlinked superscript (nothing dropped, nothing guessed).
  3. **Life periods are book state with a citation:** optional `[[life_periods]]` tables in `books/<id>/book.toml` (`label`, ISO `start` inclusive, `end` exclusive, `source`) group letters on the website; each must cite where the dates come from (for Dostoevsky, the book's own Chronological Table, pp. xi-xvi). Malformed, overlapping or unsourced periods and non-ISO letter dates stop the run.
  4. **Separate command:** `lanterns render-hugo <book_id>` reads the compiled library edition; `compile` is unchanged, so existing editions need no recompile.
- **Why:** The website (Hugo, in its own repository) must hold design only and take every word and number from pipeline output, mirroring Akshara's render-then-sync split.
- **Rejected:** Pre-rendering letter HTML into the Markdown body (would need Hugo `unsafe = true` and raw-HTML insertion on the site); copying the EPUB/Markdown/TEI files into `hugo/` (duplicates tracked bytes; the manifest points at them instead); deriving life periods inside the renderer from prose (violates the Conveyor-Belt Law).
