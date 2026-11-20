# 450-DPI Isolated Crop Diplomatic Verification (v1)

You are examining a high-resolution (450 DPI) isolated crop from a historical correspondence scan. Return a `PageExtractWire` JSON object containing the exact diplomatic reading of the visible block(s).

## Rules
1. Read strictly from the visible ink pixels in this crop; never smooth spellings, digits, or proper nouns toward modern or expected forms.
2. Check closely for:
   - Subtle diacritics (macrons, underdots, acutes, graves, circumflexes, umlauts, breathings) vs paper speckle or bleed-through flecks.
   - Medial long-s (`ſ`) vs `f` or `s`, and lexical ligatures (`æ`, `œ`) vs separate letters.
   - Superscript footnote or variant callout glyphs (`<sup>1</sup>`, `<sup>*</sup>`, `<sup>†</sup>`, `<sup>a</sup>`) and distinct physical footnote lines in apparatus bands (never merge separate footnote lines; leave `note_label: null` if a line has no printed lead marker).
   - Exact digits, punctuation, cipher dashes (`——`), and editorial brackets (`[...]`).
3. Use only the constrained inline tags `<i>`, `<b>`, `<sc>`, `<sup>`, `<sub>`, `<unclear>`.
