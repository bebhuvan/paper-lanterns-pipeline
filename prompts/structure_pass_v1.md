# Diplomatic Epistolary Volume Structure Pass (Pass 2A & Pass 2B)

You are an archival bibliographer segmenting a digitized historical correspondence edition into its exact printed structural hierarchy. Never invent text, normalize historical orthography, or paraphrase metadata. Refer exclusively to the supplied `block_id` tokens in reading order.

## Pass 2A — Volume Section Partition (`sections`)

Partition the complete volume into contiguous, non-overlapping top-level sections covering every block from the first block to the last block:
- Allowed `kind` values: `"frontmatter"`, `"chapter"`, `"epistolary_part"`, `"appendix"`, `"index"`, `"backmatter"`.
- `"frontmatter"`: title page, half-title, copyright/imprint, dedication, table of contents (`toc_entry`), preface, biographical introduction preceding letters.
- `"chapter"`: numbered or named chapter, memoir chapter, historical period, or thematic book division (`heading` with `heading_level=1`).
- `"epistolary_part"`: main body of correspondence in unchaptered letter collections, or numbered epistolary parts/books.
- `"appendix"`: documentary appendices, supplementary letters, or historical documents at the rear before notes/index.
- `"index"`: alphabetical backmatter index (`index_entry`) or glossary/errata.
- `"backmatter"`: colophon, publisher's advertisements, or rear matter after the index.
- Every `start_block_id` and `end_block_id` must match an exact block ID from the volume skeleton in printed sequence order.

## Pass 2B — Section Structural Units & Nested Enclosures (`units`)

Inside a single section, partition every block exhaustively and contiguously in printed order into compact 10-element tuples:
`[kind, start_block_id, end_block_id, label, senders, recipients, place, date_raw, date_iso, children]`

1. **`kind` Taxonomy:**
   - `"letter"`: a standalone letter (typically opening with a `letter_header`, `dateline`, `salutation`, or clear editorial header announcing correspondence, through its closing `signature`/`postscript`/`address_block` or trailing body before the next item).
   - `"letter_excerpt"`: a short quoted letter extract embedded inline within biographical narrative (`Life & Letters`), often introduced by a colon or narrative bridge and delimited by quotation marks or indentation.
   - `"narrative"`: continuous biographical, historical, or literary narration by an editor/biographer that is not itself a letter.
   - `"editorial_bridge"`: brief editorial note, connective paragraph, or headnote/endnote introducing or linking letters.
   - `"document"`: non-epistolary prose or state document (treaty, essay, article, memorandum, proclamation, diary entry, speech, report) printed alongside letters — never misclassify non-epistolary essays or reports as `"letter"`.
   - `"apparatus"`: table of contents, index entries, illustration/facsimile plates outside letters, errata, or section headings standing alone as front/back apparatus.

2. **One-Level Mid-Letter Enclosures (`children`):**
   - When an outer letter encloses a secondary document, copied letter, petition, or substantial verse insert inside its flow (whether or not marked `starts_enclosure`), keep the outer letter as ONE single `"letter"` unit and emit a `children` entry:
     `[child_kind, start_block_id, end_block_id, title]`
     where `child_kind` is `"enclosure"`, `"letter_excerpt"`, or `"verse_insert"`.
   - Child spans must lie strictly within `[start_block_id .. end_block_id]` of their parent unit and must not overlap.

3. **Correspondents & Diplomatic Dates:**
   - `senders` and `recipients`: arrays of `[name, role]` pairs where `role` is `"person"` or `"org"` (empty array `[]` if absent/unknown; never emit `"Unknown"`).
   - `place`: printed origin place if explicitly printed (else `null`).
   - `date_raw`: exact printed date string (else `null`).
   - `date_iso`: strict ISO-8601 `"YYYY-MM-DD"`, `"YYYY-MM"`, or `"YYYY"` only when unambiguous (including clear section-year context); otherwise `null`.
   - `label`: printed letter/item number or heading label (e.g. `"Letter XII"`, `"No. 383"`), else `null`.
