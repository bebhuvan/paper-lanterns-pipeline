# Epistolary Boundary & Enclosure Arbiter (v1)

Inspect the adjacent correspondence blocks or page-turn image strip and adjudicate the exact structural relationship strictly from visible printed typography, layout, and punctuation.

## Rules
1. Do not rely on language-specific greeting keywords; judge from typographic hierarchy, spacing, ornamental rules, numbering, indentation, and speaker continuity.
2. Distinguish four structural cases:
   - `starts_new_letter`: A distinct top-level letter, telegram, or dispatch begins at the candidate block.
   - `continues_same_letter`: The candidate block continues the active letter (including bottom-placed datelines, postscripts after a signature, address superscriptions, or page-turn continuations across running headers).
   - `enclosure_inside_letter`: The candidate block is an enclosed or quoted inner letter/document framed within the outer correspondent's letter.
   - `editorial_interlude`: The candidate block is third-person biographical or editorial commentary between letters.
3. Return a single JSON object:
```json
{
  "verdict": "starts_new_letter | continues_same_letter | enclosure_inside_letter | editorial_interlude",
  "confidence": 0.95,
  "rationale": "Concise visual/typographic evidence from the scan."
}
```
