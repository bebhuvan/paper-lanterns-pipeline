"""Diplomatic end-of-line dehyphenation over `PageIR` / `BlockIR`.

Operates directly on `tagged_text` and recomputes `(plain_text, spans)` via `parse_tagged_text`,
strictly respecting `block.line_breaks_preserved` on epistolary headers, datelines, salutations,
cascading valedictions, signatures, address blocks, and verse.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from lanterns.core.models import BlockIR, PageIR
from lanterns.core.tags import parse_tagged_text

DEHYPHENATABLE_PROSE_TYPES: frozenset[str] = frozenset(
    {"body", "blockquote", "editorial", "postscript", "footnote", "margin_note", "list_item", "index_entry", "toc_entry"}
)

_CARDINAL_TENS = frozenset({"twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"})
_CARDINAL_ONES = frozenset({"one", "two", "three", "four", "five", "six", "seven", "eight", "nine"})
_PRODUCTIVE_COMPOUND_PREFIXES = frozenset(
    {
        "non",
        "self",
        "well",
        "half",
        "all",
        "vice",
        "quasi",
        "anglo",
        "indo",
        "franco",
        "austro",
        "greco",
    }
)
_CLITIC_PARTICLES = frozenset({"i", "e", "ul", "al", "el", "ud", "ad", "un", "ur", "uz", "ba"})
_SUSPENDED_CONJUNCTIONS = frozenset({"and", "or", "nor", "to", "et", "und", "ou", "oder"})

_LETTERS = r"[^\W\d_]+"
_HYPHENS = r"[-‐­⸗]"
_INLINE_TAG_NAME = r"(?:i|b|sc|unclear)"
_TOKEN_RE = re.compile(rf"{_LETTERS}(?:[-‐⸗]{_LETTERS})*")
_HYPHEN_INSIDE_CLOSE_TAG_RE = re.compile(
    rf"({_LETTERS})({_HYPHENS})(</{_INLINE_TAG_NAME}>)(?=[ \t]*(?:\n|[ \t]+{_LETTERS}))",
    re.IGNORECASE,
)
_TAGGED_HYPHEN_WRAP_RE = re.compile(
    rf"({_LETTERS})(</{_INLINE_TAG_NAME}>)?({_HYPHENS})[ \t]*(?:\n[ \t]*|[ \t]+)(<{_INLINE_TAG_NAME}>)?({_LETTERS})",
    re.IGNORECASE,
)
_ADJACENT_LEFT_HYPHEN_RE = re.compile(rf"{_LETTERS}[-‐⸗](?:</?{_INLINE_TAG_NAME}>)*$", re.IGNORECASE)
_ADJACENT_RIGHT_HYPHEN_RE = re.compile(rf"^(?:</?{_INLINE_TAG_NAME}>)*[-‐⸗]{_LETTERS}", re.IGNORECASE)


def _same_style_boundary(close_tag: str | None, open_tag: str | None) -> bool:
    if not close_tag or not open_tag:
        return False
    return close_tag[2:-1].strip().lower() == open_tag[1:-1].strip().lower()


def _has_adjacent_compound_hyphen(text: str, start: int, end: int) -> bool:
    return bool(_ADJACENT_LEFT_HYPHEN_RE.search(text[:start]) or _ADJACENT_RIGHT_HYPHEN_RE.match(text[end:]))


def count_corpus_word_forms(
    pages: list[PageIR],
    witness_dir: str | Path | None = None,
) -> tuple[Counter[str], Counter[str]]:
    """Count intra-volume attested joined words (`correspondence`) vs mid-line hyphenated words (`self-same`)."""
    joined: Counter[str] = Counter()
    hyphenated: Counter[str] = Counter()
    for page in pages:
        for blk in page.blocks:
            for line in blk.plain_text.splitlines():
                stripped = line.rstrip()
                # Ignore physical line-end hyphens when building reference attestation counts
                clean_line = re.sub(rf"{_LETTERS}{_HYPHENS}\s*$", "", stripped)
                for tok in _TOKEN_RE.findall(clean_line):
                    cf = tok.casefold().replace("‐", "-").replace("⸗", "-")
                    if "-" in cf:
                        hyphenated[cf] += 1
                    elif len(cf) >= 4:
                        joined[cf] += 1
    if witness_dir is not None:
        wpath_dir = Path(witness_dir)
        if wpath_dir.is_dir():
            for wfile in sorted(wpath_dir.glob("*.txt")):
                for line in wfile.read_text(encoding="utf-8", errors="ignore").splitlines():
                    clean_line = re.sub(rf"{_LETTERS}{_HYPHENS}\s*$", "", line.rstrip())
                    for tok in _TOKEN_RE.findall(clean_line):
                        cf = tok.casefold().replace("‐", "-").replace("⸗", "-")
                        if "-" not in cf and len(cf) >= 4:
                            joined[cf] += 1
    return joined, hyphenated


def _is_structural_compound(left: str, right: str) -> tuple[bool, str]:
    l_cf, r_cf = left.casefold(), right.casefold()
    if l_cf in _CARDINAL_TENS and r_cf in _CARDINAL_ONES:
        return True, "cardinal_compound"
    if l_cf in _PRODUCTIVE_COMPOUND_PREFIXES and r_cf not in {"teenth", "tieth"}:
        return True, "prefix_compound"
    if r_cf in _CLITIC_PARTICLES or l_cf in {"al", "el", "ul", "ud", "mir", "shah"}:
        return True, "clitic_compound"
    if right[:1].isupper() and not left.isupper():
        return True, "capitalized_second_element"
    return False, ""


def _is_alpha_with_marks(tok: str) -> bool:
    return bool(tok) and any(ch.isalpha() for ch in tok) and all(
        ch.isalpha() or unicodedata.category(ch).startswith("M") for ch in tok
    )


def judge_hyphen_wrap(
    left: str,
    right: str,
    joined_counts: Counter[str],
    hyphen_counts: Counter[str],
) -> tuple[str, str]:
    """Decide `'join'` vs `'keep'` for a physical line-end hyphenated word pair `(left, right)`."""
    l_cf, r_cf = left.casefold(), right.casefold()
    j_uses = joined_counts[f"{l_cf}{r_cf}"]
    h_uses = hyphen_counts[f"{l_cf}-{r_cf}"]

    if j_uses > 0 and h_uses == 0:
        return "join", "corpus_joined"
    if h_uses > 0 and j_uses == 0:
        return "keep", "corpus_hyphenated"
    if j_uses > h_uses * 2:
        return "join", "corpus_majority"
    if h_uses > 0:
        return "keep", "corpus_hyphen_retained"

    is_comp, reason = _is_structural_compound(left, right)
    if is_comp:
        return "keep", reason
    if _is_alpha_with_marks(left) and _is_alpha_with_marks(right) and len(left) >= 2 and len(right) >= 2 and right.islower():
        return "join", "morphological_soft_wrap"
    return "keep", "conservative_retain"


def merge_hyphenated_tagged_pair(left_tagged: str, right_tagged: str) -> tuple[str, str, bool]:
    """Join a cross-page hyphenated boundary `(left_tagged, right_tagged)` diplomatically when soft-wrapped."""
    lt = left_tagged.rstrip()
    rt = right_tagged.lstrip()
    m_left = re.search(rf"({_LETTERS})({_HYPHENS})((?:</{_INLINE_TAG_NAME}>)*)$", lt, re.IGNORECASE)
    if not m_left:
        return lt, rt, False
    left_word = m_left.group(1)
    closes = m_left.group(3) or ""
    m_right = re.match(rf"^((?:<{_INLINE_TAG_NAME}>)*)({_LETTERS})(.*)$", rt, re.IGNORECASE | re.DOTALL)
    if not m_right:
        return lt, rt, False
    opens = m_right.group(1) or ""
    right_word = m_right.group(2)
    verdict, _ = judge_hyphen_wrap(left_word, right_word, Counter(), Counter())
    if verdict != "join":
        return lt, rt, False
    prefix_left = lt[: m_left.start(2)]
    rest_right = m_right.group(3)
    if _same_style_boundary(closes, opens):
        return f"{prefix_left}{right_word}", rest_right.lstrip(), True
    return f"{prefix_left}{closes}{opens}{right_word}", rest_right.lstrip(), True


def _resolve_single_block(
    blk: BlockIR,
    joined_counts: Counter[str],
    hyphen_counts: Counter[str],
    decisions: list[dict[str, Any]],
) -> BlockIR:
    if blk.line_breaks_preserved or blk.type not in DEHYPHENATABLE_PROSE_TYPES:
        return blk
    if not any(ch in blk.tagged_text for ch in ("\n", "-", "‐", "\xad", "⸗")):
        return blk

    prepped = _HYPHEN_INSIDE_CLOSE_TAG_RE.sub(r"\1\3\2", blk.tagged_text)

    def _repl(match: re.Match[str]) -> str:
        left = match.group(1)
        close_t = match.group(2) or ""
        open_t = match.group(4) or ""
        right = match.group(5)
        is_inline_space = "\n" not in match.group(0)
        if is_inline_space and right.casefold() in _SUSPENDED_CONJUNCTIONS:
            return match.group(0)
        if _has_adjacent_compound_hyphen(prepped, match.start(), match.end()):
            verdict, basis = "keep", "multi_hyphen_compound"
        else:
            verdict, basis = judge_hyphen_wrap(left, right, joined_counts, hyphen_counts)
        decisions.append(
            {
                "block_id": blk.block_id,
                "page_number": blk.page_number,
                "left": left,
                "right": right,
                "verdict": verdict,
                "basis": basis,
            }
        )
        if verdict == "join":
            if _same_style_boundary(close_t, open_t):
                return f"{left}{right}"
            return f"{left}{close_t}{open_t}{right}"
        if is_inline_space and right[:1].isupper():
            return match.group(0)
        return f"{left}{close_t}-{open_t}{right}"

    dehyphenated = _TAGGED_HYPHEN_WRAP_RE.sub(_repl, prepped)
    unwrapped = re.sub(r"[ \t]*\n[ \t]*", " ", dehyphenated).replace("\xad", "")
    unwrapped = _resolve_inline_soft_wraps(unwrapped, joined_counts, hyphen_counts)
    if unwrapped == blk.tagged_text:
        return blk
    canonical_tagged, plain_text, spans = parse_tagged_text(unwrapped)
    return blk.model_copy(update={"tagged_text": canonical_tagged, "plain_text": plain_text, "spans": spans})


_INLINE_SOFT_WRAP_RE = re.compile(
    rf"(?<![-‐⸗])(?P<left>{_LETTERS})(?P<close></{_INLINE_TAG_NAME}>)?[-‐⸗](?P<open><{_INLINE_TAG_NAME}>)?(?P<right>{_LETTERS})(?![-‐⸗])"
)


def _resolve_inline_soft_wraps(
    text: str,
    joined_counts: Counter[str],
    hyphen_counts: Counter[str],
) -> str:
    """Recover mid-line space-free soft wraps (e.g. `commo-dities`) strongly attested as joined."""

    def _repl_inline(match: re.Match[str]) -> str:
        left = match.group("left")
        close_t = match.group("close") or ""
        open_t = match.group("open") or ""
        right = match.group("right")
        if (
            len(left) < 2
            or len(right) < 2
            or not left[0].isalpha()
            or not right.islower()
            or _has_adjacent_compound_hyphen(text, match.start(), match.end())
            or _is_structural_compound(left, right)[0]
        ):
            return match.group(0)
        j_uses = joined_counts[f"{left.casefold()}{right.casefold()}"]
        h_uses = hyphen_counts[f"{left.casefold()}-{right.casefold()}"]
        if j_uses >= max(2, h_uses * 2 + 1):
            if _same_style_boundary(close_t, open_t):
                return f"{left}{right}"
            return f"{left}{close_t}{open_t}{right}"
        return match.group(0)

    return _INLINE_SOFT_WRAP_RE.sub(_repl_inline, text)


def dedup_or_join_cross_page_tagged(
    left_tagged: str,
    right_tagged: str,
    joined_counts: Counter[str],
    hyphen_counts: Counter[str],
) -> tuple[str, str]:
    """Resolve cross-page hyphen joins via `judge_hyphen_wrap` or deduplicate printed catchwords."""
    l_raw = left_tagged.rstrip()
    r_raw = right_tagged.lstrip()
    hyph_m = re.search(rf"({_LETTERS}){_HYPHENS}((?:</[a-z]+>)*)\s*$", l_raw)
    word_m = re.match(
        rf"^((?:<[a-z]+>)*)(?:[“\"‘']\s*)?({_LETTERS})(.*)$",
        r_raw,
        re.DOTALL | re.IGNORECASE,
    )
    if hyph_m and word_m and len(hyph_m.group(1)) >= 2 and word_m.group(2)[0].islower():
        left_frag = hyph_m.group(1)
        right_frag = word_m.group(2)
        verdict, _ = judge_hyphen_wrap(left_frag, right_frag, joined_counts, hyphen_counts)
        joiner = "" if verdict == "join" else "-"
        l_new = f"{l_raw[: hyph_m.start()]}{left_frag}{joiner}{right_frag}{hyph_m.group(2)}"
        r_new = f"{word_m.group(1)}{(word_m.group(3) or '').lstrip()}"
        return l_new, r_new

    if not hyph_m and l_raw and r_raw:
        l_parts = l_raw.rsplit(None, 1)
        r_parts = r_raw.split(None, 2)
        if len(l_parts) == 2 and len(r_parts) >= 1:
            last_tok = re.sub(r"</?[a-z]+>", "", l_parts[1])
            first_tok = re.sub(r"</?[a-z]+>", "", r_parts[0])
            if len(r_parts) >= 2 and re.fullmatch(r"[.,;:!?)]+", re.sub(r"</?[a-z]+>", "", r_parts[1])):
                first_tok = f"{first_tok}{re.sub(r'</?[a-z]+>', '', r_parts[1])}"
            clean_tok = last_tok.strip(".,;:!?()[]\"'“”‘’")
            if (
                len(clean_tok) >= 4
                and last_tok == first_tok
                and (clean_tok[0].isupper() or last_tok != clean_tok or len(clean_tok) >= 5)
            ):
                return l_parts[0], r_raw

    return l_raw, r_raw


def resolve_page_ir_line_wraps(
    pages: list[PageIR],
    *,
    witness_dir: str | Path | None = None,
) -> tuple[list[PageIR], dict[str, Any]]:
    """Resolve end-of-line hyphens across `pages`, returning `(updated_pages, wrap_report)`."""
    joined_counts, hyphen_counts = count_corpus_word_forms(pages, witness_dir=witness_dir)
    decisions: list[dict[str, Any]] = []
    out_pages: list[PageIR] = []

    for page in pages:
        if page.failed_closed or not page.blocks:
            out_pages.append(page)
            continue
        new_blocks = [_resolve_single_block(blk, joined_counts, hyphen_counts, decisions) for blk in page.blocks]
        out_pages.append(page.model_copy(update={"blocks": new_blocks}))

    summary = Counter(d["verdict"] for d in decisions)
    report = {
        "version": "line-wraps-v2",
        "site_count": len(decisions),
        "verdicts": dict(sorted(summary.items())),
        "sites": decisions,
    }
    return out_pages, report
