"""ROLE: Two-tier archival AST validators enforcing 100% block conservation and diplomatic fidelity.
INPUTS: Compiled CollectionAST and optional proof_ledger / confirmed_block_ids.
OUTPUTS: Deterministic list of ValidationFinding items (blocker, warning, info).
INVARIANT: Flags issues with concrete evidence; never silently mutates or drops archival text.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

from lanterns.core.models import (
    FURNITURE_BLOCK_TYPES,
    BlockIR,
    CollectionAST,
    ValidationFinding,
)
from lanterns.core.tags import extract_inline_markers
from lanterns.epistolary.compiler import normalize_note_label
from lanterns.epistolary.corrections import is_human_protected_page

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")
_PUA_CHAR_RE = re.compile(r"[\ue000-\uf8ff]")
_SCANLINE_LEAK_RE = re.compile(r"\[L\d{2,3}\]")
_FOLIO_PREFIX_RE = re.compile(r"^\[p\.\s+[^\]]+\]\s*")
_BARE_PAGE_NUM_RE = re.compile(r"^(?:[—–\-]\s*)?\d{1,4}(?:\s*[—–\-])?$")
_WORD_TOKEN_RE = re.compile(r"[^\W\d_]{4,}")


def _strip_folio_prefix(text: str) -> str:
    return _FOLIO_PREFIX_RE.sub("", (text or "").strip()).strip()


def _collect_confirmed_block_ids(
    ast: CollectionAST,
    proof_ledger: dict[str, Any] | None = None,
    *,
    human_only: bool = False,
) -> set[str]:
    confirmed: set[str] = set()
    if not human_only:
        confirmed.update(str(x) for x in (ast.proof_summary.get("confirmed_block_ids") or []))
    if not proof_ledger:
        return confirmed
    pages_by_num = {p.page_number: p for p in ast.pages}
    for p_str, rec in (proof_ledger.get("pages") or {}).items():
        if not isinstance(rec, dict):
            continue
        if not human_only:
            for bid in (rec.get("evidence") or {}).get("confirmed_block_ids") or []:
                confirmed.add(str(bid))
        if is_human_protected_page(rec) and int(p_str) in pages_by_num:
            for blk in pages_by_num[int(p_str)].blocks:
                if blk.type not in FURNITURE_BLOCK_TYPES:
                    confirmed.add(blk.block_id)
    return confirmed


def source_block_coverage(ast: CollectionAST) -> list[ValidationFinding]:
    """Mathematical 100% conservation check across all non-furniture source blocks."""
    letters_by_id = {lu.letter_id: lu for lu in ast.letters}
    conserved_section_ids: set[str] = set()
    for sec in ast.sections:
        for item in sec.items:
            if item.kind == "blocks":
                conserved_section_ids.update(b.block_id for b in item.blocks)
            elif item.kind == "letter" and item.letter_id in letters_by_id:
                lu = letters_by_id[str(item.letter_id)]
                lu_ids = {b.block_id for b in lu.blocks}
                item_ids = {b.block_id for b in item.blocks} if item.blocks else lu_ids
                conserved_section_ids.update(lu_ids & item_ids)

    sec_claimed_fn_ids = {fid for sec in ast.sections for fid in sec.footnote_ids}
    conserved_fn_block_ids = {
        bid
        for fid, fn in ast.footnotes.items()
        if fid in sec_claimed_fn_ids
        for bid in fn.block_ids
    }

    findings: list[ValidationFinding] = []
    for page in ast.pages:
        if page.failed_closed:
            continue
        for blk in page.blocks:
            if blk.type in FURNITURE_BLOCK_TYPES:
                continue
            if blk.type == "footnote":
                if blk.block_id not in conserved_fn_block_ids:
                    findings.append(
                        ValidationFinding(
                            code="source_block_dropped",
                            severity="blocker",
                            page_number=page.page_number,
                            block_id=blk.block_id,
                            message=f"Footnote block {blk.block_id} on page {page.page_number} is missing from CollectionAST footnotes/sections.",
                            evidence={"block_type": blk.type},
                        )
                    )
            elif blk.block_id not in conserved_section_ids:
                findings.append(
                    ValidationFinding(
                        code="source_block_dropped",
                        severity="blocker",
                        page_number=page.page_number,
                        block_id=blk.block_id,
                        message=f"Content block {blk.block_id} ({blk.type}) on page {page.page_number} was dropped from CollectionAST sections.",
                        evidence={"block_type": blk.type},
                    )
                )
    return findings


def _check_single_text_corruption(
    text: str,
    page_number: int,
    block_id: str | None,
) -> list[ValidationFinding]:
    out: list[ValidationFinding] = []
    ctrl = _CONTROL_CHAR_RE.search(text)
    if ctrl:
        out.append(
            ValidationFinding(
                code="control_char_or_mojibake",
                severity="blocker",
                page_number=page_number,
                block_id=block_id,
                message=f"Illegal control character U+{ord(ctrl.group(0)):04X} found in block {block_id}.",
                evidence={"kind": "control_char", "codepoint": f"U+{ord(ctrl.group(0)):04X}"},
            )
        )
    if "\ufffd" in text:
        out.append(
            ValidationFinding(
                code="control_char_or_mojibake",
                severity="blocker",
                page_number=page_number,
                block_id=block_id,
                message=f"Unicode replacement character U+FFFD (mojibake) found in block {block_id}.",
                evidence={"kind": "replacement_char"},
            )
        )
    pua = _PUA_CHAR_RE.search(text)
    if pua:
        out.append(
            ValidationFinding(
                code="control_char_or_mojibake",
                severity="blocker",
                page_number=page_number,
                block_id=block_id,
                message=f"Private Use Area glyph U+{ord(pua.group(0)):04X} found in block {block_id}.",
                evidence={"kind": "private_use_area"},
            )
        )
    leak = _SCANLINE_LEAK_RE.search(text)
    if leak:
        out.append(
            ValidationFinding(
                code="control_char_or_mojibake",
                severity="blocker",
                page_number=page_number,
                block_id=block_id,
                message=f"Leaked scanline prefix {leak.group(0)!r} found in block {block_id}.",
                evidence={"kind": "leaked_scanline_prefix", "token": leak.group(0)},
            )
        )
    return out


def control_char_or_mojibake(ast: CollectionAST) -> list[ValidationFinding]:
    """Flag NUL/C0/C1 control chars, U+FFFD replacement chars, PUA glyphs, and leaked [L01] prefixes."""
    findings: list[ValidationFinding] = []
    seen_blocks: set[str] = set()
    all_blocks: list[BlockIR] = [
        *(b for p in ast.pages for b in p.blocks),
        *(b for s in ast.sections for it in s.items for b in it.blocks),
        *(b for lu in ast.letters for b in lu.blocks),
    ]
    for blk in all_blocks:
        if blk.block_id in seen_blocks:
            continue
        seen_blocks.add(blk.block_id)
        combined = f"{blk.tagged_text}\n{blk.plain_text}"
        findings.extend(_check_single_text_corruption(combined, blk.page_number, blk.block_id))

    for fn in ast.footnotes.values():
        bid = fn.block_ids[0] if fn.block_ids else None
        findings.extend(_check_single_text_corruption(f"{fn.tagged_text}\n{fn.plain_text}", fn.origin_page, bid))
    return findings


def furniture_leak(
    ast: CollectionAST,
    *,
    confirmed_block_ids: set[str] | None = None,
) -> list[ValidationFinding]:
    """Flag bare page numbers or repeated running headers leaked as body blocks."""
    confirmed = confirmed_block_ids or set()
    findings: list[ValidationFinding] = []
    top_headers: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for page in ast.pages:
        if page.failed_closed:
            continue
        content = [b for b in page.blocks if b.type not in FURNITURE_BLOCK_TYPES]
        if not content:
            continue
        for idx, blk in enumerate(content):
            if blk.type != "body":
                continue
            txt = _strip_folio_prefix(blk.plain_text)
            y0 = blk.bbox[1] if len(blk.bbox) >= 4 else 500
            y1 = blk.bbox[3] if len(blk.bbox) >= 4 else 500
            at_edge = idx == 0 or idx == len(content) - 1 or y0 <= 115 or y1 >= 885
            if at_edge and _BARE_PAGE_NUM_RE.match(txt):
                extreme = y0 <= 90 or y1 >= 910
                sev = "info" if blk.block_id in confirmed else ("blocker" if extreme else "warning")
                findings.append(
                    ValidationFinding(
                        code="furniture_leak",
                        severity=sev,
                        page_number=page.page_number,
                        block_id=blk.block_id,
                        message=f"Bare folio numeral {txt!r} leaked as body text at page margin.",
                        evidence={"text": txt, "bbox": blk.bbox},
                    )
                )
            if idx == 0 and y0 <= 120 and "\n" not in txt and 3 <= len(txt) <= 80:
                top_headers[txt.casefold()].append((page.page_number, blk.block_id))

    for norm_hdr, locs in top_headers.items():
        if len(locs) >= 3:
            for pnum, bid in locs:
                sev = "info" if bid in confirmed else "warning"
                findings.append(
                    ValidationFinding(
                        code="furniture_leak",
                        severity=sev,
                        page_number=pnum,
                        block_id=bid,
                        message=f"Repeated top-of-page header ({len(locs)}x) leaked as body block.",
                        evidence={"normalized_header": norm_hdr, "occurrences": len(locs)},
                    )
                )
    return findings


def _page_margin_labels(blocks: list[BlockIR]) -> set[str]:
    return {normalize_note_label(b.note_label) for b in blocks if b.type == "margin_note" and b.note_label}


def footnote_parity(
    ast: CollectionAST,
    confirmed_block_ids: set[str] | None = None,
) -> list[ValidationFinding]:
    """Verify inline callout-to-footnote parity and dual-projection consistency."""
    confirmed = set(confirmed_block_ids or ())
    findings: list[ValidationFinding] = []
    notes_by_block: dict[str, list[str]] = defaultdict(list)
    for fn in ast.footnotes.values():
        for bid in fn.referenced_in_block_ids:
            notes_by_block[bid].append(normalize_note_label(fn.label))

    letter_by_block = {bid: lu.letter_id for lu in ast.letters for bid in lu.block_ids}
    for page in ast.pages:
        if page.failed_closed:
            continue
        page_margin_labels = _page_margin_labels(page.blocks)
        for blk in page.blocks:
            if blk.type in FURNITURE_BLOCK_TYPES or blk.type in {"footnote", "toc_entry"}:
                continue
            matched_labels = set(notes_by_block.get(blk.block_id, [])) | page_margin_labels
            for raw_marker in extract_inline_markers(blk.tagged_text):
                m_norm = normalize_note_label(raw_marker)
                if m_norm not in matched_labels:
                    findings.append(
                        ValidationFinding(
                            code="unmatched_callout",
                            severity="info" if blk.block_id in confirmed else "warning",
                            page_number=page.page_number,
                            block_id=blk.block_id,
                            letter_id=letter_by_block.get(blk.block_id),
                            message=f"Inline callout {raw_marker!r} on page {page.page_number} has no matching footnote.",
                            evidence={"marker": raw_marker},
                        )
                    )

    for fn in ast.footnotes.values():
        if fn.is_orphan or not fn.referenced_in_block_ids:
            folio_disp = fn.origin_folio or str(fn.origin_page)
            fn_bid = fn.block_ids[0] if fn.block_ids else None
            is_confirmed = any(bid in confirmed for bid in fn.block_ids)
            findings.append(
                ValidationFinding(
                    code="unlinked_footnote",
                    severity="info" if is_confirmed else "warning",
                    page_number=fn.origin_page,
                    block_id=fn_bid,
                    message=f"[Unlinked note, p. {folio_disp}] Footnote {fn.footnote_id} ({fn.label or 'unlabelled'}) has no matching inline callout.",
                    evidence={"footnote_id": fn.footnote_id, "label": fn.label},
                )
            )

    sec_by_id = {s.section_id: s for s in ast.sections}
    for lu in ast.letters:
        parent_sec = sec_by_id.get(lu.section_id)
        parent_fids = set(parent_sec.footnote_ids) if parent_sec else set()
        for fid in lu.footnote_ids:
            if fid not in ast.footnotes or fid not in parent_fids:
                findings.append(
                    ValidationFinding(
                        code="broken_footnote_projection",
                        severity="blocker",
                        page_number=lu.start_page,
                        letter_id=lu.letter_id,
                        message=f"Letter {lu.letter_id} references footnote {fid} missing from section {lu.section_id} or AST footnotes.",
                        evidence={"footnote_id": fid},
                    )
                )
    return findings


def epistolary_boundary_anomalies(ast: CollectionAST) -> list[ValidationFinding]:
    """Flag ambiguous boundaries, oversized (>25p) unheaded letters, and multiple signatures without enclosure."""
    findings: list[ValidationFinding] = []
    is_sparse_slice = bool(ast.total_pages and len(ast.pages) < ast.total_pages)
    for lu in ast.letters:
        first_bid = lu.block_ids[0] if lu.block_ids else None
        if lu.boundary_confidence == "ambiguous":
            findings.append(
                ValidationFinding(
                    code="ambiguous_letter_boundary",
                    severity="warning",
                    page_number=lu.start_page,
                    block_id=first_bid,
                    letter_id=lu.letter_id,
                    message=f"Letter {lu.letter_id} opened without explicit structural header/salutation; verify boundary on scan.",
                )
            )
        extracted_pages_in_letter = len({b.page_number for b in lu.blocks})
        span_pages = extracted_pages_in_letter if is_sparse_slice else (lu.end_page - lu.start_page + 1)
        if (
            span_pages > 25
            and lu.boundary_confidence not in {"human_mapped", "adjudicated"}
            and not any(b.type == "heading" for b in lu.blocks)
        ):
            findings.append(
                ValidationFinding(
                    code="oversized_letter_unit",
                    severity="warning",
                    page_number=lu.start_page,
                    block_id=first_bid,
                    letter_id=lu.letter_id,
                    message=f"Letter {lu.letter_id} spans {span_pages} pages ({lu.start_page}–{lu.end_page}) without internal headings.",
                    evidence={"span_pages": span_pages},
                )
            )
        first_ps_idx = next((i for i, b in enumerate(lu.blocks) if b.type == "postscript"), len(lu.blocks))
        sig_blocks = [b for b in lu.blocks[:first_ps_idx] if b.type == "signature"]
        if len(sig_blocks) >= 2 and not lu.metadata.has_enclosure and lu.boundary_confidence != "human_mapped":
            findings.append(
                ValidationFinding(
                    code="multiple_signatures_without_enclosure",
                    severity="warning",
                    page_number=sig_blocks[1].page_number,
                    block_id=sig_blocks[1].block_id,
                    letter_id=lu.letter_id,
                    message=(
                        f"Letter {lu.letter_id} contains {len(sig_blocks)} signature blocks without an enclosure transition; "
                        "adjudicate potential missed split."
                    ),
                    evidence={"signature_block_ids": [b.block_id for b in sig_blocks]},
                )
            )
    return findings


def _strip_combining_ascii(token: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", token) if not unicodedata.combining(ch))


def _detect_diacritic_drift(
    content_blocks: list[BlockIR],
    confirmed: set[str],
) -> list[ValidationFinding]:
    by_stem: dict[str, Counter[str]] = defaultdict(Counter)
    locs: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)

    for blk in content_blocks:
        for tok in _WORD_TOKEN_RE.findall(_strip_folio_prefix(blk.plain_text)):
            stem = _strip_combining_ascii(tok).casefold()
            if len(stem) < 4:
                continue
            by_stem[stem][tok] += 1
            locs[(stem, tok)].append((blk.page_number, blk.block_id))

    findings: list[ValidationFinding] = []
    for stem, counts in by_stem.items():
        accented = {form: cnt for form, cnt in counts.items() if not form.isascii()}
        bare = {form: cnt for form, cnt in counts.items() if form.isascii()}
        if len(accented) >= 2:
            forms_summary = ", ".join(f"{k} ({v}x)" for k, v in counts.most_common(4))
            first_form = next(iter(accented))
            pnum, bid = locs[(stem, first_form)][0]
            findings.append(
                ValidationFinding(
                    code="diacritic_drift_advisory",
                    severity="info",
                    page_number=pnum,
                    block_id=bid,
                    message=f"Multiple accented spellings coexist for stem {stem!r}: {forms_summary}; preserve printer ink.",
                    evidence={"forms": dict(counts)},
                )
            )
        if not accented or not bare:
            continue
        acc_total = sum(accented.values())
        bare_total = sum(bare.values())
        sample_form = next(iter(accented))
        if sample_form[0].isupper() and acc_total >= 3 and bare_total <= 2 and acc_total >= bare_total * 2:
            dom = max(accented.items(), key=lambda kv: kv[1])[0]
            for b_form in bare:
                for pnum, bid in locs[(stem, b_form)]:
                    sev = "info" if bid in confirmed else "warning"
                    findings.append(
                        ValidationFinding(
                            code="diacritic_drift_advisory",
                            severity=sev,
                            page_number=pnum,
                            block_id=bid,
                            message=f"Proper noun {b_form!r} lacks diacritics here while {dom!r} appears {acc_total}x in volume.",
                            evidence={"bare_form": b_form, "dominant_form": dom},
                        )
                    )
    return findings


def diacritic_and_script_integrity(
    ast: CollectionAST,
    *,
    confirmed_block_ids: set[str] | None = None,
) -> list[ValidationFinding]:
    """Flag uncomposed NFD sequences, intra-book diacritic drift, unclear spans, and rare chars."""
    confirmed = confirmed_block_ids or set()
    findings: list[ValidationFinding] = []
    content_blocks: list[BlockIR] = []
    char_counts: Counter[str] = Counter()
    char_first_loc: dict[str, tuple[int, str]] = {}

    for page in ast.pages:
        if page.failed_closed:
            continue
        for blk in page.blocks:
            if blk.type in FURNITURE_BLOCK_TYPES:
                continue
            content_blocks.append(blk)
            if unicodedata.normalize("NFC", blk.plain_text) != blk.plain_text or (
                unicodedata.normalize("NFC", blk.tagged_text) != blk.tagged_text
            ):
                findings.append(
                    ValidationFinding(
                        code="uncomposed_nfd_sequence",
                        severity="warning",
                        page_number=page.page_number,
                        block_id=blk.block_id,
                        message=f"Block {blk.block_id} contains uncomposed NFD combining characters; expected canonical NFC.",
                    )
                )
            for sp in blk.spans:
                if sp.tag == "unclear":
                    sev = "info" if blk.block_id in confirmed else "warning"
                    snippet = blk.plain_text[sp.start : sp.end]
                    findings.append(
                        ValidationFinding(
                            code="unclear_span",
                            severity=sev,
                            page_number=page.page_number,
                            block_id=blk.block_id,
                            message=f"Uncertain transcription <unclear>{snippet}</unclear> requires scan verification.",
                            evidence={"text": snippet},
                        )
                    )
            for ch in blk.plain_text:
                if not ch.isascii() and ch.isalpha():
                    char_counts[ch] += 1
                    char_first_loc.setdefault(ch, (page.page_number, blk.block_id))

    findings.extend(_detect_diacritic_drift(content_blocks, confirmed))

    rare_emitted = 0
    for ch, cnt in char_counts.items():
        if cnt == 1 and rare_emitted < 10:
            pnum, bid = char_first_loc[ch]
            rare_emitted += 1
            findings.append(
                ValidationFinding(
                    code="rare_character_advisory",
                    severity="info",
                    page_number=pnum,
                    block_id=bid,
                    message=f"Single-occurrence character {ch!r} (U+{ord(ch):04X}) on page {pnum}; optional scan spot-check.",
                    evidence={"char": ch, "codepoint": f"U+{ord(ch):04X}"},
                )
            )
    return findings


def run_ast_validators(
    ast: CollectionAST,
    *,
    proof_ledger: dict[str, Any] | None = None,
) -> list[ValidationFinding]:
    """Run all two-tier archival validators on `ast`, updating `ast.findings` in place."""
    confirmed = _collect_confirmed_block_ids(ast, proof_ledger)
    human_confirmed = _collect_confirmed_block_ids(ast, proof_ledger, human_only=True)
    findings: list[ValidationFinding] = [
        ValidationFinding(
            code="failed_closed_page",
            severity="blocker",
            page_number=p.page_number,
            message=(
                f"[Page {p.page_number} unavailable — inspect source scan] "
                f"({p.failure_reason or 'extraction failed closed'})"
            ),
            evidence={"failure_reason": p.failure_reason or "failed_closed"},
        )
        for p in ast.pages
        if p.failed_closed
    ]
    findings.extend(source_block_coverage(ast))
    findings.extend(control_char_or_mojibake(ast))
    findings.extend(furniture_leak(ast, confirmed_block_ids=confirmed))
    findings.extend(footnote_parity(ast, confirmed_block_ids=human_confirmed))
    findings.extend(epistolary_boundary_anomalies(ast))
    findings.extend(diacritic_and_script_integrity(ast, confirmed_block_ids=confirmed))
    ast.findings = findings
    return findings
