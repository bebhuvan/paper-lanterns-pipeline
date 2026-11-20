"""PDF optical preflight, diplomatic wire normalization, line-wrap resolution, and extraction pipeline."""

from lanterns.extraction.hotspot_verifier import collect_epistolary_hotspots, verify_hotspots
from lanterns.extraction.line_wraps import (
    count_corpus_word_forms,
    judge_hyphen_wrap,
    merge_hyphenated_tagged_pair,
    resolve_page_ir_line_wraps,
)
from lanterns.extraction.normalize import (
    canonicalize_footnote_label,
    is_binder_signature,
    normalize_wire_to_page_ir,
    solve_monotonic_folios,
    sort_two_column_blocks,
    split_note_label_markers,
    strip_scanline_markers,
)
from lanterns.extraction.pdf_ingest import (
    PagePreflight,
    compute_optimal_render_zoom,
    compute_pdf_sha256,
    crop_normalized_bbox,
    inspect_and_render_page,
    inspect_page_optical_dpi,
    measure_ink_density,
    pdf_page_count,
    triage_pdf,
)
from lanterns.extraction.pipeline import (
    load_run_pages,
    parse_pages_spec,
    run_extraction,
    triage_book_pdf,
)

__all__ = [
    "PagePreflight",
    "canonicalize_footnote_label",
    "collect_epistolary_hotspots",
    "compute_optimal_render_zoom",
    "compute_pdf_sha256",
    "count_corpus_word_forms",
    "crop_normalized_bbox",
    "inspect_and_render_page",
    "inspect_page_optical_dpi",
    "is_binder_signature",
    "judge_hyphen_wrap",
    "load_run_pages",
    "measure_ink_density",
    "merge_hyphenated_tagged_pair",
    "normalize_wire_to_page_ir",
    "parse_pages_spec",
    "pdf_page_count",
    "resolve_page_ir_line_wraps",
    "run_extraction",
    "solve_monotonic_folios",
    "sort_two_column_blocks",
    "split_note_label_markers",
    "strip_scanline_markers",
    "triage_book_pdf",
    "triage_pdf",
    "verify_hotspots",
]
