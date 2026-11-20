"""ROLE: Public API surface for the Paper Lanterns v2 epistolary compiler subsystem.
INPUTS: Re-exports from corrections, metadata, segmentation, and compiler.
OUTPUTS: Clean entry points for CLI, validation, renderers, and tests.
INVARIANT: Strictly acyclic (depends only on lanterns.core).
"""

from __future__ import annotations

from lanterns.epistolary.compiler import (
    compile_collection_ast,
    load_pages_from_run_dir,
    normalize_note_label,
    resolve_block_callout_targets,
)
from lanterns.epistolary.corrections import (
    apply_corrections_and_proof_overlay,
    compute_page_text_sha256,
    intake_proposal,
    is_human_protected_page,
    is_p2_human_verified_page,
    load_book_corrections,
    load_chapter_map,
    load_letter_map,
    load_proof_ledger,
    load_structure_overrides,
    make_boundary_anchor,
    rebase_boundary_anchor,
    record_machine_reread,
    record_page_proof,
    save_book_corrections,
    save_letter_map,
    save_proof_ledger,
)
from lanterns.epistolary.metadata import (
    build_letter_slug,
    detect_scripts_and_orthography,
    extract_epistolary_metadata,
)
from lanterns.epistolary.segmentation import segment_epistolary_collection

__all__ = [
    "apply_corrections_and_proof_overlay",
    "build_letter_slug",
    "compile_collection_ast",
    "compute_page_text_sha256",
    "detect_scripts_and_orthography",
    "extract_epistolary_metadata",
    "intake_proposal",
    "is_human_protected_page",
    "is_p2_human_verified_page",
    "load_book_corrections",
    "load_chapter_map",
    "load_letter_map",
    "load_pages_from_run_dir",
    "load_proof_ledger",
    "load_structure_overrides",
    "make_boundary_anchor",
    "normalize_note_label",
    "rebase_boundary_anchor",
    "record_machine_reread",
    "record_page_proof",
    "resolve_block_callout_targets",
    "save_book_corrections",
    "save_letter_map",
    "save_proof_ledger",
    "segment_epistolary_collection",
]
