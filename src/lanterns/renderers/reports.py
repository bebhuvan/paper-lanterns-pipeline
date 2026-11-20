"""Deterministic per-book scholarly & operational edition report (`EDITION_REPORT.md`).

ROLE: Summarize provenance, letter count, dual footnote apparatus, model spend, rubric breakdown, and hotspots.
INPUTS: CollectionAST from lanterns.core.models.
OUTPUTS: <edition_dir>/EDITION_REPORT.md (and optional <run_dir>/REPORT.md).
INVARIANT: Pure deterministic formatting at $0 LLM cost; zero book_id branching.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lanterns.core.models import CollectionAST
from lanterns.renderers.html_utils import atomic_write_text


def _format_findings_section(ast: CollectionAST) -> list[str]:
    if not ast.findings:
        return []
    out = ["## 6. Open Validation Findings"]
    for f in ast.findings[:40]:
        loc = (f.page_number and f"p. {f.page_number}") or f.letter_id or "book"
        out.append(f"- `[{f.severity}]` `{f.code}` ({loc}): {f.message}")
    if len(ast.findings) > 40:
        out.append(f"- ... and {len(ast.findings) - 40} additional finding(s).")
    out.append("")
    return out


def render_edition_report(
    ast: CollectionAST,
    edition_dir: str | Path,
    *,
    run_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Write `EDITION_REPORT.md` inside `edition_dir` (and `REPORT.md` inside `run_dir` if provided)."""
    ed_root = Path(edition_dir)
    ed_root.mkdir(parents=True, exist_ok=True)
    report_path = ed_root / "EDITION_REPORT.md"

    extracted_pages = sum(1 for p in ast.pages if not p.failed_closed)
    failed_pages = [p.page_number for p in ast.pages if p.failed_closed]
    rescued_pages = [p.page_number for p in ast.pages if p.rescued]
    orphan_fns = sum(1 for fn in ast.footnotes.values() if fn.is_orphan)

    rubric = ast.rubric
    tier = (rubric and rubric.provenance_tier) or "community"
    total_score = (rubric and f"{rubric.total_score:.1f} / 100.0") or "Unscored"

    lines: list[str] = [
        f"# Edition Report — {ast.title}",
        "",
        "## 1. Bibliographic & Run Provenance",
        f"- **Book ID**: `{ast.book_id}`",
        f"- **Collection**: `{ast.collection}`",
        f"- **Author / Editor**: {ast.author_or_editor or 'Public Domain'}",
        f"- **Publication Year**: {ast.year or 'n.d.'}",
        f"- **Internet Archive ID**: `{ast.archive_id or 'local-pdf'}`",
        f"- **Active Run ID**: `{ast.active_run_id}`",
        f"- **Provenance Tier**: `{tier}`",
        f"- **Total API Spend**: `${ast.total_spend_usd:.4f}`",
        "",
        "## 2. Structural & Epistolary Inventory",
        f"- **Total Source Pages**: {ast.total_pages}",
        f"- **Extracted Pages**: {extracted_pages}",
        f"- **Fail-Closed Pages**: {len(failed_pages)} ({failed_pages or 'none'})",
        f"- **Rescued / Fallback Pages**: {len(rescued_pages)} ({rescued_pages or 'none'})",
        f"- **Sections Compiled**: {len(ast.sections)}",
        f"- **Individual Letters Extracted**: {len(ast.letters)}",
        f"- **Footnotes Linked**: {len(ast.footnotes) - orphan_fns} (Orphan notes preserved: {orphan_fns})",
        "",
    ]

    if ast.model_breakdown:
        lines.append("## 3. Multi-Model Attribution")
        for model_key, cnt in sorted(ast.model_breakdown.items()):
            lines.append(f"- `{model_key}`: {cnt} page(s)")
        lines.append("")

    if rubric is not None:
        lines.extend(
            [
                "## 4. 100-Point Epistolary Quality Rubric",
                f"- **Total Score**: **{total_score}**",
                f"- **Text & Diacritic Fidelity (30)**: {rubric.text_and_diacritic_fidelity:.1f}",
                f"- **Epistolary Structure & Layout (25)**: {rubric.epistolary_structure_and_layout:.1f}",
                f"- **Apparatus & Footnote Integrity (20)**: {rubric.apparatus_and_footnote_integrity:.1f}",
                f"- **Completeness & Page Coverage (15)**: {rubric.completeness_and_page_coverage:.1f}",
                f"- **Compiled Multi-Format Validity (10)**: {rubric.compiled_output_validity:.1f}",
                f"- **Blockers**: {rubric.blocker_count} · **Hotspots**: {rubric.hotspot_count}",
                "",
            ]
        )

    proof = ast.proof_summary or {}
    if proof:
        p1_cnt = proof.get("p1_proofed", proof.get("p1_proofed_count", 0))
        ver_cnt = proof.get("verified", proof.get("verified_count", 0))
        reread_cnt = proof.get("machine_reread", proof.get("machine_reread_count", 0))
        lines.extend(
            [
                "## 5. Community Proofreading Progress (P1 → P2)",
                f"- **Round P1 Proofread**: {p1_cnt} page(s)",
                f"- **Round P2 Verified**: {ver_cnt} page(s)",
                f"- **Machine / Scan Re-read**: {reread_cnt} page(s)",
                "",
            ]
        )

    lines.extend(_format_findings_section(ast))

    content = "\n".join(lines).rstrip() + "\n"
    atomic_write_text(report_path, content)
    if run_dir is not None and Path(run_dir).is_dir():
        atomic_write_text(Path(run_dir) / "REPORT.md", content)
    return {"report_path": str(report_path)}
