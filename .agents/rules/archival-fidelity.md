---
trigger: always_on
description: Enforces 100% diplomatic archival fidelity across prompts, Unicode normalization, assembly, and proofing.
---

# Archival Diplomatic Fidelity Rule

1. **Never Modernize or Normalize Printed Text:** Preserve period spellings (`shew`, `connexion`, `publick`), compositor errata, `[sic]`, editorial square brackets (`[? 1848]`, `[torn]`), and anonymizing/cipher dashes (`M——`, `Count ***`) verbatim from the scan.
2. **Preserve Historical Letterforms & Diacritics:**
   - Keep lexical ligatures (`æ`, `œ`, `Æ`, `Œ`) and 18th-century medial long-s (`ſ`, `U+017F`). Decompose ONLY presentation ligatures `U+FB00..U+FB06` (`ﬀ`, `ﬁ`, `ﬂ`, `ﬃ`, `ﬄ`) in `core/unicode_norm.py`.
   - Keep exact transliteration conventions (19th-century Hunterian `Rájput` vs. Geneva IAST `Rājput`, `Śiva`, `ṛ`, `ṣ`, `ṃ`, `ḥ`), exact printed European diacritics (`depôt` vs. `dépôt`), and native inline scripts (Devanagari, Bengali, Perso-Arabic, Greek, Cyrillic).
3. **Preserve Source-Order Spatial Layouts:** Never reorder bottom-placed datelines, cascading valedictions, right-aligned signatures, postscripts, superscriptions (`address_block`), or outer-margin shoulder notes (`margin_note`).
4. **Conserve 100% of Non-Furniture Blocks & Human Edits:**
   - Only `running_header`, `folio`, `scanner_residue`, and `noise` are excluded from reading prose; every other `BlockIR` must survive in `CollectionAST`.
   - Human-proofed pages (`p1_proofed` or `verified` in `books/<book_id>/proof_ledger.json`) must never be overwritten by subsequent model runs.
