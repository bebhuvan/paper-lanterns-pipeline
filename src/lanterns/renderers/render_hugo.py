"""Hugo content bundle renderer for the Paper Lanterns website.

ROLE: Project a compiled `CollectionAST` into Hugo-ready content: one book `_index.md` and one page per letter.
INPUTS: CollectionAST, typed `HugoEditionMeta` (book.toml credits, life periods, download files).
OUTPUTS: `library/<collection>/<book_id>/hugo/` with JSON-front-matter Markdown files and `manifest.json`.
INVARIANT: Deterministic and offline. Text travels as escaped-by-template runs (never pre-rendered HTML),
so the site never inserts raw HTML. Zero book_id branching; routing only on block type and inline tags.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from itertools import groupby
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lanterns.core.atomic import atomic_write_json, atomic_write_text
from lanterns.core.display_text import clip_words, letter_page_title, opening_headline
from lanterns.core.edition_overlay import EditionOverlay, LifePeriod
from lanterns.core.hashes import sha256_file
from lanterns.core.models import BlockIR, CollectionAST, FootnoteItem, LetterUnit, RubricBreakdown
from lanterns.core.tags import (
    fn_anchor_id,
    normalize_note_label,
    split_tagged_lines_balanced,
    strip_constrained_tags,
    strip_footnote_callouts_plain,
    tokenize_tagged_text,
)
from lanterns.renderers.html_utils import footnotes_by_block_map

HUGO_RENDERER_VERSION = "1.0.0"
SCAN_URL_BASE = "https://archive.org/download"

_ISO_DATE_RE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_ROMAN_MONTHS = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII")
_STYLE_CLASSES = {"i": "i", "em": "i", "b": "b", "strong": "b", "sc": "sc", "unclear": "unclear"}
_ELEMENT_TAGS = {"sup": "sup", "sub": "sub"}
# One character per scan leaf for the page strip: L letter, n narrative between letters, f front, a appendix, i index/back.
_SECTION_PAGE_KIND = {
    "frontmatter": "f",
    "chapter": "n",
    "epistolary_part": "n",
    "appendix": "a",
    "index": "i",
    "backmatter": "i",
}
_DOWNLOAD_FORMATS = (
    ("epub", "epub", ".epub"),
    ("markdown", "markdown", ".md"),
    ("tei", "tei", ".tei.xml"),
)


class HugoRenderError(ValueError):
    """Raised when edition data cannot be projected without guessing."""


@dataclass(frozen=True)
class DownloadFile:
    format: str
    source_rel: str
    file: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class HugoEditionMeta:
    subtitle: str = ""
    imprint: str = ""
    authors: tuple[str, ...] = ()
    compiled_at: str = ""
    overlay: EditionOverlay = field(default_factory=EditionOverlay)
    downloads: tuple[DownloadFile, ...] = ()
    source_ast_sha256: str = ""


def format_letter_date(date_iso: str | None) -> dict[str, str]:
    """Return `{iso, display, short}` for full or partial ISO dates; empty strings when undated."""
    if not date_iso:
        return {"iso": "", "display": "", "short": ""}
    m = _ISO_DATE_RE.fullmatch(date_iso.strip())
    if m is None:
        raise HugoRenderError(f"Letter date {date_iso!r} is not ISO 8601 (YYYY, YYYY-MM or YYYY-MM-DD)")
    year, month, day = m.group(1), m.group(2), m.group(3)
    if month is None:
        return {"iso": year, "display": year, "short": year}
    mi = int(month) - 1
    if not 0 <= mi < 12 or (day is not None and not 1 <= int(day) <= 31):
        raise HugoRenderError(f"Letter date {date_iso!r} has an impossible month or day")
    head = f"{int(day)} " if day else ""
    short_head = f"{int(day)} · " if day else ""
    return {
        "iso": m.group(0),
        "display": f"{head}{_MONTH_NAMES[mi]} {year}",
        "short": f"{short_head}{_ROMAN_MONTHS[mi]} · {year}",
    }


def _runs_for_line(line: str, callouts: dict[str, FootnoteItem]) -> list[dict[str, str]]:
    runs: list[dict[str, str]] = []
    stack: list[str] = []
    for kind, val in tokenize_tagged_text(line):
        if kind == "open":
            stack.append(val)
        elif kind == "close":
            stack.pop()
        else:
            runs.append(_make_run(val, stack, callouts))
    return runs


def _make_run(text: str, stack: list[str], callouts: dict[str, FootnoteItem]) -> dict[str, str]:
    run: dict[str, str] = {"t": text}
    classes = sorted({_STYLE_CLASSES[t] for t in stack if t in _STYLE_CLASSES})
    elements = [_ELEMENT_TAGS[t] for t in stack if t in _ELEMENT_TAGS]
    if classes:
        run["c"] = " ".join(classes)
    # Legacy runs tag callouts as <sup>; like compiler.py, link a <sup> only when this block attests that note.
    fn = callouts.get(normalize_note_label(text)) if ("fnref" in stack or "sup" in stack) else None
    if fn is not None:
        run["tag"] = "sup"
        run["fn"] = fn_anchor_id(fn.footnote_id)
    elif "fnref" in stack:
        run["tag"] = "sup"
    elif elements:
        run["tag"] = elements[-1]
    return run


def tagged_to_lines(tagged_text: str, *, keep_line_breaks: bool, callouts: dict[str, FootnoteItem] | None = None) -> list[list[dict[str, str]]]:
    """Split diplomatic tagged text into lines of style runs the Hugo templates escape and wrap."""
    lookup = callouts or {}
    lines = split_tagged_lines_balanced(tagged_text) if keep_line_breaks else [tagged_text]
    return [_runs_for_line(line, lookup) for line in lines]


def _block_payload(block: BlockIR, callouts: dict[str, FootnoteItem]) -> dict[str, Any]:
    return {
        "id": block.block_id,
        "type": block.type,
        "align": block.alignment,
        "page": block.page_number,
        "leaf": block.page_number - 1,
        "folio": block.printed_folio or "",
        "bbox": list(block.bbox),
        "lines": tagged_to_lines(block.tagged_text, keep_line_breaks=block.line_breaks_preserved, callouts=callouts),
    }


def _letter_heading(letter: LetterUnit) -> str:
    header_lines = [strip_footnote_callouts_plain(x) for x in split_tagged_lines_balanced(letter.metadata.header_tagged or "")]
    parts = [x for x in header_lines if x]
    return " · ".join(parts) or f"Letter {letter.sequence_index}"


def _period_for(date_iso: str, periods: tuple[LifePeriod, ...]) -> str:
    for period in periods:
        if date_iso and period.start <= date_iso < period.end:
            return period.label
    return ""


def _letter_footnotes(letter: LetterUnit, ast: CollectionAST) -> list[dict[str, Any]]:
    notes = []
    for fn_id in letter.footnote_ids:
        fn = ast.footnotes[fn_id]
        notes.append(
            {
                "id": fn_anchor_id(fn.footnote_id),
                "label": fn.label,
                "page": fn.origin_page,
                "lines": tagged_to_lines(fn.tagged_text, keep_line_breaks=True),
            }
        )
    return notes


def _full_sender(letter: LetterUnit, ast: CollectionAST, overlay: EditionOverlay) -> str:
    # The overlay's default writer applies only inside the volume's own letters section, never to appendices.
    in_main_part = any(s.section_id == letter.section_id and s.kind == "epistolary_part" for s in ast.sections)
    return overlay.full_name(letter.metadata.sender_display) or (overlay.default_sender if in_main_part else "")


def build_letter_front_matter(letter: LetterUnit, ast: CollectionAST, meta: HugoEditionMeta) -> dict[str, Any]:
    md = letter.metadata
    overlay = meta.overlay
    opening = next(
        (strip_footnote_callouts_plain(b.tagged_text) for b in letter.blocks if b.type == "body"), md.incipit
    )
    heading = _letter_heading(letter)
    sender_full = _full_sender(letter, ast, overlay)
    recipient_full = overlay.full_name(md.recipient_display)
    by_block = footnotes_by_block_map(ast.footnotes, set(letter.footnote_ids))
    blocks = [
        _block_payload(b, {normalize_note_label(fn.label): fn for fn in by_block.get(b.block_id, [])})
        for b in letter.blocks
    ]
    date = format_letter_date(md.date_iso)
    pages = sorted({b["page"] for b in blocks} | {letter.start_page})
    folio_by_page = {p.page_number: p.printed_folio or "" for p in ast.pages}
    return {
        "title": opening_headline(opening) or heading,
        "heading": heading,
        "page_title": letter_page_title(sender_full, recipient_full, date["display"]),
        "layout": "letter",
        "weight": letter.sequence_index,
        "description": clip_words(opening, 155),
        "book_id": ast.book_id,
        "letter_id": letter.letter_id,
        # Survives a re-split that renumbers letters: the first printed block of the letter.
        "stable_id": (letter.block_ids or [letter.letter_id])[0],
        "anchor_text": clip_words(opening, 80),
        "pipeline_slug": letter.slug,
        "number": md.letter_number_label or "",
        "sequence": letter.sequence_index,
        "letter_count": len(ast.letters),
        "section_id": letter.section_id,
        "letter_date": date,
        "period": _period_for(date["iso"], overlay.periods),
        "place": md.place or "",
        "sender": md.sender_display or "",
        "recipient": md.recipient_display or "",
        "sender_full": sender_full,
        "recipient_full": recipient_full,
        "salutation": strip_footnote_callouts_plain(md.salutation_tagged or ""),
        "incipit": md.incipit,
        "start_page": letter.start_page,
        "end_page": letter.end_page,
        "start_folio": letter.start_folio or "",
        "end_folio": letter.end_folio or "",
        "pages": [{"page": p, "leaf": p - 1, "folio": folio_by_page.get(p, "")} for p in pages],
        "archive_id": ast.archive_id or "",
        "boundary_confidence": letter.boundary_confidence,
        "blocks": blocks,
        "footnotes": _letter_footnotes(letter, ast),
    }


def page_kind_strip(ast: CollectionAST) -> str:
    """One character per scan leaf: L letter, n narrative, f front, a appendix, i index/back, . unassigned."""
    kinds = ["."] * ast.total_pages
    for sec in ast.sections:
        for pnum in range(sec.start_page, min(sec.end_page, ast.total_pages) + 1):
            kinds[pnum - 1] = _SECTION_PAGE_KIND.get(sec.kind, ".")
    for letter in ast.letters:
        for pnum in range(letter.start_page, min(letter.end_page, ast.total_pages) + 1):
            kinds[pnum - 1] = "L"
    return "".join(kinds)


def _first_page_with(ast: CollectionAST, required: set[str]) -> int | None:
    front = [s for s in ast.sections if s.kind == "frontmatter"]
    last_front = front[0].end_page if front else 0
    for page in ast.pages:
        if page.page_number <= last_front and required <= {b.type for b in page.blocks}:
            return page.page_number
    return None


def _plate(ast: CollectionAST) -> dict[str, Any]:
    pnum = _first_page_with(ast, {"illustration", "caption"})
    if pnum is None:
        return {}
    page = next(p for p in ast.pages if p.page_number == pnum)
    caption = next(b for b in page.blocks if b.type == "caption")
    return {
        "page": pnum,
        "leaf": pnum - 1,
        "caption": strip_constrained_tags(caption.tagged_text).strip(),
    }


def build_book_front_matter(ast: CollectionAST, meta: HugoEditionMeta) -> dict[str, Any]:
    dates = [format_letter_date(lt.metadata.date_iso)["iso"] for lt in ast.letters]
    periods = meta.overlay.periods
    period_counts = Counter(_period_for(d, periods) for d in dates)
    year_counts = Counter(d[:4] for d in dates if d)
    title_page = _first_page_with(ast, {"imprint"})
    proof = ast.proof_summary
    return {
        "title": ast.title,
        "layout": "book",
        "book_id": ast.book_id,
        "collection": ast.collection,
        "subtitle": meta.subtitle,
        "imprint": meta.imprint,
        "authors": list(meta.authors),
        "translators": list(meta.overlay.translators),
        "publisher": meta.overlay.publisher,
        "default_sender": meta.overlay.default_sender,
        "default_sender_source": meta.overlay.default_sender_source,
        "author_or_editor": ast.author_or_editor,
        "year": ast.year,
        "archive_id": ast.archive_id or "",
        "language": ast.language,
        "text_direction": ast.text_direction,
        "counts": {
            "letters": len(ast.letters),
            "notes": len(ast.footnotes),
            "pages": ast.total_pages,
            "dated_letters": sum(1 for d in dates if d),
        },
        "provenance_tier": ast.rubric.provenance_tier if ast.rubric else "",
        "quality": _quality(ast.rubric),
        "proof": {
            "total_pages": int(proof.get("total_pages", ast.total_pages)),
            "p1_proofed": int(proof.get("p1_proofed", 0)),
            "verified": int(proof.get("verified", 0)),
            "machine_reread": int(proof.get("machine_reread", 0)),
        },
        "page_kinds": page_kind_strip(ast),
        # The same strip run-length encoded ([kind, leaves], ...) for small drawings such as generated covers.
        "page_runs": [[kind, len(list(run))] for kind, run in groupby(page_kind_strip(ast))],
        "parts": [
            {"label": s.title_plain, "kind": s.kind, "start": s.start_page, "end": s.end_page} for s in ast.sections
        ],
        "year_counts": [[int(y), n] for y, n in sorted(year_counts.items())],
        "periods": [
            {"label": p.label, "start": p.start, "end": p.end, "source": p.source, "count": period_counts[p.label]}
            for p in periods
        ],
        "unperiodised_count": period_counts[""],
        "frontispiece": _plate(ast),
        "title_page": {"page": title_page, "leaf": title_page - 1} if title_page else {},
        "downloads": [{"format": d.format, "file": d.file, "bytes": d.bytes, "sha256": d.sha256} for d in meta.downloads],
        "scan_base": f"{SCAN_URL_BASE}/{ast.archive_id}/page/" if ast.archive_id else "",
        "provenance": {
            "active_run_id": ast.active_run_id,
            "compiled_at": meta.compiled_at,
            "source_ast_sha256": meta.source_ast_sha256,
            "hugo_renderer_version": HUGO_RENDERER_VERSION,
        },
    }


def dump_front_matter(front_matter: dict[str, Any]) -> str:
    """Serialize JSON front matter (Hugo reads a file starting with `{` as JSON) with an empty body."""
    return json.dumps(front_matter, ensure_ascii=False, indent=1) + "\n"


# Each pillar's score next to its maximum, so the site can say "26 of 30" without knowing the rubric.
_RUBRIC_PILLARS = (
    ("text", "text_and_diacritic_fidelity", 30.0),
    ("structure", "epistolary_structure_and_layout", 25.0),
    ("notes", "apparatus_and_footnote_integrity", 20.0),
    ("completeness", "completeness_and_page_coverage", 15.0),
    ("files", "compiled_output_validity", 10.0),
)


def _quality(rubric: RubricBreakdown | None) -> dict[str, Any] | None:
    """The 100-point rubric score for readers; None when the edition was never scored."""
    if rubric is None:
        return None
    return {
        "score": rubric.total_score,
        "out_of": sum(m for _, _, m in _RUBRIC_PILLARS),
        "pillars": [{"key": k, "score": getattr(rubric, f), "out_of": m} for k, f, m in _RUBRIC_PILLARS],
        "flagged": rubric.hotspot_count,
        "blockers": rubric.blocker_count,
    }


def build_hugo_bundle(ast: CollectionAST, meta: HugoEditionMeta) -> dict[str, str]:
    """Pure projection: relative path -> file text for the whole book bundle."""
    files = {"_index.md": dump_front_matter(build_book_front_matter(ast, meta))}
    for letter in ast.letters:
        # Letter pages are addressed by their sequence number (/collections/<book_id>/<n>/), frozen once published.
        name = f"{letter.sequence_index}.md"
        if name in files:
            raise HugoRenderError(f"Duplicate letter number {letter.sequence_index} would overwrite another page")
        files[name] = dump_front_matter(build_letter_front_matter(letter, ast, meta))
    return files


def collect_download_files(edition_dir: Path, book_id: str) -> tuple[DownloadFile, ...]:
    """Find the compiled EPUB, Markdown and TEI files; a missing one stops the run."""
    found = []
    for fmt, sub, suffix in _DOWNLOAD_FORMATS:
        src = edition_dir / sub / f"{book_id}{suffix}"
        if not src.is_file():
            raise HugoRenderError(f"Expected compiled {fmt} file at {src}; run `lanterns compile` first")
        found.append(
            DownloadFile(
                format=fmt,
                source_rel=src.relative_to(edition_dir).as_posix(),
                file=src.name,
                bytes=src.stat().st_size,
                sha256=sha256_file(src),
            )
        )
    return tuple(found)


def render_hugo_edition(ast: CollectionAST, edition_dir: str | Path, meta: HugoEditionMeta) -> dict[str, Any]:
    """Write `<edition_dir>/hugo/` from scratch (stale pages never survive) plus `manifest.json` for the site sync."""
    ed_root = Path(edition_dir)
    out_dir = ed_root / "hugo"
    files = build_hugo_bundle(ast, meta)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    for rel, text in files.items():
        atomic_write_text(out_dir / rel, text)
    manifest = {
        "book_id": ast.book_id,
        "collection": ast.collection,
        "hugo_renderer_version": HUGO_RENDERER_VERSION,
        "source_ast_sha256": meta.source_ast_sha256,
        "pages": sorted(files),
        "downloads": [
            {"from": d.source_rel, "to": d.file, "bytes": d.bytes, "sha256": d.sha256} for d in meta.downloads
        ],
    }
    atomic_write_json(out_dir / "manifest.json", manifest)
    return {"hugo_dir": str(out_dir), "page_count": len(files), "letter_count": len(ast.letters)}
