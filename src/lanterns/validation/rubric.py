"""ROLE: 100-Point Epistolary Edition Quality Rubric scorer and 4-tier provenance classifier.
INPUTS: CollectionAST, optional proof_ledger, and optional compiled_gate_result.
OUTPUTS: RubricBreakdown (30+25+20+15+10 = 100 pts) and honest ProvenanceTier.
INVARIANT: Never inflates provenance tiers; source_certified requires 100% P1 != P2 human verification.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from lanterns.core.models import CollectionAST, ProvenanceTier, RubricBreakdown
from lanterns.epistolary.corrections import is_p2_human_verified_page


def _score_text_and_diacritics(
    warn_by_code: Counter[str],
    block_by_code: Counter[str],
    notes: list[str],
) -> float:
    score = 30.0
    corrupt = block_by_code.get("control_char_or_mojibake", 0)
    if corrupt:
        ded = min(30.0, 15.0 * corrupt)
        score -= ded
        notes.append(f"-{ded:.1f} text fidelity: {corrupt} control-char/mojibake/scanline leak(s)")
    nfd = warn_by_code.get("uncomposed_nfd_sequence", 0)
    if nfd:
        ded = min(12.0, 3.0 * nfd)
        score -= ded
        notes.append(f"-{ded:.1f} text fidelity: {nfd} uncomposed NFD sequence(s)")
    drift = warn_by_code.get("diacritic_drift_advisory", 0)
    if drift:
        ded = min(10.0, 2.0 * drift)
        score -= ded
        notes.append(f"-{ded:.1f} text fidelity: {drift} unresolved proper-noun diacritic drop(s)")
    unclear = warn_by_code.get("unclear_span", 0)
    if unclear:
        ded = min(5.0, 0.5 * unclear)
        score -= ded
        notes.append(f"-{ded:.1f} text fidelity: {unclear} unverified <unclear> span(s)")
    return max(0.0, round(score, 2))


def _score_epistolary_structure(
    ast: CollectionAST,
    warn_by_code: Counter[str],
    block_by_code: Counter[str],
    notes: list[str],
) -> float:
    score = 25.0
    furn_blk = block_by_code.get("furniture_leak", 0)
    furn_warn = warn_by_code.get("furniture_leak", 0)
    if furn_blk or furn_warn:
        ded = min(15.0, 5.0 * furn_blk + 2.0 * furn_warn)
        score -= ded
        notes.append(f"-{ded:.1f} structure: {furn_blk + furn_warn} furniture leak(s)")
    ambig = warn_by_code.get("ambiguous_letter_boundary", 0)
    if ambig:
        ded = min(10.0, 2.5 * ambig)
        score -= ded
        notes.append(f"-{ded:.1f} structure: {ambig} ambiguous letter boundary(ies)")
    multi_sig = warn_by_code.get("multiple_signatures_without_enclosure", 0)
    if multi_sig:
        ded = min(12.0, 3.0 * multi_sig)
        score -= ded
        notes.append(f"-{ded:.1f} structure: {multi_sig} multi-signature letter(s) without enclosure")
    oversized = warn_by_code.get("oversized_letter_unit", 0)
    if oversized:
        ded = min(8.0, 4.0 * oversized)
        score -= ded
        notes.append(f"-{ded:.1f} structure: {oversized} oversized (>25p) letter unit(s)")
    empty_secs = sum(1 for s in ast.sections if not s.items and not s.unavailable_pages)
    if empty_secs:
        ded = min(6.0, 3.0 * empty_secs)
        score -= ded
        notes.append(f"-{ded:.1f} structure: {empty_secs} empty section(s)")
    return max(0.0, round(score, 2))


def _score_apparatus(
    warn_by_code: Counter[str],
    block_by_code: Counter[str],
    notes: list[str],
) -> float:
    score = 20.0
    broken_proj = block_by_code.get("broken_footnote_projection", 0)
    if broken_proj:
        ded = min(20.0, 10.0 * broken_proj)
        score -= ded
        notes.append(f"-{ded:.1f} apparatus: {broken_proj} broken dual-footnote projection(s)")
    unmatched = warn_by_code.get("unmatched_callout", 0)
    if unmatched:
        ded = min(12.0, 2.5 * unmatched)
        score -= ded
        notes.append(f"-{ded:.1f} apparatus: {unmatched} unmatched inline callout(s)")
    unlinked = warn_by_code.get("unlinked_footnote", 0)
    if unlinked:
        ded = min(10.0, 2.5 * unlinked)
        score -= ded
        notes.append(f"-{ded:.1f} apparatus: {unlinked} unlinked orphan footnote(s)")
    return max(0.0, round(score, 2))


def _score_completeness(
    ast: CollectionAST,
    block_by_code: Counter[str],
    notes: list[str],
) -> float:
    score = 15.0
    dropped = block_by_code.get("source_block_dropped", 0)
    if dropped:
        score = 0.0
        notes.append(f"-15.0 completeness: {dropped} source block(s) dropped (conservation failure)")
        return score
    failed_pages = sum(1 for p in ast.pages if p.failed_closed)
    if failed_pages:
        ded = min(15.0, 5.0 * failed_pages)
        score -= ded
        notes.append(f"-{ded:.1f} completeness: {failed_pages} failed-closed page(s)")
    if not ast.pages:
        score = 0.0
        notes.append("-15.0 completeness: 0 extracted pages")
    return max(0.0, round(score, 2))


def _score_compiled_validity(
    blocker_count: int,
    compiled_gate_result: dict[str, Any] | None,
    notes: list[str],
) -> tuple[float, int]:
    if compiled_gate_result is None:
        return (10.0 if blocker_count == 0 else 5.0), 0
    err_cnt = int(compiled_gate_result.get("error_count") or len(compiled_gate_result.get("errors") or []))
    if err_cnt == 0 and compiled_gate_result.get("ok", True):
        return 10.0, 0
    ded = min(10.0, 2.5 * max(1, err_cnt))
    notes.append(f"-{ded:.1f} compiled validity: {err_cnt} post-compile gate error(s)")
    return max(0.0, round(10.0 - ded, 2)), err_cnt


def _all_pages_p2_verified(
    ast: CollectionAST,
    proof_ledger: dict[str, Any] | None,
) -> bool:
    if not ast.pages:
        return False
    if proof_ledger is not None:
        pages_map = proof_ledger.get("pages") or {}
        return all(is_p2_human_verified_page(pages_map.get(str(p.page_number))) for p in ast.pages)
    ver_cnt = int(ast.proof_summary.get("verified") or 0)
    return ver_cnt == len(ast.pages) and len(ast.pages) > 0


def _classify_provenance_tier(
    ast: CollectionAST,
    total_blockers: int,
    hotspot_count: int,
    proof_ledger: dict[str, Any] | None,
) -> ProvenanceTier:
    has_failed_page = any(p.failed_closed for p in ast.pages)
    if total_blockers > 0 or has_failed_page or not ast.pages:
        return "blocked"
    if _all_pages_p2_verified(ast, proof_ledger):
        return "source_certified"
    if hotspot_count == 0:
        return "machine_checked"
    return "community"


def score_collection_rubric(
    ast: CollectionAST,
    *,
    proof_ledger: dict[str, Any] | None = None,
    compiled_gate_result: dict[str, Any] | None = None,
) -> RubricBreakdown:
    """Compute the 100-point `RubricBreakdown` and honest 4-tier provenance for `ast`."""
    block_by_code: Counter[str] = Counter(f.code for f in ast.findings if f.severity == "blocker")
    warn_by_code: Counter[str] = Counter(f.code for f in ast.findings if f.severity == "warning")

    ast_blockers = sum(block_by_code.values())
    hotspot_count = sum(warn_by_code.values())
    notes: list[str] = []

    s_text = _score_text_and_diacritics(warn_by_code, block_by_code, notes)
    s_struct = _score_epistolary_structure(ast, warn_by_code, block_by_code, notes)
    s_app = _score_apparatus(warn_by_code, block_by_code, notes)
    s_comp = _score_completeness(ast, block_by_code, notes)
    s_valid, gate_errs = _score_compiled_validity(ast_blockers, compiled_gate_result, notes)

    total_blockers = ast_blockers + gate_errs
    total_score = round(s_text + s_struct + s_app + s_comp + s_valid, 2)
    tier = _classify_provenance_tier(ast, total_blockers, hotspot_count, proof_ledger)
    if not notes:
        notes.append(f"All 5 rubric pillars clean; provenance tier: {tier}.")

    rubric = RubricBreakdown(
        total_score=total_score,
        text_and_diacritic_fidelity=s_text,
        epistolary_structure_and_layout=s_struct,
        apparatus_and_footnote_integrity=s_app,
        completeness_and_page_coverage=s_comp,
        compiled_output_validity=s_valid,
        provenance_tier=tier,
        blocker_count=total_blockers,
        hotspot_count=hotspot_count,
        notes=notes,
    )
    ast.rubric = rubric
    return rubric
