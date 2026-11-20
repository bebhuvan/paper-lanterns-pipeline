"""ROLE: Public API surface for Paper Lanterns v2 archival validation, rubric, and AST guard.
INPUTS: Re-exports from validators, compiled_gate, rubric, and architecture_guard.
OUTPUTS: Clean entry points for CLI, doctor, and test suites.
INVARIANT: Strictly acyclic (`core -> ... -> epistolary -> validation -> renderers -> cli`).
"""

from __future__ import annotations

from lanterns.validation.architecture_guard import (
    compute_function_complexity,
    evaluate_epistolary_exam,
    run_architecture_guard,
)
from lanterns.validation.compiled_gate import validate_compiled_edition
from lanterns.validation.rubric import score_collection_rubric
from lanterns.validation.validators import (
    control_char_or_mojibake,
    diacritic_and_script_integrity,
    epistolary_boundary_anomalies,
    footnote_parity,
    furniture_leak,
    run_ast_validators,
    source_block_coverage,
)

__all__ = [
    "compute_function_complexity",
    "control_char_or_mojibake",
    "diacritic_and_script_integrity",
    "epistolary_boundary_anomalies",
    "evaluate_epistolary_exam",
    "footnote_parity",
    "furniture_leak",
    "run_architecture_guard",
    "run_ast_validators",
    "score_collection_rubric",
    "source_block_coverage",
    "validate_compiled_edition",
]
