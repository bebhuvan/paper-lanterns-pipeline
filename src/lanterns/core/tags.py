"""Deterministic parser and safe HTML/XML helpers for constrained inline tags.

Allowed inline tags: `<i>`, `<b>`, `<sc>`, `<fnref>`, `<sup>`, `<sub>`, `<unclear>`
(synonyms `<em>` -> `i` and `<strong>` -> `b` are canonicalized on parse).
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any, Literal, cast

from lanterns.core.models import StyleSpan
from lanterns.core.unicode_norm import normalize_diplomatic_text

TagLiteral = Literal["i", "b", "sc", "fnref", "sup", "sub", "unclear"]

RAW_TAG_MAP: dict[str, TagLiteral] = {
    "i": "i",
    "em": "i",
    "b": "b",
    "strong": "b",
    "sc": "sc",
    "fnref": "fnref",
    "sup": "sup",
    "sub": "sub",
    "unclear": "unclear",
}

_TAG_RANK: dict[str, int] = {
    "b": 1,
    "i": 2,
    "sc": 3,
    "fnref": 4,
    "sup": 5,
    "sub": 6,
    "unclear": 7,
}

_TAG_TOKEN_RE = re.compile(r"<(/?)(i|em|b|strong|sc|fnref|sup|sub|unclear)\s*>", re.IGNORECASE)
_ANY_ANGLE_TAG_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9_-]*)(?:\s[^>]*)?>")
_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_SMALL_CAPS_SPAN_RE = re.compile(r"<span\b[^>]*(?:small[-_ ]?caps|\bsc\b)[^>]*>([\s\S]*?)</span>", re.IGNORECASE)
_BARE_SPAN_TAG_RE = re.compile(r"</?span(?:\s[^>]*)?>", re.IGNORECASE)
SYMBOL_NOTE_LABELS = frozenset({"*", "**", "†", "‡", "§", "‖", "||", "¶"})
_LEADING_NOTE_SUP_RE = re.compile(r"^\s*<(?:sup|fnref)>\s*([^<]+?)\s*</(?:sup|fnref)>\s*")


def escape_html(text: str, *, quote: bool = True) -> str:
    """Safely escape text for HTML5/XHTML output."""
    return html.escape(text or "", quote=quote)


def escape_xml(text: str, *, quote: bool = True) -> str:
    """Safely escape text for strict XML/TEI P5 output."""
    escaped = html.escape(text or "", quote=quote)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", escaped)


def validate_constrained_tags(raw_text: str) -> list[str]:
    """Return any disallowed XML/HTML tag names present in `raw_text`."""
    bad: list[str] = []
    for match in _ANY_ANGLE_TAG_RE.finditer(raw_text or ""):
        name = match.group(1).lower()
        if name not in RAW_TAG_MAP and name != "br":
            bad.append(name)
    return sorted(set(bad))


def _preprocess_tag_synonyms(raw_text: str) -> str:
    source = normalize_diplomatic_text(raw_text or "")
    if "<" not in source:
        return source
    source = _HTML_BR_RE.sub("\n", source)
    source = _SMALL_CAPS_SPAN_RE.sub(r"<sc>\1</sc>", source)
    return _BARE_SPAN_TAG_RE.sub("", source)


def parse_tagged_text(raw_text: str) -> tuple[str, str, list[StyleSpan]]:
    """Parse constrained inline tags into `(canonical_tagged_text, plain_text, spans)`."""
    source = _preprocess_tag_synonyms(raw_text)
    plain_parts: list[str] = []
    open_stack: list[tuple[TagLiteral, int]] = []
    raw_spans: list[StyleSpan] = []

    cursor = 0
    plain_len = 0
    for match in _TAG_TOKEN_RE.finditer(source):
        literal = source[cursor : match.start()]
        if literal:
            plain_parts.append(literal)
            plain_len += len(literal)
        is_closing = bool(match.group(1))
        tag = RAW_TAG_MAP[match.group(2).lower()]
        if not is_closing:
            open_stack.append((tag, plain_len))
        else:
            for idx in range(len(open_stack) - 1, -1, -1):
                if open_stack[idx][0] == tag:
                    _, start_pos = open_stack.pop(idx)
                    if plain_len > start_pos:
                        raw_spans.append(StyleSpan(tag=tag, start=start_pos, end=plain_len))
                    break
        cursor = match.end()

    tail = source[cursor:]
    if tail:
        plain_parts.append(tail)
        plain_len += len(tail)

    plain_text = "".join(plain_parts)
    while open_stack:
        tag, start_pos = open_stack.pop()
        if plain_len > start_pos:
            raw_spans.append(StyleSpan(tag=tag, start=start_pos, end=plain_len))

    deduped = {(s.tag, s.start, s.end): s for s in raw_spans}
    ordered = sorted(
        deduped.values(),
        key=lambda s: (s.start, -(s.end - s.start), _TAG_RANK.get(s.tag, 99)),
    )
    non_crossing = _filter_non_crossing_spans(ordered, len(plain_text))
    canonical_tagged = serialize_tagged_text(plain_text, non_crossing)
    return canonical_tagged, plain_text, non_crossing


def parse_inline_tags(raw_text: str) -> tuple[str, list[StyleSpan]]:
    """Parse constrained inline tags into `(plain_text, list[StyleSpan])`."""
    _, plain_text, spans = parse_tagged_text(raw_text)
    return plain_text, spans


def _coerce_span_tuple(item: StyleSpan | dict[str, Any], text_len: int) -> tuple[TagLiteral, int, int] | None:
    if isinstance(item, StyleSpan):
        raw_tag, start, end = item.tag, item.start, item.end
    else:
        raw_tag = str(item.get("tag") or "")
        start = int(item.get("start") or 0)
        end = int(item.get("end") or 0)
    tag = RAW_TAG_MAP.get(raw_tag.lower())
    if tag is None:
        return None
    clamped_start = max(0, min(text_len, start))
    clamped_end = max(0, min(text_len, end))
    if clamped_end <= clamped_start:
        return None
    return tag, clamped_start, clamped_end


def _filter_non_crossing_spans(spans: list[StyleSpan], text_len: int) -> list[StyleSpan]:
    accepted: list[StyleSpan] = []
    seen: set[tuple[str, int, int]] = set()
    for span in spans:
        coerced = _coerce_span_tuple(span, text_len)
        if coerced is None:
            continue
        tag, start, end = coerced
        if (tag, start, end) in seen:
            continue
        if any((start < a.start < end < a.end) or (a.start < start < a.end < end) for a in accepted):
            continue
        seen.add((tag, start, end))
        accepted.append(StyleSpan(tag=cast(TagLiteral, tag), start=start, end=end))
    return accepted


def serialize_tagged_text(plain_text: str, spans: list[StyleSpan | dict[str, Any]]) -> str:
    """Reconstruct balanced canonical tagged text from `plain_text` and non-crossing spans."""
    text = normalize_diplomatic_text(plain_text)
    if not spans:
        return text
    coerced_spans: list[StyleSpan] = []
    for item in spans:
        tup = _coerce_span_tuple(item, len(text))
        if tup is not None:
            coerced_spans.append(StyleSpan(tag=tup[0], start=tup[1], end=tup[2]))
    valid = _filter_non_crossing_spans(
        sorted(coerced_spans, key=lambda s: (s.start, -(s.end - s.start), _TAG_RANK.get(s.tag, 99))),
        len(text),
    )
    opens: dict[int, list[tuple[int, int, str]]] = {}
    closes: dict[int, list[tuple[int, int, str]]] = {}
    for span in valid:
        rank = _TAG_RANK.get(span.tag, 99)
        opens.setdefault(span.start, []).append((-(span.end - span.start), rank, span.tag))
        closes.setdefault(span.end, []).append(((span.end - span.start), -rank, span.tag))

    out: list[str] = []
    for pos in range(len(text) + 1):
        if pos in closes:
            for _, _, tag in sorted(closes[pos]):
                out.append(f"</{tag}>")
        if pos in opens:
            for _, _, tag in sorted(opens[pos]):
                out.append(f"<{tag}>")
        if pos < len(text):
            out.append(text[pos])
    return "".join(out)


def split_tagged_lines_balanced(tagged_text: str) -> list[str]:
    """Split `tagged_text` on `\\n`, closing and reopening active inline tags on each line."""
    if "\n" not in tagged_text:
        return [tagged_text]
    _, plain_text, spans = parse_tagged_text(tagged_text)
    lines: list[str] = []
    pos = 0
    for line_plain in plain_text.split("\n"):
        line_end = pos + len(line_plain)
        if not line_plain.strip():
            lines.append(line_plain)
        else:
            line_spans = [
                StyleSpan(tag=s.tag, start=max(s.start, pos) - pos, end=min(s.end, line_end) - pos)
                for s in spans
                if s.end > pos and s.start < line_end
            ]
            lines.append(serialize_tagged_text(line_plain, line_spans))
        pos = line_end + 1
    return lines


def _is_non_callout_superscript(
    plain_text: str,
    span_start: int,
    span_end: int,
    cleaned: str,
    spans: list[StyleSpan] | None = None,
) -> bool:
    """Return True when legacy `<sup>...</sup>` is a typographic contraction, ordinal, fraction, or exponent."""
    if not cleaned or len(cleaned) > 6 or " " in cleaned or cleaned == "0":
        return True
    if plain_text[span_end:].lstrip(" ").startswith(("/", "\u2044", "\u2215")):
        return True
    if spans and cleaned.isdigit() and any(s.tag == "sub" and (s.end == span_start or s.start == span_end) for s in spans):
        return True
    alpha = cleaned.rstrip(".")
    if alpha.isalpha():
        if len(alpha) >= 2:
            return True
        if span_start > 0 and (plain_text[span_start - 1].isalnum() or plain_text[span_start - 1] in "./"):
            return True
    return False


_SIDE_BY_SIDE_FN_SPLIT_RE = re.compile(
    r"(?:\s{2,}|\n|(?<=[.!?])\s+)\s*(?=<(?:sup|fnref)>\s*(?:\d+|[a-zA-Z]|[*†‡§‖¶]+)\s*</(?:sup|fnref)>)"
)
_PLAIN_NOTE_LEAD_SUP_RE = re.compile(r"^\s*<(?:sup|fnref)>\s*([^<]+?)\s*</(?:sup|fnref)>")


def split_side_by_side_footnote_tagged(tagged_text: str) -> list[tuple[str | None, str]]:
    """Split two-column consolidated footnote blocks (`<sup>3</sup> ...  <sup>4</sup> ...`) into segments."""
    parts = [p.strip() for p in _SIDE_BY_SIDE_FN_SPLIT_RE.split(tagged_text) if p and p.strip()]
    if len(parts) <= 1:
        return [(None, tagged_text)]
    out: list[tuple[str | None, str]] = []
    for part in parts:
        m_sup = _PLAIN_NOTE_LEAD_SUP_RE.match(part)
        lbl = m_sup.group(1).strip().strip("()[]").rstrip(".") if m_sup else None
        out.append((lbl or None, part))
    return out


def _callout_label_for_span(plain_text: str, span: StyleSpan, spans: list[StyleSpan]) -> str | None:
    if span.tag not in {"fnref", "sup"}:
        return None
    cleaned = plain_text[span.start : span.end].strip().strip("()[]")
    if not cleaned:
        return None
    if span.tag == "sup" and _is_non_callout_superscript(plain_text, span.start, span.end, cleaned, spans):
        return None
    return cleaned


def extract_inline_markers(tagged_text: str) -> list[str]:
    """Extract inline `<fnref>` and eligible legacy `<sup>` footnote callout labels."""
    _, plain_text, spans = parse_tagged_text(tagged_text)
    return [lbl for s in spans if (lbl := _callout_label_for_span(plain_text, s, spans)) is not None]


def strip_footnote_callouts_plain(tagged_text: str) -> str:
    """Return `plain_text` with inline `<fnref>` and legacy footnote callouts stripped cleanly."""
    _, plain_text, spans = parse_tagged_text(tagged_text)
    drop_ranges = [(s.start, s.end) for s in spans if _callout_label_for_span(plain_text, s, spans) is not None]
    if not drop_ranges:
        return plain_text
    out: list[str] = []
    cursor = 0
    for start, end in sorted(drop_ranges):
        if start > cursor:
            out.append(plain_text[cursor:start])
        if start > 0 and end < len(plain_text) and plain_text[start - 1].isalnum() and plain_text[end].isalnum():
            out.append(" ")
        cursor = max(cursor, end)
    if cursor < len(plain_text):
        out.append(plain_text[cursor:])
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


_GLUED_DIGITS_ALPHA_RE = re.compile(r"(?<=[A-Za-z\u00C0-\u024F][a-z\u00DF-\u00F6\u00F8-\u024F])\d{1,2}\b")
_GLUED_DIGITS_PUNCT_RE = re.compile(r"(?<=[A-Za-z\u00C0-\u024F][a-z\u00DF-\u00F6\u00F8-\u024F][,;!?)\]])\d{1,2}\b")
_GLUED_DIGITS_PERIOD_RE = re.compile(r"(?<=[A-Za-z\u00C0-\u024F][a-z\u00DF-\u00F6\u00F8-\u024F]{2}\.)\d{1,2}(?=\s|$|[)\],;!?])")
_GLUED_DIGITS_YEAR_RE = re.compile(r"(?<=\b(?:1[4-9]\d{2}|20\d{2})\.)[1-9]\d?(?=\s|$|[)\],;!?])")


def strip_glued_footnote_digits(text: str) -> str:
    """Strip untagged footnote callout digits glued to words, clause punct, or 4-digit years (preserving postal/formula codes)."""
    if not text:
        return text
    out = _GLUED_DIGITS_ALPHA_RE.sub("", text)
    out = _GLUED_DIGITS_PUNCT_RE.sub("", out)
    out = _GLUED_DIGITS_PERIOD_RE.sub("", out)
    out = _GLUED_DIGITS_YEAR_RE.sub("", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


_ENDNOTE_ROW_FOLIO_RE = re.compile(
    r"^\s*(?:(?:PAGE|<sc>\s*Page\s*</sc>)\s+)?(\d+|[ivxlcdm]+)\s+(\d+|[A-Za-z])\.\s+([\s\S]+)$",
    re.IGNORECASE,
)
_SUP_TAG_MASK_RE = re.compile(r"<(?:sup|fnref)>.*?</(?:sup|fnref)>", re.DOTALL | re.IGNORECASE)
_CIPHER_INITIAL_RE = re.compile(r"(?:^|[\s(\[\"“‘>])[A-Z]\.?$")


def promote_bare_symbol_in_tagged(
    tagged_text: str,
    sym: str,
    *,
    max_replacements: int = 0,
) -> tuple[str, bool]:
    """Promote bare footnote symbols (`Chiapé*`) to `<fnref>*</fnref>`, skipping cipher asterisks (`Mr. B*`, `***`)."""
    if not sym or sym not in tagged_text:
        return tagged_text, False
    esc = re.escape(sym)
    first_ch = re.escape(sym[0])
    pat = re.compile(
        rf"(?:[\w.,;:!?'\"“”‘’)\]]|</(?:i|b|sc|unclear)>)[ \t]?({esc})(?!{first_ch})(?![ \t]*{first_ch})"
    )
    masked = _SUP_TAG_MASK_RE.sub(lambda m: " " * len(m.group(0)), tagged_text)
    out: list[str] = []
    cursor = 0
    replaced = 0
    for match in pat.finditer(masked):
        if max_replacements > 0 and replaced >= max_replacements:
            break
        m_start, m_end = match.start(1), match.end(1)
        if sym == "*":
            prefix = tagged_text[: match.start(1)].rstrip(" \t")
            after = tagged_text[m_end:].lstrip(" \t")
            if _CIPHER_INITIAL_RE.search(prefix) or after.startswith(("—", "–")):
                continue
        out.append(tagged_text[cursor:m_start])
        out.append(f"<fnref>{sym}</fnref>")
        cursor = m_end
        replaced += 1
    if not replaced:
        return tagged_text, False
    out.append(tagged_text[cursor:])
    return "".join(out), True


def resolve_endnote_row_target(
    tagged_text: str,
    raw_note_label: str | None,
    folio_to_pnum: Mapping[str, int],
    current_page: int,
    target_page: int | None,
    target_folio: str | None,
) -> tuple[int | None, str | None, str, str]:
    """Resolve optional leading `<folio> <label>. <text>` on backmatter endnote rows."""
    norm_lbl = (raw_note_label or "").strip().strip("()[]").rstrip(".")
    match = _ENDNOTE_ROW_FOLIO_RE.match(tagged_text or "")
    if match:
        folio_tok, lbl_tok, rest_text = match.group(1), match.group(2), match.group(3).lstrip()
        mapped_page = folio_to_pnum.get(folio_tok) or folio_to_pnum.get(folio_tok.lower())
        if mapped_page is not None and mapped_page < current_page and (not norm_lbl or norm_lbl == lbl_tok):
            return mapped_page, folio_tok, lbl_tok, rest_text
    return target_page, target_folio, norm_lbl, tagged_text


def normalize_note_label(raw_label: str | None) -> str:
    """Normalize printed footnote marker for deterministic callout-to-note pairing."""
    if raw_label is None:
        return ""
    cleaned = str(raw_label).strip().strip("()[]")
    if cleaned.endswith(".") and len(cleaned) > 1 and cleaned[:-1].isalnum():
        cleaned = cleaned[:-1]
    return cleaned.strip()


def strip_redundant_leading_label(tagged_text: str, norm_label: str) -> str:
    """Strip a redundant leading superscript/symbol/parenthesized marker matching `norm_label`."""
    if not norm_label:
        return tagged_text
    match = _LEADING_NOTE_SUP_RE.match(tagged_text)
    if match and normalize_note_label(match.group(1)) == norm_label:
        rest = tagged_text[match.end() :].lstrip()
        return rest or tagged_text
    if norm_label in SYMBOL_NOTE_LABELS and tagged_text.startswith(f"{norm_label} "):
        return tagged_text[len(norm_label) :].lstrip()
    return re.sub(rf"^\s*\(\s*{re.escape(norm_label)}\s*\)\s*", "", tagged_text) or tagged_text
