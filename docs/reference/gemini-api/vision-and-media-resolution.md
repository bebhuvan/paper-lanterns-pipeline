# Vision, Media Resolution, Prefix Ordering & Bounding Boxes

## 1. Media Resolution Token Budgets (Gemini 4 & Gemini 3)

The `media_resolution` parameter governs the maximum visual token budget allocated per page scan or crop:

| Resolution Level | Enum (`PartMediaResolutionLevel`) | Interactions API Value | Image Tokens (Gemini 3 & 4) | PDF Page Tokens | Archival Use Case in Paper Lanterns |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Ultra High** | `MEDIA_RESOLUTION_ULTRA_HIGH` | `"ultra_high"` | **2,240** | N/A *(render to PNG!)* | **Required** for `polyglot_epistolary`, `indic_and_oriental_epistolary`, `archaic_18c_epistolary`, 6-pt footnotes, faded diacritics, long-s `ſ`, and 450-DPI hotspot crops |
| **High** | `MEDIA_RESOLUTION_HIGH` | `"high"` | **1,120** | 1,120 + native text | Standard crisp 19th/20th-c. English typography (`english_epistolary`) |
| **Medium** | `MEDIA_RESOLUTION_MEDIUM` | `"medium"` | **560** | 560 + native text | Coarse layout/orientation preflight only |
| **Low** | `MEDIA_RESOLUTION_LOW` | `"low"` | **280** | 280 + native text | Blank/tissue-guard preflight only |
| **Unspecified** | `MEDIA_RESOLUTION_UNSPECIFIED` | `"unspecified"` | 1,120 | 560 | Avoid leaving unspecified; declare profile resolution explicitly |

---

## 2. CRITICAL SDK TRAP: `MEDIA_RESOLUTION_ULTRA_HIGH` Is Per-Part Only

In `google-genai==3.3.0`, there are **two distinct enums**:

1. **Global config enum (`types.MediaResolution`):**
   - Values: `MEDIA_RESOLUTION_UNSPECIFIED`, `MEDIA_RESOLUTION_LOW`, `MEDIA_RESOLUTION_MEDIUM`, `MEDIA_RESOLUTION_HIGH`.
   - **Does NOT contain `MEDIA_RESOLUTION_ULTRA_HIGH`!** Passing `MEDIA_RESOLUTION_ULTRA_HIGH` on `GenerateContentConfig(media_resolution=...)` raises a validation error.
2. **Per-content-item enum (`types.PartMediaResolutionLevel`):**
   - Values: `MEDIA_RESOLUTION_UNSPECIFIED`, `MEDIA_RESOLUTION_LOW`, `MEDIA_RESOLUTION_MEDIUM`, `MEDIA_RESOLUTION_HIGH`, **`MEDIA_RESOLUTION_ULTRA_HIGH`**.
   - Supported on **Gemini 4 and Gemini 3** models by attaching `media_resolution` directly to each `Part` (or `"resolution": "ultra_high"` on the image object in `client.interactions.create`).

```python
from google.genai import types

image_part = types.Part(
    inline_data=types.Blob(data=png_bytes, mime_type="image/png"),
    media_resolution=types.PartMediaResolution(
        level=types.PartMediaResolutionLevel.MEDIA_RESOLUTION_ULTRA_HIGH
    ),
)
```

> [!TIP]
> Note also from the token table above that native PDF inputs cap at `high` (1,120 tokens) and do not support `ultra_high` (2,240 tokens). Rendering archival PDF pages via PyMuPDF at **300–400 DPI PNG** (or extracting raw Archive.org `_jp2.zip` camera scans) and feeding them as `image/png` parts at `MEDIA_RESOLUTION_ULTRA_HIGH` doubles visual token fidelity for tiny 6-point footnotes, IAST underdots (`ṛ`, `ṣ`, `ṃ`), French circumflexes (`depôt`), and 18th-century medial long-s (`ſ`).

---

## 3. Prefix-Ordered Parts for Automatic Cache Hits

Always order multimodal request parts deterministically:

1. **Index `0`:** Static system/extraction prompt (`prompts/extract_epistolary_v1.md`) + any static book-level observational context.
2. **Index `1..N`:** Variable page scan image(s) (`page_0014.png`).
3. **Final Index (optional):** Dynamic per-page suffix (e.g., scanline prefix hint or page number context).

Putting static instructions **before** image bytes allows Gemini's prefix cache to reuse prefix computations across hundreds of pages in a volume.

---

## 4. Normalized `0..1000` Bounding Boxes & Reading Order

- Gemini vision models natively reason over normalized coordinates in the **`[0, 1000]` integer grid** regardless of source PNG pixel dimensions.
- While Gemini's generic object-detection examples often emit `box_2d` as `[ymin, xmin, ymax, xmax]`, Paper Lanterns' `page_extract_wire.schema.json` standardizes `bbox` as **`[x0, y0, x1, y1]`** (`0 <= x0 < x1 <= 1000`, `0 <= y0 < y1 <= 1000`) and normalizes/clamps coordinates in `extraction/normalize.py`.
- Outer-margin shoulder notes (`margin_note`, e.g., `1831. Æt. 39.`) preserve their true horizontal `bbox` (`x0 < 200` on verso outer margins, `x0 > 750` on recto outer margins) so Proofing Studio overlays highlight the exact marginal shoulder region without scrambling single-column right-aligned datelines (`alignment="right"`).

---

## 5. Gemini Files API (`client.files`)

- Supports uploading files up to **2 GB** (`MAX_FILES_API_BYTES = 2 * 1024**3`) when submitting large Batch JSONL files (`> 16 MB` inline limit) or whole-volume PDF surveys.
- Uploaded files are temporary staging resources; canonical provenance always rests on local `source_page_sha256` hashes in `workspace/runs/<book_id>/<run_id>/`.
