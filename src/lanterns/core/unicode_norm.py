"""Diplomatic Unicode NFC normalization for multilingual historical correspondence.

Decomposes ONLY pure typographic presentation ligatures `U+FB00..U+FB06`:
  - `ﬀ` (U+FB00) -> `ff`
  - `ﬁ` (U+FB01) -> `fi`
  - `ﬂ` (U+FB02) -> `fl`
  - `ﬃ` (U+FB03) -> `ffi`
  - `ﬄ` (U+FB04) -> `ffl`
  - `ﬅ` (U+FB05) -> `ſt` (preserves medial long-s `ſ` U+017F)
  - `ﬆ` (U+FB06) -> `st`

Strictly preserves lexical/orthographic ligatures (`æ`, `œ`, `Æ`, `Œ`, `ß`), medial long-s (`ſ`),
Hunterian/Jonesian (`Rájput`, `Khán`), IAST (`Śiva`, `ṛ`, `ṃ`, `ḥ`, `ṅ`, `ñ`, `ṭ`, `ḍ`, `ṇ`, `ś`, `ṣ`),
European diacritics (`depôt` vs `dépôt`, `ä`, `ö`, `ü`), Greek, Cyrillic, Perso-Arabic, and Indic scripts.
Never applies NFKC/NFKD compatibility folding to archival text.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

PRESENTATION_LIGATURE_MAP = str.maketrans(
    {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\ufb05": "\u017ft",
        "\ufb06": "st",
    }
)

_URL_SPAN_RE = re.compile(r"(?:https?|file)://\S+", re.IGNORECASE)
_VLM_UTF8_PERCENT_RE = re.compile(
    r"(?:%(?:C[2-9A-Fa-f]|D[0-9A-Fa-f])%[89ABab][0-9A-Fa-f]"
    r"|%E[0-9A-Fa-f](?:%[89ABab][0-9A-Fa-f]){2}"
    r"|%F[0-4](?:%[89ABab][0-9A-Fa-f]){3})+"
)
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

CONTROL_CHARS_WARNING = "control_chars_stripped"


def _decode_vlm_utf8_percent_escapes(text: str) -> str:
    """Decode multi-byte UTF-8 percent-encoding glitches emitted by VLM JSON tokenizers outside URLs."""
    if "%" not in text:
        return text
    url_spans = [m.span() for m in _URL_SPAN_RE.finditer(text)]

    def _replace(match: re.Match[str]) -> str:
        ms, me = match.span()
        if any(us <= ms and me <= ue for us, ue in url_spans):
            return match.group(0)
        raw_seq = match.group(0)
        try:
            decoded = bytes.fromhex(raw_seq.replace("%", "")).decode("utf-8")
        except ValueError:
            return raw_seq
        if decoded and all(ch.isprintable() for ch in decoded):
            return decoded
        return raw_seq

    return _VLM_UTF8_PERCENT_RE.sub(_replace, text)


def normalize_diplomatic_text(text: str) -> str:
    """Return canonical diplomatic NFC text with ONLY U+FB00..U+FB06 presentation ligatures decomposed."""
    if not text:
        return ""
    cleaned = _decode_vlm_utf8_percent_escapes(text).translate(PRESENTATION_LIGATURE_MAP)
    return unicodedata.normalize("NFC", cleaned)


nfc_text = normalize_diplomatic_text


def fold_for_search(text: str) -> str:
    """Return diacritic-folded lowercase string strictly for search indexing (never for edition text)."""
    if not text:
        return ""
    folded_ligs = (
        text.replace("æ", "ae")
        .replace("Æ", "ae")
        .replace("œ", "oe")
        .replace("Œ", "oe")
        .replace("ſ", "s")
    )
    decomposed = unicodedata.normalize("NFD", folded_ligs)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", stripped).casefold()


def strip_control_chars(value: Any) -> tuple[Any, bool]:
    """Recursively strip NUL and C0 control characters (except \\n and \\t) from wire JSON payloads."""
    if isinstance(value, str):
        if _C0_CONTROL_RE.search(value):
            return _C0_CONTROL_RE.sub("", value), True
        return value, False
    if isinstance(value, list):
        changed = False
        out_list: list[Any] = []
        for item in value:
            cleaned, hit = strip_control_chars(item)
            out_list.append(cleaned)
            changed = changed or hit
        return out_list, changed
    if isinstance(value, dict):
        changed = False
        out_dict: dict[str, Any] = {}
        for k, v in value.items():
            cleaned, hit = strip_control_chars(v)
            out_dict[str(k)] = cleaned
            changed = changed or hit
        return out_dict, changed
    return value, False


_BINDER_SIG_RE = re.compile(
    r"^(?:[A-HJ-UWYZ]\*?|[a-hj-uwyz]\*?|\d+[A-Za-z]|[A-Za-z]\s*[*]?\s*\d+|\*+\d*|(?:[A-Z]\.){1,3}\s*[–—-]\s*\d+)$"
)


def is_binder_signature(folio_text: str | None, page_number: int = 1) -> bool:
    """Return True when a bottom-margin token (`A*`, `B`, `B 2`, `G.C.–4`) is a printer's gathering mark."""
    if not folio_text:
        return False
    cleaned = folio_text.strip().strip("[]().")
    if not cleaned or len(cleaned) > 12:
        return True
    if _BINDER_SIG_RE.match(cleaned):
        return True
    if len(cleaned) == 1 and cleaned in {"C", "D", "L", "M"} and page_number < 85:
        return True
    return False

