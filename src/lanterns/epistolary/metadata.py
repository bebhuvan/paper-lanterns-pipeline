"""ROLE: Diplomatic EpistolaryMetadata extraction, script/orthography detection, and slug generation.
INPUTS: Ordered BlockIR sequence of a LetterUnit and optional letter_map explicit overrides.
OUTPUTS: EpistolaryMetadata and deterministic collision-free letter slugs.
INVARIANT: Never rewrites text and never matches natural-language content words.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal, cast

from lanterns.core.models import BlockIR, EpistolaryMetadata
from lanterns.core.tags import (
    strip_footnote_callouts_plain,
    strip_glued_footnote_digits,
)
from lanterns.core.unicode_norm import fold_for_search

_FOLIO_MARKER_RE = re.compile(r"\[p\.\s+[^\]]+\]\s*")
_PURE_NUMERAL_LINE_RE = re.compile(r"^[(\[]?(?:\d{1,4}|[IVXLCDM]{1,9})[.)\]]?$")
_LABEL_NUMERAL_LINE_RE = re.compile(
    r"^(?:(?:letter|lettre|epistle|epistola|brief|carta|dispatch)(?:\s+no\.?)?|no\.?|num\.?|number(?:\s+[a-z]+)?)[:\s]+([IVXLCDM]{1,9}|\d{1,4}[a-z]?)[.)]?$",
    re.IGNORECASE,
)
_LEADING_NUMERAL_RE = re.compile(r"^[(\[]?(\d{1,4}|[IVXLCDM]{1,9})[.)\]]?(?:\s|[—–:\-]|$)")
_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_ALPHA_TOK_RE = re.compile(r"[^\W\d_]+")
_BARE_PAREN_NUM_RE = re.compile(r"\(\d{1,2}\)")
_LEADING_HDR_NUM_RE = re.compile(r"^(?:[IVXLCDM]{1,7}|\d{1,4}[a-z]?)\.?\s+(?=[A-Za-z\[“\"'])")
_ONLY_HDR_NUM_RE = re.compile(
    r"^(?:letter\s+(?:no\.?\s*)?(?:[ivxlcdm]+|\d+[a-z]?)|(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+letter|[ivxlcdm]{1,7}|\d{1,4}[a-z]?)\.?\s*$",
    re.IGNORECASE,
)
_DATED_TAIL_RE = re.compile(r"(?:\s*[;,]\s*|\s+)(?:dated|written(?:\s+in)?)\s+(.+)$", re.IGNORECASE)
_COMMA_DATE_RE = re.compile(
    r"^(.+?),\s+(?=(?:the\s+)?\d|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z.]*\s+\d)(.*\b(?:1[4-9]\d{2}|20\d{2})\b.*)$"
)
_PERIOD_DATE_RE = re.compile(
    r"^(.+?)(?<!\b(?:Wm|Ed|Mr|Dr|St))(?<!\b(?:Rev|Mrs|Tho|Rob|Col|Gen|Hon|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))"
    r"(?<!\b(?:Rich|Capt|Thos|Sept|Will))(?<!\b(?:Lieut))(?<!\b(?:Honble))\.\s+([^.]*\b(?:1[4-9]\d{2}|20\d{2})\b.*)$"
)
_NON_SENDER_PREFIX_RE = re.compile(
    r"^(?:the\s+(?:copy|following|foregoing|above|same|king's\s+firmand)|a\s+|an\s+|letter|reply|answer|articles|copy|agreement|objections?|consultations?|notes?|account|overland|extract|minutes?|declaration|proceedings|commission|instructions?|memorandum|firmand)\b",
    re.IGNORECASE,
)

IAST_CODEPOINTS = frozenset(
    "āĀīĪūŪṛṚṝṜḷḶḹḸṁṀṃṂḥḤṅṄṭṬḍḌṇṆśŚṣṢ"
    "ḏḎḫḪẖḳḲḵḴḻḺṟṞṯṮẓẒẕẔʿʾ"
)

EUROPEAN_DIACRITIC_CODEPOINTS = frozenset(
    "àáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿßœčďěğĺľłńňőřšťůűźżž"
    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞŸŒČĎĚĞĹĽŁŃŇŐŘŠŤŮŰŹŻŽ"
)

_SCRIPT_RANGES: tuple[tuple[str, int, int], ...] = (
    ("Greek", 0x0370, 0x03FF),
    ("Greek", 0x1F00, 0x1FFF),
    ("Cyrillic", 0x0400, 0x052F),
    ("Arabic/Perso-Arabic", 0x0600, 0x06FF),
    ("Arabic/Perso-Arabic", 0x0750, 0x077F),
    ("Arabic/Perso-Arabic", 0x08A0, 0x08FF),
    ("Arabic/Perso-Arabic", 0xFB50, 0xFDFF),
    ("Arabic/Perso-Arabic", 0xFE70, 0xFEFF),
    ("Devanagari", 0x0900, 0x097F),
    ("Devanagari", 0xA8E0, 0xA8FF),
    ("Bengali", 0x0980, 0x09FF),
    ("Gurmukhi", 0x0A00, 0x0A7F),
    ("Gujarati", 0x0A80, 0x0AFF),
    ("Oriya", 0x0B00, 0x0B7F),
    ("Tamil", 0x0B80, 0x0BFF),
    ("Telugu", 0x0C00, 0x0C7F),
    ("Kannada", 0x0C80, 0x0CFF),
    ("Malayalam", 0x0D00, 0x0D7F),
    ("Sinhala", 0x0D80, 0x0DFF),
    ("Modi", 0x11600, 0x1165F),
)

_SCRIPT_ORDER: tuple[str, ...] = (
    "Latin",
    "Devanagari",
    "Arabic/Perso-Arabic",
    "Bengali",
    "Gurmukhi",
    "Gujarati",
    "Oriya",
    "Tamil",
    "Telugu",
    "Kannada",
    "Malayalam",
    "Sinhala",
    "Modi",
    "Greek",
    "Cyrillic",
    "IAST",
    "European diacritics",
)


def _strip_folio_markers(text: str) -> str:
    return _FOLIO_MARKER_RE.sub("", text or "").strip()


def _classify_char_script(ch: str) -> str | None:
    code = ord(ch)
    if (0x0041 <= code <= 0x005A) or (0x0061 <= code <= 0x007A) or (0x00C0 <= code <= 0x024F) or (0x1E00 <= code <= 0x1EFF):
        return "Latin"
    for label, low, high in _SCRIPT_RANGES:
        if low <= code <= high:
            return label
    return None


def detect_scripts_and_orthography(text: str) -> list[str]:
    """Detect scripts and scholarly diacritic layers strictly from Unicode codepoints."""
    found: set[str] = set()
    nfc = unicodedata.normalize("NFC", text or "")
    for ch in nfc:
        if not ch.isalpha() and ch not in IAST_CODEPOINTS:
            continue
        script_name = _classify_char_script(ch)
        if script_name is not None:
            found.add(script_name)
        if ch in IAST_CODEPOINTS:
            found.add("IAST")
        elif ch in EUROPEAN_DIACRITIC_CODEPOINTS:
            found.add("European diacritics")
    return [name for name in _SCRIPT_ORDER if name in found]


def _partition_outer_and_enclosed(
    blocks: list[BlockIR],
    enclosure_ids: set[str] | None = None,
) -> tuple[list[BlockIR], bool]:
    explicit_enc = enclosure_ids or set()
    outer: list[BlockIR] = []
    depth = 0
    has_enclosure = False
    for blk in blocks:
        if blk.epistolary_transition == "starts_enclosure":
            depth += 1
            has_enclosure = True
        if blk.block_id in explicit_enc:
            has_enclosure = True
        if blk.type == "blockquote":
            has_enclosure = True
        if depth == 0 and blk.block_id not in explicit_enc:
            outer.append(blk)
        if blk.epistolary_transition == "ends_enclosure" and depth > 0:
            depth -= 1
    return (outer if outer else list(blocks)), has_enclosure


def _extract_letter_number(header_blocks: list[BlockIR]) -> str | None:
    for blk in header_blocks:
        for raw_line in strip_footnote_callouts_plain(blk.tagged_text).splitlines():
            line = _strip_folio_markers(raw_line).strip()
            if not line:
                continue
            if _PURE_NUMERAL_LINE_RE.match(line):
                return line.strip("()[]").rstrip(".")
            lbl_m = _LABEL_NUMERAL_LINE_RE.match(line)
            if lbl_m:
                return lbl_m.group(1)
            lead = _LEADING_NUMERAL_RE.match(line)
            if lead:
                return lead.group(1)
    return None


def _compute_dateline_position(
    outer_blocks: list[BlockIR],
) -> Literal["top", "bottom", "inline", "absent"]:
    dl_indices = [idx for idx, b in enumerate(outer_blocks) if b.type == "dateline"]
    if not dl_indices:
        return "absent"
    first_dl_idx = dl_indices[0]
    first_dl = outer_blocks[first_dl_idx]
    if first_dl.alignment == "inline":
        return "inline"

    body_indices = [idx for idx, b in enumerate(outer_blocks) if b.type in {"body", "verse"}]
    closer_indices = [idx for idx, b in enumerate(outer_blocks) if b.type in {"valediction", "signature"}]
    if not body_indices:
        return "bottom" if (closer_indices and first_dl_idx >= closer_indices[0]) else "top"
    if first_dl_idx < body_indices[0]:
        return "top"
    if first_dl_idx > body_indices[-1] or (closer_indices and first_dl_idx >= closer_indices[0]):
        return "bottom"
    return "inline"


def _clean_person_display(blocks: list[BlockIR]) -> str | None:
    if not blocks:
        return None
    parts: list[str] = []
    for blk in blocks:
        cleaned = strip_glued_footnote_digits(_strip_folio_markers(strip_footnote_callouts_plain(blk.tagged_text)))
        collapsed = " ".join(cleaned.split()).strip(" ,;—–")
        alpha_tokens = [tok.casefold() for tok in _ALPHA_TOK_RE.findall(collapsed)]
        if collapsed and alpha_tokens and not all(tok in {"c", "etc"} for tok in alpha_tokens):
            parts.append(collapsed)
    return " / ".join(parts) if parts else None


def _extract_incipit(outer_blocks: list[BlockIR], all_blocks: list[BlockIR], max_chars: int = 140) -> str:
    candidates = [b for b in outer_blocks if b.type in {"body", "verse"}]
    if not candidates:
        candidates = [b for b in outer_blocks if b.type == "salutation"] or list(all_blocks)
    for blk in candidates:
        cleaned = _strip_folio_markers(strip_footnote_callouts_plain(blk.tagged_text))
        single_line = " ".join(cleaned.split())
        if not single_line:
            continue
        if len(single_line) <= max_chars:
            return single_line
        clipped = single_line[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:")
        return f"{clipped or single_line[:max_chars]}…"
    return ""


def _join_block_tagged(blocks: list[BlockIR]) -> str | None:
    parts = [_strip_folio_markers(b.tagged_text) for b in blocks if _strip_folio_markers(b.tagged_text)]
    return "\n".join(parts) if parts else None


def _join_block_plain(blocks: list[BlockIR]) -> str | None:
    parts = [
        " ".join(
            strip_glued_footnote_digits(_strip_folio_markers(strip_footnote_callouts_plain(b.tagged_text))).split()
        )
        for b in blocks
    ]
    non_empty = [p for p in parts if p]
    return " ".join(non_empty) if non_empty else None


def _clean_hdr_fragment(val: str) -> str:
    return " ".join(val.replace("\u00ad", "").split()).strip(" ,;*†‡.")


def _unfold_header_logical_lines(tagged_lines: list[str]) -> list[str]:
    """Normalize and unfold continuation lines across `letter_header` tagged blocks."""
    logical_lines: list[str] = []
    for raw_block in tagged_lines:
        cur = ""
        for raw_ln in strip_footnote_callouts_plain(raw_block).splitlines():
            part = _BARE_PAREN_NUM_RE.sub("", strip_glued_footnote_digits(raw_ln))
            part = " ".join(part.split()).strip()
            if not part or _ONLY_HDR_NUM_RE.match(part):
                continue
            part = _LEADING_HDR_NUM_RE.sub("", part).strip()
            if not part:
                continue
            can_append = (
                cur
                and not cur.endswith((".", "!", "?", "]", "”", '"'))
                and not re.match(r"^(?:to|from|letter)\b", part, re.I)
                and not cur.upper().startswith("LETTER")
            )
            if can_append:
                cur = f"{cur} {part}"
            else:
                if cur:
                    logical_lines.append(cur)
                cur = part
        if cur:
            logical_lines.append(cur)
    return logical_lines


def _split_recipient_and_date(rest: str, dateline: str | None) -> tuple[str, str | None]:
    """Split an optional trailing date clause off a recipient header fragment."""
    if not dateline:
        date_split = _COMMA_DATE_RE.match(rest) or _PERIOD_DATE_RE.match(rest)
        if date_split:
            return _clean_hdr_fragment(date_split.group(1)), " ".join(date_split.group(2).split()).strip()
    return _clean_hdr_fragment(rest), dateline


def parse_header_metadata_lines(tagged_lines: list[str]) -> tuple[str | None, str | None, str | None]:
    """Extract `(sender, recipient, dateline)` from printed `letter_header` tagged lines."""
    logical_lines = _unfold_header_logical_lines(tagged_lines)
    sender: str | None = None
    recipient: str | None = None
    dateline: str | None = None

    for line in logical_lines:
        dated_m = _DATED_TAIL_RE.search(line)
        head_part = line
        if dated_m and not dateline:
            dateline = " ".join(dated_m.group(1).split()).strip()
            head_part = line[: dated_m.start()].strip(" ;,")

        from_to_m = re.match(
            r"^(?:(?:a\s+)?letter\s+)?(?:\[from\s+([^\]]+)\]|from\s+(.+?))[,]?\s+to\s+(.+)$",
            head_part,
            re.IGNORECASE,
        )
        if from_to_m and not (sender and recipient):
            sender = sender or _clean_hdr_fragment(from_to_m.group(1) or from_to_m.group(2) or "")
            rec_part, dateline = _split_recipient_and_date(from_to_m.group(3).strip(), dateline)
            recipient = recipient or rec_part
            continue

        to_from_m = re.match(
            r"^(?:(?:a\s+)?letter\s+(?:addressed\s+)?)?to\s+(.+?)\s+from\s+(.+)$",
            head_part,
            re.IGNORECASE,
        )
        if to_from_m and not recipient:
            recipient = _clean_hdr_fragment(to_from_m.group(1))
            sender = sender or _clean_hdr_fragment(to_from_m.group(2))
            continue

        to_only_m = re.match(
            r"^(?:(?:a\s+)?letter\s+(?:addressed\s+)?)?to\s+(.+)$",
            head_part,
            re.IGNORECASE,
        )
        if to_only_m and not recipient:
            recipient, dateline = _split_recipient_and_date(to_only_m.group(1).strip(), dateline)
            continue

        s_to_r_m = re.match(r"^(.+?)\s+to\s+(.+)$", head_part, re.IGNORECASE)
        if s_to_r_m and not (sender and recipient):
            cand_s = _clean_hdr_fragment(s_to_r_m.group(1))
            cand_r = s_to_r_m.group(2).strip()
            if (
                cand_s
                and not _NON_SENDER_PREFIX_RE.match(cand_s)
                and len(cand_s.split()) <= 14
                and "(" not in cand_s
            ):
                sender = sender or cand_s
                rec_part, dateline = _split_recipient_and_date(cand_r, dateline)
                recipient = recipient or rec_part
                continue

        if (
            not dateline
            and len(head_part.split()) <= 12
            and re.search(r"\b(?:1[4-9]\d{2}|20\d{2})\b", head_part)
            and not head_part.lower().startswith(("[missing", "missing"))
        ):
            dateline = " ".join(head_part.split()).strip()

    return sender or None, recipient or None, dateline or None


def extract_epistolary_metadata(
    blocks: list[BlockIR],
    *,
    enclosure_block_ids: set[str] | None = None,
    explicit_override: dict[str, Any] | None = None,
) -> EpistolaryMetadata:
    """Extract diplomatic `EpistolaryMetadata` strictly from block types and Unicode scripts."""
    outer, has_enc = _partition_outer_and_enclosed(blocks, enclosure_block_ids)
    headers = [b for b in outer if b.type == "letter_header"]
    datelines = [b for b in outer if b.type == "dateline"]
    salutations = [b for b in outer if b.type == "salutation"]
    valedictions = [b for b in outer if b.type == "valediction"]
    signatures = [b for b in outer if b.type == "signature"]
    addresses = [b for b in outer if b.type == "address_block"]
    hdr_sender, hdr_recipient, hdr_dateline = parse_header_metadata_lines([b.tagged_text for b in headers])

    combined_plain = "\n".join(_strip_folio_markers(b.plain_text) for b in blocks)
    meta = EpistolaryMetadata(
        letter_number_label=_extract_letter_number(headers),
        header_tagged=_join_block_tagged(headers),
        sender_display=_clean_person_display(signatures) or hdr_sender,
        recipient_display=hdr_recipient or _clean_person_display(addresses),
        dateline_tagged=_join_block_tagged(datelines) or hdr_dateline,
        dateline_plain=_join_block_plain(datelines) or hdr_dateline,
        dateline_position=_compute_dateline_position(outer),
        salutation_tagged=_join_block_tagged(salutations),
        valediction_tagged=_join_block_tagged(valedictions),
        signature_tagged=_join_block_tagged(signatures),
        address_block_tagged=_join_block_tagged(addresses),
        has_postscript=any(b.type == "postscript" for b in blocks),
        has_enclosure=has_enc,
        languages=detect_scripts_and_orthography(combined_plain),
        incipit=_extract_incipit(outer, blocks),
    )

    patch = {
        key: explicit_override[key]
        for key in (
            "letter_number_label",
            "header_tagged",
            "sender_display",
            "recipient_display",
            "dateline_tagged",
            "dateline_plain",
            "dateline_position",
            "languages",
        )
        if (explicit_override or {}).get(key) is not None
    }
    return cast(EpistolaryMetadata, meta.model_copy(update=patch) if patch else meta)


def build_letter_slug(
    sequence_index: int,
    metadata: EpistolaryMetadata,
    *,
    explicit_slug: str | None = None,
    used_slugs: set[str] | None = None,
) -> str:
    """Build a deterministic, URL-safe, collision-free slug for a `LetterUnit`."""
    taken = used_slugs if used_slugs is not None else set()
    if explicit_slug:
        base_cand = _SLUG_NON_ALNUM_RE.sub("-", fold_for_search(explicit_slug)).strip("-")
    else:
        base_parts = [f"letter-{sequence_index:04d}"]
        if metadata.sender_display:
            sender_token = _SLUG_NON_ALNUM_RE.sub("-", fold_for_search(metadata.sender_display)).strip("-")
            if sender_token:
                base_parts.append(sender_token[:24].rstrip("-"))
        base_cand = "-".join(base_parts)

    slug = base_cand or f"letter-{sequence_index:04d}"
    if slug not in taken:
        taken.add(slug)
        return slug
    suffix = 2
    while f"{slug}-{suffix}" in taken:
        suffix += 1
    unique_slug = f"{slug}-{suffix}"
    taken.add(unique_slug)
    return unique_slug
