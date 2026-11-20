"""Multi-format archival edition renderers, Proofing Studio, catalog ledger, and doctor gate.

ROLE: Export unified rendering entry points for Book Web, Online Letters, EPUB 3, Markdown, TEI P5, Proofing, Reports, Status, and Doctor.
INPUTS: CollectionAST from lanterns.core.models and RepoPaths.
OUTPUTS: Complete multi-format edition directories and repository health/catalog artifacts.
INVARIANT: Pure structural rendering; safe DOM (createElement/textContent only); zero book_id branching.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lanterns.core.models import CollectionAST
from lanterns.renderers.catalog_status import garbage_collect_workspace, generate_catalog_status
from lanterns.renderers.doctor import run_doctor
from lanterns.renderers.html_utils import atomic_write_json
from lanterns.renderers.proofing_studio import compute_page_blocks_sha256, render_proofing_studio
from lanterns.renderers.render_book_web import render_book_web
from lanterns.renderers.render_epub import render_epub_edition
from lanterns.renderers.render_letters import render_letters_edition
from lanterns.renderers.render_markdown_tei import (
    render_markdown_and_tei,
    render_scholarly_markdown,
    render_tei_edition,
)
from lanterns.renderers.reports import render_edition_report


def render_complete_edition(
    ast: CollectionAST,
    edition_dir: str | Path,
    *,
    proof_ledger: dict[str, Any] | None = None,
    run_dir: str | Path | None = None,
    source_pdf: str | Path | None = None,
    bundle_scans: bool = False,
    relay_url: str = "",
    include_proofing: bool = True,
) -> dict[str, Any]:
    """Render all publication formats + Proofing Studio + reports + manifests into `edition_dir`."""
    ed_root = Path(edition_dir)
    ed_root.mkdir(parents=True, exist_ok=True)

    epub_info = render_epub_edition(ast, ed_root)
    md_tei_info = render_markdown_and_tei(ast, ed_root)
    letters_info = render_letters_edition(ast, ed_root, has_book_web=True, has_proofing=include_proofing)
    web_info = render_book_web(
        ast,
        ed_root,
        has_letters_edition=True,
        has_epub=True,
        has_markdown=True,
        has_tei=True,
        has_proofing=include_proofing,
    )
    proof_info: dict[str, Any] = {}
    if include_proofing:
        proof_info = render_proofing_studio(
            ast,
            ed_root,
            proof_ledger=proof_ledger,
            run_dir=run_dir,
            source_pdf=source_pdf,
            bundle_scans=bundle_scans,
            relay_url=relay_url,
            has_book_web=True,
            has_letters=True,
        )
    report_info = render_edition_report(ast, ed_root, run_dir=run_dir)

    atomic_write_json(ed_root / "collection_ast.json", ast.model_dump(mode="json"))
    if ast.rubric is not None:
        atomic_write_json(ed_root / "rubric_score.json", ast.rubric.model_dump(mode="json"))

    manifest = {
        "schema_version": "1.0.0",
        "book_id": ast.book_id,
        "collection": ast.collection,
        "title": ast.title,
        "active_run_id": ast.active_run_id,
        "compiled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_pages": ast.total_pages,
        "section_count": len(ast.sections),
        "letter_count": len(ast.letters),
        "footnote_count": len(ast.footnotes),
        "provenance_tier": ast.rubric.provenance_tier if ast.rubric else "community",
        "rubric_score": ast.rubric.total_score if ast.rubric else None,
        "blocker_count": ast.rubric.blocker_count if ast.rubric else 0,
        "hotspot_count": ast.rubric.hotspot_count if ast.rubric else 0,
        "formats": {
            "web": web_info["web_index"],
            "letters": letters_info["letters_index"],
            "epub": epub_info["epub_path"],
            "markdown": md_tei_info["markdown_path"],
            "tei": md_tei_info["tei_path"],
            "proofing": proof_info.get("proofing_index"),
            "report": report_info["report_path"],
        },
    }
    atomic_write_json(ed_root / "edition_manifest.json", manifest)
    return {
        "edition_dir": str(ed_root),
        "manifest": manifest,
        "web": web_info,
        "letters": letters_info,
        "epub": epub_info,
        "markdown_tei": md_tei_info,
        "proofing": proof_info,
        "report": report_info,
    }


__all__ = [
    "compute_page_blocks_sha256",
    "garbage_collect_workspace",
    "generate_catalog_status",
    "render_book_web",
    "render_complete_edition",
    "render_doctor",
    "render_edition_report",
    "render_epub_edition",
    "render_letters_edition",
    "render_markdown_and_tei",
    "render_proofing_studio",
    "render_scholarly_markdown",
    "render_tei_edition",
    "run_doctor",
]

render_doctor = run_doctor
