# Structured Output & Decisions API (`gemini-decision-1`)

## 1. JSON Schema Structured Output

Both `client.interactions.create` and `client.models.generate_content` enforce constrained decoding against JSON Schema Draft 2020-12 contracts (`schemas/page_extract_wire.schema.json`).

### 1.1 Schema Sanitization Rules (`gemini_clean_schema`)
Before passing a repository `.schema.json` file to Gemini SDK transport:
1. Strip recursive `"$schema"` and `"$id"` meta-keywords so strict OpenAPI/JSON-Schema transport validators never reject Draft 2020-12 URIs.
2. Keep schemas **lean and observation-focused** (`PageExtractWire`):
   - Emit constrained inline tags (`<i>`, `<b>`, `<sc>`, `<sup>`, `<sub>`, `<unclear>`) inside `text` rather than asking the VLM to count 0-based Unicode character offsets.
   - Keep `additionalProperties: false` and explicit `required` arrays (`["printed_folio", "warning_codes", "blocks"]` at root; `["type", "bbox", "text"]` on each block).
3. Always re-validate the parsed JSON response locally with `jsonschema.Draft202012Validator` and Pydantic (`PageExtractWire.model_validate`) before converting to `PageIR`.

### 1.2 Structured Output Syntax Comparison

**Interactions API (`v1` / GA):**
```python
interaction = client.interactions.create(
    model="gemini-4-hydrogen",
    store=False,
    input=parts,
    response_format={
        "type": "text",
        "mime_type": "application/json",
        "schema": clean_schema,
    },
)
wire = PageExtractWire.model_validate_json(interaction.output_text)
```

**`generate_content` API:**
```python
response = client.models.generate_content(
    model="gemini-4-hydrogen",
    contents=contents,
    config=types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=clean_schema,
    ),
)
wire = PageExtractWire.model_validate_json(response.text)
```

---

## 2. Decisions API (`client.decisions.create` with `gemini-decision-1`)

When pipeline code needs a **calibrated probability or discrete classification to branch on**—such as judging whether an ambiguous mid-page break starts a new top-level letter vs. an enclosed quoted letter, or scoring whether a flagged crop matches the diplomatic transcription—generating prose or JSON tokens adds latency and cost.

The **Decisions API** (`v1beta`, requires `google-genai >= 3.2.0`) evaluates a map of typed questions in **one parallel forward pass** using `gemini-decision-1`:
- **Cost:** `$0.10` per 1M input tokens, **`$0.00` output cost** (`total_output_tokens == 0` always).
- **Isolation:** Questions in the `questions` map run in parallel against the shared `input` and cannot influence each other.

### 2.1 Question Types

| Question Type | Purpose | Returned Fields | Notes |
| :--- | :--- | :--- | :--- |
| `"noul"` | Calibrated Yes/No probability | `noul` (`0.01` to `0.99`), `confidence` | Bounded strictly within `[0.01, 0.99]`; never compare `> 0.99` |
| `"choice"` | Single selection from fixed options | `choice`, `probabilities` (sums to `1.0`), `confidence` | Option keys are UTF-8 byte-sorted before evaluation (order-invariant) |
| `"score"` | Ordered rubric rating | `score`, `probabilities`, `legend`, `confidence` | Ideal for calibrated quality/legibility grading |

### 2.2 Epistolary Boundary & Verification Pattern (`v1beta`)

```python
from google import genai

decision_client = genai.Client(
    api_key=api_key,
    http_options={"api_version": "v1beta"},
)

result = decision_client.decisions.create(
    model="gemini-decision-1",
    input=shared_multimodal_or_text_context,
    questions={
        "is_new_top_level_letter": {
            "type": "noul",
            "instructions": (
                "Does candidate block B begin a new top-level letter in the volume, "
                "rather than a quoted enclosure or continuation of the current letter?"
            ),
            "criteria": {
                "true_description": "A distinct subsequent letter in the collection sequence begins at block B.",
                "false_description": "Block B is an inner quoted letter (enclosure), postscript, bottom dateline, or continuation of the active letter.",
            },
        },
        "transition_kind": {
            "type": "choice",
            "instructions": "Classify the epistolary transition at candidate block B.",
            "criteria": {
                "starts_new_letter": "Begins a new top-level letter, telegram, or note.",
                "starts_enclosure": "Begins a letter or document quoted/enclosed inside the active letter.",
                "ends_enclosure": "Returns from an enclosed document to the outer letter.",
                "none": "Continues the current letter (including postscripts and bottom datelines).",
            },
        },
    },
)

ans = result.answers["is_new_top_level_letter"]
# Escalate to gemini-4-hydrogen full VLM arbiter only when confidence is low!
if ans.confidence >= 0.85:
    starts_new = ans.noul >= 0.50
```
