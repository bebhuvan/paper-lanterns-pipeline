# Archival Diplomatic Standards, Correspondence Taxonomy & 100-Point Rubric

Paper Lanterns v2 produces **diplomatic archival transcriptions** on par with scholarly critical editions, Standard Ebooks, and Distributed Proofreaders' strictest historical rounds. Every rule in this specification governs `prompts/extract_epistolary_v1.md`, `core/unicode_norm.py`, `epistolary/`, `validation/`, and human proofreading.

---

## 1. Diplomatic Transcription Law ("Type What the Printer Printed")

### 1.1 Spelling, Punctuation & Editorial Brackets
- **Zero Modernization:** Preserve archaic, regional, and idiosyncratic spellings verbatim (`shew`, `connexion`, `publick`, `chuse`, `oeconomy`, `develope`, `Mahomedan`, `ryotwar`, `honour`).
- **Printer Errata & `[sic]`:** If the compositor printed `recieve` or `1884` for `1848`, transcribe the exact printed glyph. Never silently correct errors. Preserve printed `[sic]`, `[?]`, `[? May 1849]`, `[torn]`, `[illegible]`, and editorial interpolations in square brackets verbatim.
- **Redacted Names & Cipher Dashes:** 18th- and 19th-century editions frequently anonymize living persons or transcribe state ciphers as `M——`, `Madame de ***`, `Lord ——`, or `— —`. Preserve exact dash lengths and asterisk counts.
- **Line-End Hyphenation & Catchwords:** Soft end-of-line printer hyphens across line wraps or page breaks (and witness-attested inline soft-wrap breaks like `commo-dities`, `fa-thers`, `pla-nets`) are rejoined diplomatically (`cor-` + `respondence` -> `correspondence`) while genuine lexical compounds (`twenty-one`, `self-same`, `well-known`, `Saint-Germain`, `Ain-i-Akbarī`) keep their hyphens; duplicate 18th/19th-century cross-page catchwords (`Points. Points.`, `*pagoda* *pagoda*`) are deduplicated while structural/short word pairs (`if I had | had none`) remain untouched.

### 1.2 Ligatures, Archaic Letterforms & Unicode Normalization (`unicode_norm.py`)
- **Normalized NFC:** All text is stored in Unicode Canonical Composition (`NFC`) so combining accents never drift from base characters.
- **Decompose ONLY Presentation Ligatures (`U+FB00..U+FB06`):** Typographic metal-sort ligatures `ﬀ` (`ff`), `ﬁ` (`fi`), `ﬂ` (`fl`), `ﬃ` (`ffi`), and `ﬄ` (`ffl`) are decomposed to standard ASCII letters.
- **Strictly Preserve Lexical & Historical Letterforms:**
  - Latin/French/Old English ligatures **`æ` (`U+00E6`), `Æ` (`U+00C6`), `œ` (`U+0153`), `Œ` (`U+0152`)** (`Æt.`, `Cæsar`, `œuvres`, `Œdipus`) MUST survive intact.
  - 18th-century **medial long-s `ſ` (`U+017F`)** (`ſhall`, `neceſſary`, `philoſophy` in Newton–Bentley 1756 and early East India Company records) MUST be transcribed as `ſ` where printed—never flattened to `s` in the archival text! (Web readers may offer an optional visual toggle without mutating the underlying diplomatic text.)

### 1.3 Diacritics, Romanization Schemes & Multilingual Scripts
- **Indian & Orientalist Transliteration:** Preserve the exact convention used by the historical edition without cross-converting schemes:
  - **19th-Century Hunterian / Jonesian:** Acute and circumflex vowel marks (`Rájputána`, `Musalmán`, `Kâshmîr`, `Sháh Jahán`).
  - **Geneva IAST (Sanskrit / Pali / Prakrit):** Macrons and underdots/overdots (`ā`, `ī`, `ū`, `ṛ`, `ṝ`, `ḷ`, `ṅ`, `ñ`, `ṭ`, `ḍ`, `ṇ`, `ś`, `ṣ`, `ṃ`, `ḥ` — e.g., `Śiva`, `Bhagavadgītā`, `ṛṣi`, `ahiṃsā`).
  - **Perso-Arabic / Indo-Persian Romanization:** Ayn `ʿ` (`U+02BF`), hamza `ʾ` (`U+02BE`), `ġ`, `ḫ`, `ṣ`, `ḍ`, `ṭ`, `ẓ`, `ż`, `underline/sub-macron` conventions (`ʿAin-i-Akbarī`, `ṣūbahdār`).
- **European Diacritics:** Preserve French (`é`, `è`, `ê`, `ë`, `à`, `â`, `î`, `ô`, `û`, `ù`, `ç` — including historical un-accented forms like 19th-c. `depôt` when printed without the acute!), German (`ä`, `ö`, `ü`, `Ä`, `Ö`, `Ü`, `ß`), Classical/Humanist Latin (`ē`, `ā`, e-caudata `ę`), Spanish/Italian/Slavic marks, and polytonic Greek breathings/accents (`ἀ`, `ἡ`, `ῶ`).
- **Inline Non-Latin Scripts:** Transcribe embedded **Devanagari, Bengali, Gujarati, Tamil, Perso-Arabic (Urdu / Farsi / Arabic), Greek, Hebrew, and Cyrillic** passages in their native Unicode script blocks—never transliterate or skip non-Latin words (`[Greek text omitted]` is a critical failure!).

### 1.4 Constrained Inline Markup Tags
Only six inline tags are permitted inside `WireBlock.text` and `BlockIR.tagged_text`:
- `<i>...</i>` — Italics (foreign phrases, ship/book titles, emphasis)
- `<b>...</b>` — Boldface
- `<sc>...</sc>` — Small capitals (common in salutations `DEAR SIR`, signatures `<sc>I. Newton</sc>`, and `ÆT.`)
- `<sup>...</sup>` — Superscripts (footnote callouts `<sup>1</sup>`, `<sup>*</sup>`, plus non-callout ordinals/fractions/abbreviations/currency/multi-word glosses such as `M<sup>me</sup>`, `d<sup>o</sup>`, `537<sup>li</sup>`, `3/.<sup>s</sup>`, `2<sub>2</sub><sup>1</sup>`, `26<sup>en</sup>`, `a<sup>o</sup>`, `Geograph<sup>l</sup>`, `Estab<sup>t</sup>`; multiline inline tags are closed/reopened cleanly across `\n` via `split_tagged_lines_balanced`)
- `<sub>...</sub>` — Subscripts (chemical/mathematical notation `H<sub>2</sub>O`, `x<sub>1</sub>`)
- `<unclear>...</unclear>` — Faded, worm-holed, or blot-damaged ink requiring human scan inspection

---

## 2. Historical Printed Correspondence Peculiarities Taxonomy

Across 500+ years of printed letter collections (Humanist Latin epistles, 18th-century pamphlets, Victorian *Life and Letters*, colonial dispatches, and 20th-century collected works), printers evolved specialized conventions. Paper Lanterns v2 represents **every one of these natively via typed `BlockIR` fields—without regex hacks**:

| # | Archival Peculiarity | Real Corpus Examples | Wire / `BlockIR` Representation | Compiler & Renderer Behavior |
| :--- | :--- | :--- | :--- | :--- |
| **1** | **Multiple Short Letters, Dispatches, or Salutation-Free Notes on One Page** | Napoleon–Joseph (`[18.]`, `[19.]`, `[20.]` on p.33 with zero salutation/signature); Gandhi–Patel; wartime telegrams | Each letter's opening block (`letter_header`, `dateline`, or `salutation`) carries `epistolary_transition = "starts_new_letter"` | `segmentation.py` splits cleanly at exact `BlockIR` IDs (`p0014-b0001..b0004`, `p0014-b0005..b0008`) even when salutations or signatures are absent |
| **2** | **Marginal Shoulder Chronology, Topical Side-Notes & Cut-In Notes** | Faraday (`1813. / ÆT. 21.`); Bacon (`exam_09` topical `Ferrara.` / `Mantua.`); Cortés cut-in side-notes | `type = "margin_note"` with true outer-margin or cut-in `bbox` (`x0 < 200` left or `x0 > 750` right) | Preserved in reading order with `.block-margin-note` shoulder styling; never merged mid-word into `body` prose or confused with `running_header` |
| **3** | **Bottom-Placed Datelines, Dual-Calendar Dates & Unusual Opener Order** | Erasmus (*"Antwerp, 5 Jan. 1519"* after body); Dostoevsky Old/New Style (`February 12 [24], 1870.`); pre-salutation superscriptions | `type = "dateline"` / `"address_block"` at its physical position (before `salutation` or after `body`/`signature`) | Kept in **exact source block order**; `metadata.py` records `dateline_position` (`top`, `bottom`, `inline`, `absent`) while preserving dual-calendar brackets verbatim |
| **4** | **Cascading Multi-Line Polite Valedictions & Co-Signatures** | Newton–Bentley 1756 (*"I am, / Sir, / Your most humble / Servant,"*); EIC factory letters signed by multiple factors | `type = "valediction"` / `"signature"`, `alignment = "cascading"` or `"right"`, `line_breaks_preserved = True` | Rendered with `<br />` and stepped indentation (`.align-cascading`) matching letterpress layout |
| **5** | **Postscripts (`P.S.`), Superscriptions, Postmarks & Endorsements** | Newton (`For Mr. BENTLEY, at the Palace at Worceſter.`); Gandhi–Mirabehn (`Unrevised*`, `27-12-’26 (Post Mark)`); EIC post-signature `P.S.` | `type = "postscript"` and `type = "address_block"` (`line_breaks_preserved = True`) | Attached to the active `LetterUnit` (whether printed above `salutation` or after `signature`)—never triggers a false split |
| **6** | **In-Body & Post-Signature Enclosures (Letter/Document Inside a Letter)** | Goethe–Zelter (`GOETHE.` followed immediately by centered `ENCLOSURE.`); diplomatic dispatches enclosing reports | Opening block of enclosed text sets `epistolary_transition = "starts_enclosure"`; closing block sets `"ends_enclosure"` | Kept **inside** the parent `LetterUnit` (`metadata.has_enclosure = True`) even when printed *after* the parent letter's `signature` |
| **7** | **Interleaved Biography & Editorial Bridges (*Life and Letters*)** | Spedding's *Letters and Life of Francis Bacon*; Bence Jones's *Faraday*; Modeste Tchaikovsky | Biographer narrative classified as `type = "editorial"` | Emitted as `SectionItem(kind="blocks")` in `BookSection` AND referenced via `preceding_editorial_block_ids` / `following_editorial_block_ids` on adjacent `LetterUnit`s |
| **8** | **Two-Tier Apparatus, Backmatter Endnotes & Multi-Script Salutations** | Erasmus (`a, b, c` + `1, 2, 3`); EIC backmatter `"Page N."` endnotes & two-column footnotes; Vivekananda Devanagari śloka & `*` legend; Tipu `*` / `||` sub-notes | `type = "footnote"`, exact `note_label`, `continues_from_prev = True` on split notes; native Unicode scripts preserved | Stitched across page breaks (with `"Page N."` endnote routing, side-by-side splitting, and sub-footnote linking) into unique `fn-pNNNN-<idx>` IDs; projected into both `BookSection.footnote_ids` and `LetterUnit.footnote_ids` |
| **9** | **Centered Letter Numbers vs. Bottom Folios, Gathering Marks & Catchwords** | Gandhi (`44`, `45` centered letter headers vs. bottom folio `49` & gathering `G.C.–4`); Newton gathering `C 2` + catchword `LET-` | Centered letter numbers = `letter_header` (`starts_new_letter`); bottom folios, gathering marks (`C 2`), and catchwords = `folio` / `noise` | Furniture (`folio`, `running_header`, `noise`) cleanly excluded from prose so catchwords/signatures never fuse into `IS. NEWTON.` or body text |
| **10** | **Woodcut Drop-Caps, Three-Form Lacuna Brackets & JBIG2/MRC Blots** | EIC 4-line woodcut `M` + `R. FEMELL` (`MR. FEMELL`), `[torn away]`, `[a bay on the East]`, `[        ]`; 86.5% JBIG2/MRC derivative risk (Bacon p.45 blots) | Rejoin drop-cap initial (`MR. FEMELL`); preserve lacuna whitespace/brackets verbatim; mark blot-obliterated words `<unclear>` | `triage-pdf` & `archive_org.py` flag `/JBIG2Decode` & MRC blot risk upfront and prefer `_jp2.zip` camera scans (`prefer_jp2=True`) |
| **11** | **Unchaptered Epistolary Volumes & Unheadered Informal Letters** | Goethe–Zelter (pure letter sequence, zero chapters); Faraday inline openings (`‘Dear A——,`); Vivekananda informal notes | Absent `heading` / `letter_header` blocks | Compiler creates clean `epistolary_part` section(s) automatically and synthesizes titles from sender/recipient/dateline/incipit without dropping blocks |
| **12** | **Calendar / Register of Letters in Frontmatter or Backmatter** | Chronological letter registers & correspondent indexes in scholarly editions | `table_caption` + `table_row` (`cells`) or `toc_entry` / `index_entry` | Preserved in `frontmatter` / `index` `BookSection`s as structured HTML, EPUB 3, Markdown, and TEI tables/lists |

---

## 3. The 100-Point Epistolary Edition Quality Rubric (`rubric.py`)

Every compiled edition is scored deterministically out of **100.0 points** (`RubricBreakdown` in `lanterns.core.models`) across five weighted archival dimensions:

| Dimension | Max Points | What Is Measured & Deducted |
| :--- | :---: | :--- |
| **1. Text & Diacritic Fidelity** (`text_and_diacritic_fidelity`) | **30** | Zero `U+FFFD` replacement characters, C0 control chars, Private Use Area bytes, or leaked `[L01]` prefixes (any occurrence = `blocker` & `-30`); deductions for NFD uncomposed marks, cross-script homoglyphs, intra-book proper-noun diacritic drift, and high unresolved `<unclear>` density. |
| **2. Epistolary Structure & Layout Preservation** (`epistolary_structure_and_layout`) | **25** | Boundary confidence across `LetterUnit`s (`high` / `adjudicated` / `human_mapped` vs. `ambiguous`), preservation of `alignment` & `line_breaks_preserved` on openers/closers, clean handling of enclosures (`has_enclosure`), editorial framing integrity, and zero `epistolary_boundary_anomalies` (e.g. un-enclosed double signatures or runaway `>25`-page letters). |
| **3. Apparatus & Bidirectional Footnote Integrity** (`apparatus_and_footnote_integrity`) | **20** | 1-to-1 parity between inline `<sup>` callouts and `FootnoteItem` targets (`-3` per `unmatched_callout`, `-2` per `unlinked_footnote` / `is_orphan`), cross-page & backmatter footnote stitching, and verified dual projection (`BookSection.footnote_ids` + `LetterUnit.footnote_ids`); never silenced by machine `scan_check` confirmations (`human_only=True`). Full `20/20` if volume has zero footnotes and zero stray callouts. |
| **4. Completeness & 100% Page/Block Coverage** (`completeness_and_page_coverage`) | **15** | Mathematical `source_block_coverage` conservation: every non-furniture `BlockIR` on every extracted page MUST appear in `CollectionAST` (any dropped block = `blocker` & `-15`); zero `failed_closed` / missing pages (any unavailable page = `blocker`); clean separation of `running_header` / `folio` (`furniture_leak` deductions). |
| **5. Compiled Multi-Format Validity** (`compiled_output_validity`) | **10** | `validate_compiled_edition()` verification of Whole-Book Web HTML, all `letters/<slug>.{html,md,json}` + `letters/index.json`, valid EPUB 3.3 (`content.opf`, `nav.xhtml`, XHTML chapters, internal `#fn`/`#fnref` links), Scholarly Markdown, well-formed TEI P5 XML with unique `xml:id`s & `<correspDesc>`, and zero-`innerHTML` Proofing Studio. |
| **TOTAL** | **100** | Stored in `library/<collection>/<book_id>/rubric_score.json` and `CATALOG_STATUS.md`. |

---

## 4. Honest Four-Tier Provenance Gate

`score_collection_rubric()` assigns one of four transparent provenance tiers—never inflating machine verification into human certification:

1. **`blocked`** — Any `severity == "blocker"` finding (dropped block, `failed_closed` page, `U+FFFD`/control char, broken EPUB/TEI/HTML link, or `innerHTML` security violation) OR `total_score < 70.0`. Cannot be promoted as a clean shelf edition.
2. **`community`** — `blocker_count == 0` and `total_score >= 70.0`, with one or more open `warning` hotspots awaiting machine re-read or volunteer inspection. Published immediately with co-present page scans and one-click Proofing Studio links.
3. **`machine_checked`** — `blocker_count == 0`, `hotspot_count == 0` (zero unresolved warnings), and `total_score >= 95.0`. Machine-verified across primary extraction, validators, and hotspot re-reads; explicitly states how many pages have been checked by humans (`0` until `P1 -> P2` begins).
4. **`source_certified`** — `blocker_count == 0`, `hotspot_count == 0`, `total_score >= 96.0`, AND **100% of extracted pages** carry `status == "verified"` in `books/<book_id>/proof_ledger.json` signed off by **two distinct human reviewers (`p2_verifier_id != p1_reviewer_id`)** against matching `source_page_sha256` and `page_text_sha256`.
