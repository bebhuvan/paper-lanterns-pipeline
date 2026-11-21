"""Short display strings derived from transcribed text: website headlines, descriptions and page titles.

ROLE: Pure helpers that cut printed text at punctuation or word boundaries; they never rewrite the words.
INPUTS: Plain diplomatic text and correspondent/date strings.
OUTPUTS: Strings for headlines (opening words), meta descriptions and HTML <title>s.
INVARIANT: Routing is on punctuation and length only; no natural-language word lists.
"""

from __future__ import annotations

import re

ELLIPSIS = "…"
# A sentence ends at . ! or ? followed by whitespace and a capital (optionally after an opening quote or bracket).
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s+[\"“‘(\[]?[A-Z])")
_CLAUSE_BREAK_RE = re.compile(r"\s*(?:[,;:]|—|–|\s-\s)\s*")
_SPACE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _SPACE_RE.sub(" ", text).strip()


def _word_before(text: str, end: int) -> str:
    start = text.rfind(" ", 0, end) + 1
    return text[start:end]


def opening_headline(text: str, limit: int = 90, minimum: int = 20) -> str:
    """The letter's own opening words: the first sentence if it fits, else up to a clause break, else whole words.

    A full stop after a short word ("Mme.", "N.") is not taken as a sentence end, so initials never cut a name.
    """
    flat = _normalise(text)
    if len(flat) <= limit:
        return flat
    for m in _SENTENCE_END_RE.finditer(flat, 0, limit + 1):
        if m.end() >= minimum and len(_word_before(flat, m.start()).strip("\"“”‘’()[]")) >= 3:
            return flat[: m.end()]
    breaks = [m.start() for m in _CLAUSE_BREAK_RE.finditer(flat, 0, limit) if m.start() >= minimum]
    if breaks:
        return flat[: breaks[-1]].rstrip() + ELLIPSIS
    return clip_words(flat, limit)


def clip_words(text: str, limit: int = 155) -> str:
    """Whole words up to `limit` characters, with an ellipsis when anything was cut."""
    flat = _normalise(text)
    if len(flat) <= limit:
        return flat
    cut = flat.rfind(" ", 0, limit)
    return flat[: cut if cut > 0 else limit].rstrip(" ,;:—–-") + ELLIPSIS


def letter_page_title(sender: str, recipient: str, date_display: str) -> str:
    """"Sender to recipient, date", degrading gracefully when any part is missing."""
    who = (
        f"{sender} to {recipient}"
        if sender and recipient
        else sender or (f"Letter to {recipient}" if recipient else "Letter")
    )
    return f"{who}, {date_display}" if date_display else who
