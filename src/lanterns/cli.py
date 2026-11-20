"""Unified CLI (`lanterns`) for Paper Lanterns v2 archival correspondence compiler & proofing system.

ROLE: Parse user CLI commands and dispatch to core, source, extraction, epistolary, validation, and renderer entry points.
INPUTS: Command-line arguments (`sys.argv`).
OUTPUTS: JSON/console summaries and exit code (`0` on success, `1` on validation/gate failure).
INVARIANT: Enforces 5-plane separation (`workspace/experiments/` -> `previews/`, `workspace/runs/` -> `library/`).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lanterns.core.config import BookConfig
from lanterns.core.env import load_env
from lanterns.core.hashes import sha256_file
from lanterns.core.paths import RepoPaths
from lanterns.renderers import (
    garbage_collect_workspace,
    generate_catalog_status,
    render_complete_edition,
    render_proofing_studio,
    run_doctor,
)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_init_book(args: argparse.Namespace, repo: RepoPaths) -> int:
    authors = list(args.author or [])
    author_str = ", ".join(authors)
    cfg = BookConfig(
        book_id=args.book_id,
        title=args.title,
        author_or_editor=author_str,
        authors=authors,
        publication_year=args.year,
        collection=args.collection,
        language=args.language,
        archive_id=args.archive_id or "",
        source_pdf=args.source_pdf or "",
        profile=args.profile,
    )
    saved_path = cfg.save(paths=repo)
    (repo.corrections_dir(cfg.book_id)).mkdir(parents=True, exist_ok=True)
    (repo.scan_checks_dir(cfg.book_id)).mkdir(parents=True, exist_ok=True)
    _print_json({"ok": True, "book_id": cfg.book_id, "book_toml": str(saved_path)})
    return 0


def _cmd_search_archive(args: argparse.Namespace, _repo: RepoPaths) -> int:
    from lanterns.source.archive_org import search_archive_correspondence

    hits = search_archive_correspondence(args.query, limit=args.limit)
    _print_json({"ok": True, "count": len(hits), "results": hits})
    return 0


def _sync_book_toml_after_fetch(
    bcfg: BookConfig | None,
    res: dict[str, Any],
    repo: RepoPaths,
) -> None:
    if bcfg is None:
        return
    orig_info = res.get("originals_pdf")
    prim_info = res.get("primary")
    pdf_info: dict[str, Any] | None = None
    if isinstance(orig_info, dict) and str(orig_info.get("path") or "").lower().endswith(".pdf"):
        pdf_info = orig_info
    elif isinstance(prim_info, dict) and str(prim_info.get("path") or "").lower().endswith(".pdf"):
        pdf_info = prim_info
    if pdf_info is None:
        return
    pdf_path = Path(str(pdf_info["path"])).resolve()
    if not pdf_path.is_file():
        return
    repo_root = repo.root.resolve()
    try:
        bcfg.source_pdf = pdf_path.relative_to(repo_root).as_posix()
    except ValueError:
        bcfg.source_pdf = str(pdf_path)
    bcfg.source_pdf_sha256 = str(pdf_info.get("sha256") or sha256_file(pdf_path))
    saved_toml = bcfg.save(paths=repo)
    res["source_pdf"] = bcfg.source_pdf
    res["source_pdf_sha256"] = bcfg.source_pdf_sha256
    res["book_toml_updated"] = str(saved_toml)


def _cmd_fetch_archive(args: argparse.Namespace, repo: RepoPaths) -> int:
    from lanterns.source.archive_org import download_archive_source

    identifier = args.identifier
    bcfg: BookConfig | None = None
    book_toml = repo.book_dir(identifier) / "book.toml"
    if book_toml.is_file():
        bcfg = BookConfig.load(identifier, paths=repo)
        if bcfg.archive_id:
            identifier = bcfg.archive_id
    dest = Path(args.dest_dir) if args.dest_dir else (repo.sources_dir / identifier)
    leaves: list[int] | None = None
    if args.leaves:
        leaves = [int(x.strip()) for x in args.leaves.split(",") if x.strip()]
    expected_pages = getattr(bcfg, "page_count", None) if bcfg is not None else None
    res = download_archive_source(
        identifier,
        dest,
        prefer_jp2=not args.prefer_pdf,
        leaf_indices=leaves,
        stream_slice_only=args.stream_slice,
        expected_page_count=expected_pages if (isinstance(expected_pages, int) and expected_pages > 0) else None,
    )
    _sync_book_toml_after_fetch(bcfg, res, repo)
    _print_json(res)
    return 0


def _cmd_triage_pdf(args: argparse.Namespace, repo: RepoPaths) -> int:
    from lanterns.extraction.pipeline import triage_book_pdf

    report = triage_book_pdf(args.target, sample_pages=args.pages, repo_root=repo.root)
    _print_json(report)
    return 0


def _cmd_extract(args: argparse.Namespace, repo: RepoPaths) -> int:
    from lanterns.extraction.pipeline import run_extraction

    scanline_opt: bool | None = True if args.use_scanline_prefix else None
    report = run_extraction(
        args.book_id,
        kind=args.kind,
        run_id=args.run_id,
        pages=args.pages,
        model=args.model,
        profile=args.profile,
        max_workers=args.max_workers,
        cost_ceiling_usd=args.cost_ceiling_usd,
        resume=args.resume,
        batch=args.batch,
        batch_chunk_size=args.batch_chunk_size,
        batch_poll_seconds=args.batch_poll_seconds,
        use_scanline_prefix=scanline_opt,
        source_pdf=args.source_pdf,
        repo_root=repo.root,
    )
    _print_json(report)
    return 0


def _cmd_verify_hotspots(args: argparse.Namespace, repo: RepoPaths) -> int:
    from lanterns.extraction.hotspot_verifier import verify_hotspots

    report = verify_hotspots(
        args.book_id,
        run_id=args.run_id,
        verify_id=args.verify_id,
        cost_ceiling_usd=args.cost_ceiling_usd,
        model=args.model,
        source_pdf=args.source_pdf,
        repo_root=repo.root,
    )
    _print_json(report)
    return 0


def _resolve_compile_target_dir(
    book: BookConfig,
    run_id: str,
    run_kind: str,
    args: argparse.Namespace,
    repo: RepoPaths,
) -> Path:
    if args.output_dir:
        return Path(args.output_dir).resolve()
    if run_kind == "canonical" or args.publish_preview_to_library:
        return repo.edition_dir(book.collection, book.book_id)
    return repo.preview_edition_dir(book.book_id, run_id)


def _cmd_compile(args: argparse.Namespace, repo: RepoPaths) -> int:
    from lanterns.epistolary import compile_collection_ast, load_proof_ledger
    from lanterns.validation import run_ast_validators, score_collection_rubric, validate_compiled_edition

    book = BookConfig.load(args.book_id, paths=repo)
    resolved_run_id = book.resolve_run_id(args.run_id)
    run_dir, run_kind = repo.resolve_run_dir_with_kind(book.book_id, resolved_run_id)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    if args.set_active:
        if run_kind != "canonical" and not args.publish_preview_to_library:
            raise ValueError(
                f"Refusing `--set-active` on experiment run {resolved_run_id!r}; pass `--kind canonical` or `--publish-preview-to-library`."
            )
        book.active_run_id = resolved_run_id
        book.save(paths=repo)

    out_dir = _resolve_compile_target_dir(book, resolved_run_id, run_kind, args, repo)
    proof_ledger = load_proof_ledger(book.book_id, repo_root=repo.root)
    pdf_path = book.resolve_source_pdf_path(args.source_pdf, paths=repo)

    ast = compile_collection_ast(
        book_id=book.book_id,
        title=book.title,
        collection=book.collection,
        author_or_editor=book.author_or_editor,
        year=book.publication_year,
        archive_id=book.archive_id or None,
        active_run_id=resolved_run_id,
        run_dir=run_dir,
        repo_root=repo.root,
        proof_ledger=proof_ledger,
    )
    findings = run_ast_validators(ast, proof_ledger=proof_ledger)
    ast = ast.model_copy(update={"findings": findings})

    render_complete_edition(
        ast,
        out_dir,
        proof_ledger=proof_ledger,
        run_dir=run_dir,
        source_pdf=pdf_path,
        bundle_scans=args.bundle_scans,
        relay_url=args.relay_url,
        include_proofing=True,
    )
    gate_res = validate_compiled_edition(out_dir, ast, require_proofing=True)
    rubric = score_collection_rubric(ast, proof_ledger=proof_ledger, compiled_gate_result=gate_res)
    ast = ast.model_copy(update={"rubric": rubric})

    final_info = render_complete_edition(
        ast,
        out_dir,
        proof_ledger=proof_ledger,
        run_dir=run_dir,
        source_pdf=pdf_path,
        bundle_scans=args.bundle_scans,
        relay_url=args.relay_url,
        include_proofing=True,
    )
    _print_json(
        {
            "ok": gate_res.get("ok", False),
            "book_id": book.book_id,
            "run_id": resolved_run_id,
            "run_kind": run_kind,
            "edition_dir": str(out_dir),
            "letter_count": len(ast.letters),
            "rubric_score": rubric.total_score,
            "provenance_tier": rubric.provenance_tier,
            "compiled_gate_errors": gate_res.get("error_count", 0),
            "formats": final_info["manifest"]["formats"],
        }
    )
    return 0


def _cmd_validate_edition(args: argparse.Namespace, repo: RepoPaths) -> int:
    from lanterns.core.models import CollectionAST
    from lanterns.validation import validate_compiled_edition

    targets: list[Path] = []
    if args.edition_dir:
        targets.append(Path(args.edition_dir).resolve())
    elif args.all:
        if repo.library_dir.is_dir():
            targets.extend(sorted(p.parent for p in repo.library_dir.rglob("edition_manifest.json")))
    elif args.book_id:
        book = BookConfig.load(args.book_id, paths=repo)
        targets.append(repo.edition_dir(book.collection, book.book_id))
    else:
        raise ValueError("Provide `book_id`, `--edition-dir`, or `--all`.")

    results: list[dict[str, Any]] = []
    all_ok = True
    for ed_dir in targets:
        ast_file = ed_dir / "collection_ast.json"
        ast_obj = (
            CollectionAST.model_validate(json.loads(ast_file.read_text(encoding="utf-8")))
            if ast_file.is_file()
            else None
        )
        res = validate_compiled_edition(ed_dir, ast_obj, require_proofing=not args.skip_proofing)
        results.append(res)
        if not res.get("ok", False):
            all_ok = False

    _print_json({"ok": all_ok, "checked": len(results), "results": results})
    return 0 if all_ok else 1


def _cmd_proof_site(args: argparse.Namespace, repo: RepoPaths) -> int:
    from lanterns.core.models import CollectionAST
    from lanterns.epistolary import compile_collection_ast, load_proof_ledger

    book = BookConfig.load(args.book_id, paths=repo)
    resolved_run_id = book.resolve_run_id(args.run_id)
    run_dir, run_kind = repo.resolve_run_dir_with_kind(book.book_id, resolved_run_id)
    ed_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else (
            repo.edition_dir(book.collection, book.book_id)
            if run_kind == "canonical"
            else repo.preview_edition_dir(book.book_id, resolved_run_id)
        )
    )
    proof_ledger = load_proof_ledger(book.book_id, repo_root=repo.root)
    ast_path = ed_dir / "collection_ast.json"
    if ast_path.is_file():
        ast = CollectionAST.model_validate(json.loads(ast_path.read_text(encoding="utf-8")))
    else:
        ast = compile_collection_ast(
            book_id=book.book_id,
            title=book.title,
            collection=book.collection,
            author_or_editor=book.author_or_editor,
            year=book.publication_year,
            archive_id=book.archive_id or None,
            active_run_id=resolved_run_id,
            run_dir=run_dir,
            repo_root=repo.root,
            proof_ledger=proof_ledger,
        )

    pdf_path = book.resolve_source_pdf_path(args.source_pdf, paths=repo)
    info = render_proofing_studio(
        ast,
        ed_dir,
        proof_ledger=proof_ledger,
        run_dir=run_dir,
        source_pdf=pdf_path,
        bundle_scans=args.bundle_scans,
        relay_url=args.relay_url,
        has_book_web=(ed_dir / "web" / "index.html").is_file(),
        has_letters=(ed_dir / "letters" / "index.html").is_file(),
    )
    _print_json({"ok": True, **info})
    return 0


def _cmd_intake(args: argparse.Namespace, repo: RepoPaths) -> int:
    from lanterns.epistolary import intake_proposal, load_pages_from_run_dir

    prop_path = args.proposal or args.proposal_pos
    if not prop_path:
        raise ValueError("Provide proposal JSON path via positional argument or `--proposal`.")
    pages = None
    witness_dir = None
    if (repo.book_dir(args.book_id) / "book.toml").is_file():
        book = BookConfig.load(args.book_id, paths=repo)
        if book.active_run_id:
            run_dir = repo.find_run_dir(book.book_id, book.active_run_id)
            if run_dir is not None and run_dir.is_dir():
                pages = load_pages_from_run_dir(run_dir)
                witness_dir = run_dir / "witnesses"
    res = intake_proposal(args.book_id, prop_path, repo_root=repo.root, pages=pages, witness_dir=witness_dir)
    _print_json(res)
    return 0


def _cmd_status(_args: argparse.Namespace, repo: RepoPaths) -> int:
    res = generate_catalog_status(repo.root, write_markdown=True)
    _print_json(
        {
            "ok": True,
            "catalog_status_path": res["catalog_status_path"],
            "book_count": len(res["books"]),
            "canonical_run_count": len(res["canonical_runs"]),
            "experiment_run_count": len(res["experiment_runs"]),
        }
    )
    return 0


def _cmd_doctor(_args: argparse.Namespace, repo: RepoPaths) -> int:
    res = run_doctor(repo.root)
    _print_json(res)
    return 0 if res["ok"] else 1


def _cmd_gc(args: argparse.Namespace, repo: RepoPaths) -> int:
    res = garbage_collect_workspace(
        repo.root,
        apply=args.apply,
        prune_previews=args.prune_previews,
        force=bool(getattr(args, "force", False)),
    )
    _print_json(res)
    return 0


def _cmd_benchmark(args: argparse.Namespace, repo: RepoPaths) -> int:
    from lanterns.validation.architecture_guard import evaluate_epistolary_exam

    res = evaluate_epistolary_exam(
        repo.root,
        exam_dir=args.exam_dir,
        predictions_dir=args.predictions_dir,
    )
    _print_json(res)
    return 0 if res.get("ok", True) else 1


def _register_ingest_subparsers(sub: Any) -> None:
    p_init = sub.add_parser("init-book", help="Register a correspondence volume in books/<book_id>/book.toml")
    p_init.add_argument("book_id")
    p_init.add_argument("--title", required=True)
    p_init.add_argument("--author", action="append", default=[])
    p_init.add_argument("--year", type=int, default=None)
    p_init.add_argument("--collection", default="world-correspondence")
    p_init.add_argument("--archive-id", default="")
    p_init.add_argument("--profile", default="english_epistolary")
    p_init.add_argument("--source-pdf", default="")
    p_init.add_argument("--language", default="en")

    p_sa = sub.add_parser("search-archive", help="Search Internet Archive for public-domain correspondence")
    p_sa.add_argument("query")
    p_sa.add_argument("--limit", type=int, default=10)

    p_fa = sub.add_parser("fetch-archive", help="Download SHA-256-verified JP2/PDF source from Internet Archive")
    p_fa.add_argument("identifier")
    p_fa.add_argument("--dest-dir", default=None)
    p_fa.add_argument("--prefer-pdf", action="store_true")
    p_fa.add_argument("--leaves", default=None, help="Comma-separated leaf indices for slice download")
    p_fa.add_argument("--stream-slice", action="store_true")

    p_tri = sub.add_parser("triage-pdf", help="Inspect optical DPI, OCR layer, and JBIG2 risk on a PDF or book_id")
    p_tri.add_argument("target")
    p_tri.add_argument("--pages", default=None)


def _register_pipeline_subparsers(sub: Any) -> None:
    p_ext = sub.add_parser("extract", help="Run diplomatic VLM extraction into workspace/{experiments,runs}/")
    p_ext.add_argument("book_id")
    p_ext.add_argument("--kind", choices=["experiment", "canonical"], default="experiment")
    p_ext.add_argument("--run-id", default=None)
    p_ext.add_argument("--pages", default=None)
    p_ext.add_argument("--model", default=None)
    p_ext.add_argument("--profile", default=None)
    p_ext.add_argument("--max-workers", type=int, default=None)
    p_ext.add_argument("--cost-ceiling-usd", type=float, default=None)
    p_ext.add_argument("--resume", dest="resume", action="store_true", default=True)
    p_ext.add_argument("--no-resume", dest="resume", action="store_false")
    p_ext.add_argument("--batch", action="store_true", help="Submit 50%%-discounted Anthropic/Gemini Batch jobs")
    p_ext.add_argument("--batch-chunk-size", type=int, default=25, help="Pages per batch job (default: 25)")
    p_ext.add_argument("--batch-poll-seconds", type=float, default=10.0, help="Seconds between batch status polls")
    p_ext.add_argument("--use-scanline-prefix", action="store_true")
    p_ext.add_argument("--source-pdf", default=None)

    p_vh = sub.add_parser("verify-hotspots", help="Run 450-DPI isolated crop & boundary hotspot verification")
    p_vh.add_argument("book_id")
    p_vh.add_argument("--run-id", default=None)
    p_vh.add_argument("--verify-id", default=None)
    p_vh.add_argument("--cost-ceiling-usd", type=float, default=0.10)
    p_vh.add_argument("--model", default=None)
    p_vh.add_argument("--source-pdf", default=None)

    p_comp = sub.add_parser("compile", help="Compile CollectionAST into Web, Online Letters, EPUB 3, MD, TEI, & Proofing")
    p_comp.add_argument("book_id")
    p_comp.add_argument("--run-id", default=None)
    p_comp.add_argument("--set-active", action="store_true")
    p_comp.add_argument("--bundle-scans", action="store_true")
    p_comp.add_argument("--output-dir", default=None)
    p_comp.add_argument("--publish-preview-to-library", action="store_true")
    p_comp.add_argument("--relay-url", default="")
    p_comp.add_argument("--source-pdf", default=None)


def _register_ops_subparsers(sub: Any) -> None:
    p_val = sub.add_parser("validate-edition", help="Validate compiled Web, Letters, EPUB 3, Markdown, TEI, & Proofing")
    p_val.add_argument("book_id", nargs="?", default=None)
    p_val.add_argument("--edition-dir", default=None)
    p_val.add_argument("--all", action="store_true")
    p_val.add_argument("--skip-proofing", action="store_true")

    p_ps = sub.add_parser("proof-site", help="Build safe-DOM Distributed Proofreaders (P1 -> P2) Studio")
    p_ps.add_argument("book_id")
    p_ps.add_argument("--run-id", default=None)
    p_ps.add_argument("--bundle-scans", action="store_true")
    p_ps.add_argument("--relay-url", default="")
    p_ps.add_argument("--output-dir", default=None)
    p_ps.add_argument("--source-pdf", default=None)

    p_int = sub.add_parser("intake", help="Ingest community correction/sign-off proposal JSON into books/<book_id>/")
    p_int.add_argument("book_id")
    p_int.add_argument("proposal_pos", nargs="?", default=None)
    p_int.add_argument("--proposal", default=None)

    sub.add_parser("status", help="Regenerate master CATALOG_STATUS.md")
    sub.add_parser("doctor", help="Run anti-sprawl, security, dependency, plane-isolation, schema, & AST checks")

    p_gc = sub.add_parser("gc", help="Reclaim regenerable workspace PNG caches (dry-run default)")
    p_gc.add_argument("--apply", action="store_true")
    p_gc.add_argument("--prune-previews", action="store_true")
    p_gc.add_argument("--force", action="store_true", help="Prune even if run.lock or active batch job exists")

    p_bm = sub.add_parser("benchmark", help="Score tuning vs held-out epistolary exam generalization")
    p_bm.add_argument("--exam-dir", default=None)
    p_bm.add_argument("--predictions-dir", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lanterns",
        description="Paper Lanterns v2 — Archival Correspondence Compiler & Community Proofing Platform",
    )
    parser.add_argument("--root", default=None, help="Override repository root directory")
    sub = parser.add_subparsers(dest="command", required=True)
    _register_ingest_subparsers(sub)
    _register_pipeline_subparsers(sub)
    _register_ops_subparsers(sub)
    return parser


COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace, RepoPaths], int]] = {
    "init-book": _cmd_init_book,
    "search-archive": _cmd_search_archive,
    "fetch-archive": _cmd_fetch_archive,
    "triage-pdf": _cmd_triage_pdf,
    "extract": _cmd_extract,
    "verify-hotspots": _cmd_verify_hotspots,
    "compile": _cmd_compile,
    "validate-edition": _cmd_validate_edition,
    "proof-site": _cmd_proof_site,
    "intake": _cmd_intake,
    "status": _cmd_status,
    "doctor": _cmd_doctor,
    "gc": _cmd_gc,
    "benchmark": _cmd_benchmark,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = RepoPaths.from_root(args.root)
    load_env(repo.root / ".env")
    handler = COMMAND_HANDLERS[args.command]
    return handler(args, repo)


if __name__ == "__main__":
    sys.exit(main())
