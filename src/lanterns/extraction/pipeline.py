"""Archival correspondence extraction pipeline enforcing 5-plane isolation and 4-tier VLM fallback.

Writes run artifacts exclusively under:
  - `workspace/experiments/<book_id>/<run_id>/` when `kind == "experiment"`
  - `workspace/runs/<book_id>/<run_id>/` when `kind == "canonical"`
"""

from __future__ import annotations

import fcntl
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymupdf as fitz

from lanterns.core.atomic import atomic_write_json
from lanterns.core.config import BookConfig, load_prompt, validate_data
from lanterns.core.cost import CostLedger
from lanterns.core.models import PageIR
from lanterns.core.paths import RepoPaths
from lanterns.core.pricing import conservative_call_reservation_usd, ensure_priced_routes, parse_model_route
from lanterns.extraction.batch_runner import execute_batch_candidate_pages
from lanterns.extraction.line_wraps import resolve_page_ir_line_wraps
from lanterns.extraction.normalize import normalize_wire_to_page_ir, solve_monotonic_folios
from lanterns.extraction.pdf_ingest import (
    PagePreflight,
    compute_pdf_sha256,
    inspect_and_render_page,
    pdf_page_count,
    triage_pdf,
)
from lanterns.providers.gemini import GeminiProvider, gemini_supports_explicit_cache
from lanterns.providers.router import FallbackRouter, ProviderRequest, build_provider, make_failed_closed_page_ir

SYSTEM_INSTRUCTION = (
    "You are a diplomatic archival correspondence transcription engine. "
    "Transcribe exact printed ink glyphs with 100% verbatim fidelity; never modernize or paraphrase."
)


def parse_pages_spec(pages_spec: str | list[int] | None, total_pages: int) -> list[int]:
    """Parse `'1-5,8,12-14'` or `list[int]` or `None` into sorted 1-based page numbers within `1..total_pages`."""
    if pages_spec is None or pages_spec == "":
        return list(range(1, total_pages + 1))
    if isinstance(pages_spec, list):
        return sorted({int(p) for p in pages_spec if 1 <= int(p) <= total_pages})
    spec = str(pages_spec).strip()
    if not spec or spec.lower() == "all":
        return list(range(1, total_pages + 1))

    selected: set[int] = set()
    for part in spec.split(","):
        tok = part.strip()
        if not tok:
            continue
        if "-" in tok:
            lo_s, hi_s = tok.split("-", 1)
            lo, hi = int(lo_s.strip()), int(hi_s.strip())
            if lo > hi:
                lo, hi = hi, lo
            for pnum in range(max(1, lo), min(total_pages, hi) + 1):
                selected.add(pnum)
        else:
            pnum = int(tok)
            if 1 <= pnum <= total_pages:
                selected.add(pnum)
    if not selected:
        raise ValueError(f"No valid pages resolved from {pages_spec!r} (PDF has {total_pages} pages)")
    return sorted(selected)


def load_run_pages(run_dir: str | Path) -> list[PageIR]:
    """Load sorted `PageIR` list from `run_dir / 'ir' / 'pages.json'` or `run_dir / 'pages' / 'page_*.json'`."""
    root = Path(run_dir)
    ir_pages_path = root / "ir" / "pages.json"
    if ir_pages_path.is_file():
        doc = json.loads(ir_pages_path.read_text(encoding="utf-8"))
        raw_list = doc.get("pages") if isinstance(doc, dict) else doc
        return sorted((PageIR.model_validate(item) for item in (raw_list or [])), key=lambda p: p.page_number)

    pages_dir = root / "pages"
    if pages_dir.is_dir():
        loaded = [
            PageIR.model_validate(json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(pages_dir.glob("page_*.json"))
        ]
        return sorted(loaded, key=lambda p: p.page_number)
    raise FileNotFoundError(f"No extracted PageIR artifacts found under {root}")


def triage_book_pdf(
    book_id_or_pdf: str | Path,
    *,
    sample_pages: str | list[int] | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Run optical DPI and JBIG2 lossy-mask triage on either a `book_id` or a direct PDF path."""
    repo = RepoPaths.from_root(repo_root)
    cand_path = Path(book_id_or_pdf)
    if cand_path.is_file():
        pdf_path = cand_path
    else:
        book = BookConfig.load(str(book_id_or_pdf), paths=repo)
        resolved = book.resolve_source_pdf_path(paths=repo)
        if resolved is None or not resolved.is_file():
            raise FileNotFoundError(f"Source PDF not found for book {book.book_id!r}: {resolved}")
        pdf_path = resolved

    total = pdf_page_count(pdf_path)
    selected = parse_pages_spec(sample_pages, total) if sample_pages is not None else None
    return triage_pdf(pdf_path, sample_pages=selected)


def _format_observational_dossier_hint(dossier: dict[str, Any] | None) -> str:
    """Format strictly non-rewriting observational volume context (script & footnote convention hint)."""
    if not dossier:
        return ""
    notes: list[str] = []
    if dossier.get("scripts"):
        notes.append(f"Observed scripts in volume: {', '.join(str(s) for s in dossier['scripts'])}.")
    if dossier.get("footnote_convention"):
        notes.append(f"Observed apparatus convention: {dossier['footnote_convention']}.")
    if notes:
        notes.append("Observational context only — never alter or normalize visible scan glyphs.")
    return " ".join(notes)


def _make_blank_page_ir(page_number: int, source_page_sha256: str) -> PageIR:
    return PageIR(
        page_number=page_number,
        printed_folio=None,
        page_orientation="upright",
        warning_codes=["blank_page"],
        failed_closed=False,
        failure_reason=None,
        source_page_sha256=source_page_sha256,
        blocks=[],
        provider="preflight",
        model="blank_preflight",
        harness="preflight",
        rescued=False,
        cost_usd=0.0,
    )


def _resolve_routes(
    settings: dict[str, Any],
    model_override: str | None,
) -> tuple[str, str, list[str]]:
    if model_override:
        prov, mod = parse_model_route(model_override)
    elif settings.get("primary"):
        prov, mod = parse_model_route(str(settings["primary"]))
    else:
        prov = str(settings.get("primary_provider") or "gemini")
        mod = str(settings.get("primary_model") or "gemini-3.8-flash")
    fallback_chain = [str(r) for r in (settings.get("fallback_chain") or []) if str(r).strip()]
    return prov, mod, fallback_chain


def _preflight_and_resume_pages(
    *,
    pdf_path: Path,
    target_pages: list[int],
    render_dpi: int,
    renders_dir: Path,
    witness_dir: Path,
    pages_out_dir: Path,
    resume: bool,
) -> tuple[dict[int, PagePreflight], dict[int, PageIR], list[int]]:
    preflights: dict[int, PagePreflight] = {}
    ready_pages: dict[int, PageIR] = {}
    to_extract: list[int] = []

    with fitz.open(pdf_path) as doc:
        for pnum in target_pages:
            png_path = renders_dir / f"page_{pnum:04d}.png"
            wit_path = witness_dir / f"page_{pnum:04d}.txt"
            pf = inspect_and_render_page(
                pdf_path,
                pnum,
                png_path,
                dpi=render_dpi,
                out_witness_txt=wit_path,
                doc=doc,
                reuse_existing=True,
            )
            preflights[pnum] = pf
            saved_page_json = pages_out_dir / f"page_{pnum:04d}.json"
            if resume and saved_page_json.is_file():
                cached_ir = PageIR.model_validate(json.loads(saved_page_json.read_text(encoding="utf-8")))
                if not cached_ir.failed_closed:
                    ready_pages[pnum] = cached_ir
                    continue
            if pf.is_blank_or_tissue:
                blank_ir = _make_blank_page_ir(pnum, pf.source_page_sha256)
                atomic_write_json(saved_page_json, blank_ir.model_dump(mode="json"))
                ready_pages[pnum] = blank_ir
            else:
                to_extract.append(pnum)
    return preflights, ready_pages, to_extract


def _build_page_provider_request(
    *,
    png_path: Path,
    pnum: int,
    total_pdf_pages: int,
    settings: dict[str, Any],
    extract_prompt: str,
    cache_name: str | None,
    dossier_hint: str,
    book_dir: Path,
) -> ProviderRequest:
    req_settings: dict[str, Any] = {
        "media_resolution": settings.get("media_resolution", "MEDIA_RESOLUTION_HIGH"),
        "thinking_level": settings.get("thinking_level", "low"),
        "max_output_tokens": int(settings.get("max_output_tokens") or 16384),
        "page_number": pnum,
        "book_dir": str(book_dir),
    }
    if cache_name:
        req_settings["cached_content"] = cache_name
        prompt_text = ""
    else:
        req_settings["system_instruction"] = SYSTEM_INSTRUCTION
        prompt_text = extract_prompt

    suffix = f"Physical PDF leaf {pnum} of {total_pdf_pages}."
    if dossier_hint:
        suffix = f"{suffix} {dossier_hint}"
    return ProviderRequest(
        prompt=prompt_text,
        schema_name="page_extract_wire",
        image_paths=[str(png_path)],
        dynamic_suffix=suffix,
        settings=req_settings,
    )


def run_extraction(
    book_id: str,
    *,
    kind: str = "experiment",
    run_id: str | None = None,
    pages: str | list[int] | None = None,
    model: str | None = None,
    profile: str | None = None,
    max_workers: int | None = None,
    cost_ceiling_usd: float | None = None,
    resume: bool = True,
    batch: bool = False,
    batch_chunk_size: int = 25,
    batch_poll_seconds: float = 10.0,
    use_scanline_prefix: bool | None = None,
    source_pdf: str | Path | None = None,
    primary_provider_override: Any = None,
    fallback_providers_override: list[Any] | None = None,
    prefer_scan_checks: bool = True,
    observational_dossier: dict[str, Any] | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Execute end-to-end diplomatic page extraction into `workspace/{experiments,runs}/<book_id>/<run_id>/`."""
    t0 = time.monotonic()
    started_iso = datetime.now(timezone.utc).isoformat()
    repo = RepoPaths.from_root(repo_root)
    book = BookConfig.load(book_id, paths=repo)

    pdf_path = book.resolve_source_pdf_path(source_pdf, paths=repo)
    if pdf_path is None or not pdf_path.is_file():
        raise FileNotFoundError(f"Source PDF not found for book {book_id!r}: {pdf_path}")

    eff_run_id = (run_id or f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}").strip()
    run_root = repo.run_dir(book.book_id, eff_run_id, kind=kind)
    run_root.mkdir(parents=True, exist_ok=True)

    lock_path = run_root / "run.lock"
    lock_fh = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_fh.close()
        raise RuntimeError(f"Another extraction process holds active lock on {run_root}") from exc

    try:
        return _execute_extraction_locked(
            book=book,
            pdf_path=pdf_path,
            run_root=run_root,
            kind=kind,
            pages=pages,
            model=model,
            profile=profile,
            max_workers=max_workers,
            cost_ceiling_usd=cost_ceiling_usd,
            resume=resume,
            batch=batch,
            batch_chunk_size=batch_chunk_size,
            batch_poll_seconds=batch_poll_seconds,
            use_scanline_prefix=use_scanline_prefix,
            primary_provider_override=primary_provider_override,
            fallback_providers_override=fallback_providers_override,
            prefer_scan_checks=prefer_scan_checks,
            observational_dossier=observational_dossier,
            repo=repo,
            t0=t0,
            started_iso=started_iso,
        )
    finally:
        lock_path.unlink(missing_ok=True)
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()


def _execute_extraction_locked(
    *,
    book: BookConfig,
    pdf_path: Path,
    run_root: Path,
    kind: str,
    pages: str | list[int] | None,
    model: str | None,
    profile: str | None,
    max_workers: int | None,
    cost_ceiling_usd: float | None,
    resume: bool,
    batch: bool,
    batch_chunk_size: int,
    batch_poll_seconds: float,
    use_scanline_prefix: bool | None,
    primary_provider_override: Any,
    fallback_providers_override: list[Any] | None,
    prefer_scan_checks: bool,
    observational_dossier: dict[str, Any] | None,
    repo: RepoPaths,
    t0: float,
    started_iso: str,
) -> dict[str, Any]:
    settings = book.resolved_extraction_settings(paths=repo, profile_override=profile)
    primary_prov_name, primary_model, fallback_chain = _resolve_routes(settings, model)
    render_dpi = int(settings.get("render_dpi") or 350)
    workers = max(1, int(max_workers or settings.get("max_workers") or 4))
    scanline_flag = (
        bool(use_scanline_prefix)
        if use_scanline_prefix is not None
        else bool(settings.get("use_scanline_prefix", False))
    )

    if primary_provider_override is None and primary_prov_name != "mock":
        routes_to_check: list[tuple[str, str | None] | str] = [(primary_prov_name, primary_model), *fallback_chain]
        ensure_priced_routes(routes_to_check, root=str(repo.root))

    primary_provider = primary_provider_override or build_provider(primary_prov_name, primary_model)
    default_res = (
        0.0
        if getattr(primary_provider, "name", "") == "mock"
        else conservative_call_reservation_usd(primary_prov_name, primary_model, root=str(repo.root))
    )
    ledger = CostLedger(
        run_root / "cost_ledger.jsonl",
        ceiling_usd=cost_ceiling_usd,
        default_reservation_usd=default_res,
    )

    total_pdf_pages = pdf_page_count(pdf_path)
    target_pages = parse_pages_spec(pages, total_pdf_pages)
    renders_dir = run_root / "renders"
    witness_dir = run_root / "witnesses"
    raw_wire_dir = run_root / "raw_wire"
    pages_out_dir = run_root / "pages"
    bands_dir = run_root / "band_splits"
    for d in (renders_dir, witness_dir, raw_wire_dir, pages_out_dir):
        d.mkdir(parents=True, exist_ok=True)

    preflights, page_map, to_extract = _preflight_and_resume_pages(
        pdf_path=pdf_path,
        target_pages=target_pages,
        render_dpi=render_dpi,
        renders_dir=renders_dir,
        witness_dir=witness_dir,
        pages_out_dir=pages_out_dir,
        resume=resume,
    )

    extract_prompt = load_prompt("extract_epistolary_v1", root=str(repo.root))
    dossier_hint = _format_observational_dossier_hint(observational_dossier)
    cache_name = _maybe_create_cache(primary_provider, settings, extract_prompt, len(to_extract), book.book_id)

    router = FallbackRouter(
        primary_provider,
        fallback_providers=fallback_providers_override,
        fallback_routes=fallback_chain,
        use_scanline_prefix=scanline_flag,
        prompt_fallback_text=extract_prompt,
        band_split_dir=bands_dir,
        book_dir=repo.book_dir(book.book_id),
        prefer_scan_checks=prefer_scan_checks,
        repo_root=str(repo.root),
    )

    try:
        if batch:
            execute_batch_candidate_pages(
                to_extract=to_extract,
                primary_provider=primary_provider,
                build_request_fn=lambda pnum: _build_page_provider_request(
                    png_path=renders_dir / f"page_{pnum:04d}.png",
                    pnum=pnum,
                    total_pdf_pages=total_pdf_pages,
                    settings=settings,
                    extract_prompt=extract_prompt,
                    cache_name=cache_name,
                    dossier_hint=dossier_hint,
                    book_dir=repo.book_dir(book.book_id),
                ),
                router=router,
                ledger=ledger,
                preflights=preflights,
                run_root=run_root,
                raw_wire_dir=raw_wire_dir,
                pages_out_dir=pages_out_dir,
                page_map=page_map,
                resume=resume,
                chunk_size=batch_chunk_size,
                poll_interval_s=batch_poll_seconds,
            )
        else:
            _extract_candidate_pages(
                to_extract=to_extract,
                workers=workers,
                renders_dir=renders_dir,
                total_pdf_pages=total_pdf_pages,
                settings=settings,
                extract_prompt=extract_prompt,
                cache_name=cache_name,
                dossier_hint=dossier_hint,
                book_dir=repo.book_dir(book.book_id),
                router=router,
                ledger=ledger,
                preflights=preflights,
                raw_wire_dir=raw_wire_dir,
                pages_out_dir=pages_out_dir,
                page_map=page_map,
            )
    finally:
        if isinstance(primary_provider, GeminiProvider) and cache_name:
            primary_provider.delete_context_cache(cache_name)
        ledger.flush()

    ordered_pages = [page_map[p] for p in sorted(page_map)]
    folio_solved = solve_monotonic_folios(ordered_pages)
    wrapped_pages, wrap_report = resolve_page_ir_line_wraps(folio_solved, witness_dir=witness_dir)

    for page_ir in wrapped_pages:
        payload = page_ir.model_dump(mode="json")
        validate_data("block_ir", payload, root=str(repo.root))
        atomic_write_json(pages_out_dir / f"page_{page_ir.page_number:04d}.json", payload)

    return _persist_run_summary_and_ir(
        book=book,
        pdf_path=pdf_path,
        run_root=run_root,
        kind=kind,
        target_pages=target_pages,
        total_pdf_pages=total_pdf_pages,
        wrapped_pages=wrapped_pages,
        wrap_report=wrap_report,
        ledger=ledger,
        started_iso=started_iso,
        duration_s=round(time.monotonic() - t0, 3),
    )


def _maybe_create_cache(
    primary_provider: Any,
    settings: dict[str, Any],
    extract_prompt: str,
    candidate_count: int,
    book_id: str,
) -> str | None:
    if (
        candidate_count >= 4
        and isinstance(primary_provider, GeminiProvider)
        and bool(settings.get("enable_context_cache", True))
        and gemini_supports_explicit_cache(primary_provider.model)
    ):
        return primary_provider.create_context_cache(
            system_instruction=SYSTEM_INSTRUCTION,
            static_prompt_text=extract_prompt,
            ttl_seconds=3600,
            display_name=f"lanterns-{book_id}",
        )
    return None


def _extract_candidate_pages(
    *,
    to_extract: list[int],
    workers: int,
    renders_dir: Path,
    total_pdf_pages: int,
    settings: dict[str, Any],
    extract_prompt: str,
    cache_name: str | None,
    dossier_hint: str,
    book_dir: Path,
    router: FallbackRouter,
    ledger: CostLedger,
    preflights: dict[int, PagePreflight],
    raw_wire_dir: Path,
    pages_out_dir: Path,
    page_map: dict[int, PageIR],
) -> None:
    if not to_extract:
        return

    def _process_one(pnum: int) -> PageIR:
        pf = preflights[pnum]
        if ledger.cancelled_event.is_set():
            return make_failed_closed_page_ir(
                pnum,
                f"Skipped because cost ceiling (${ledger.ceiling_usd or 0.0:.4f}) was reached.",
                source_page_sha256=pf.source_page_sha256,
            )
        req = _build_page_provider_request(
            png_path=renders_dir / f"page_{pnum:04d}.png",
            pnum=pnum,
            total_pdf_pages=total_pdf_pages,
            settings=settings,
            extract_prompt=extract_prompt,
            cache_name=cache_name,
            dossier_hint=dossier_hint,
            book_dir=book_dir,
        )
        outcome = router.execute_page(req, page_number=pnum, ledger=ledger)
        if outcome.failed_closed or outcome.success is None:
            failed_ir = make_failed_closed_page_ir(
                pnum,
                outcome.failure_reason or "Extraction failed closed",
                source_page_sha256=pf.source_page_sha256,
                cost_usd=outcome.total_cost_usd,
            )
            atomic_write_json(pages_out_dir / f"page_{pnum:04d}.json", failed_ir.model_dump(mode="json"))
            return failed_ir

        succ = outcome.success
        prov = succ.provenance or {}
        extra_warns = ["jbig2_lossy_mask_risk"] if pf.uses_jbig2 else []
        atomic_write_json(
            raw_wire_dir / f"page_{pnum:04d}.json",
            {
                "page_number": pnum,
                "wire": succ.data,
                "provenance": prov,
                "attempts": [a.__dict__ for a in outcome.attempts],
            },
        )
        page_ir = normalize_wire_to_page_ir(
            succ.data,
            page_number=pnum,
            source_page_sha256=pf.source_page_sha256,
            provider=str(prov.get("provider") or ""),
            model=str(prov.get("model") or ""),
            harness=str(prov.get("harness") or "sdk"),
            rescued=outcome.rescued,
            cost_usd=outcome.total_cost_usd,
            extra_warnings=extra_warns,
        )
        atomic_write_json(pages_out_dir / f"page_{pnum:04d}.json", page_ir.model_dump(mode="json"))
        return page_ir

    with ThreadPoolExecutor(max_workers=min(workers, len(to_extract))) as pool:
        futures = {pool.submit(_process_one, pnum): pnum for pnum in to_extract}
        for fut in as_completed(futures):
            p_res = fut.result()
            page_map[p_res.page_number] = p_res


def _persist_run_summary_and_ir(
    *,
    book: BookConfig,
    pdf_path: Path,
    run_root: Path,
    kind: str,
    target_pages: list[int],
    total_pdf_pages: int,
    wrapped_pages: list[PageIR],
    wrap_report: dict[str, Any],
    ledger: CostLedger,
    started_iso: str,
    duration_s: float,
) -> dict[str, Any]:
    pdf_sha = book.source_pdf_sha256 or compute_pdf_sha256(pdf_path)
    serialized_pages = [p.model_dump(mode="json") for p in wrapped_pages]
    flat_blocks = [b.model_dump(mode="json") for p in wrapped_pages for b in p.blocks]
    total_spend = round(ledger.total_spent_usd, 6)

    ir_dir = run_root / "ir"
    ir_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        ir_dir / "pages.json",
        {
            "version": "pages-ir-v2",
            "book_id": book.book_id,
            "run_id": run_root.name,
            "run_kind": kind,
            "source_pdf_sha256": pdf_sha,
            "total_pages": total_pdf_pages,
            "total_spend_usd": total_spend,
            "pages": serialized_pages,
        },
    )
    atomic_write_json(
        ir_dir / "blocks.json",
        {
            "version": "blocks-ir-v2",
            "book_id": book.book_id,
            "run_id": run_root.name,
            "source_pdf_sha256": pdf_sha,
            "total_pages": total_pdf_pages,
            "pages": serialized_pages,
            "blocks": flat_blocks,
        },
    )
    atomic_write_json(ir_dir / "line_wraps.json", wrap_report)

    failed_count = sum(1 for p in wrapped_pages if p.failed_closed)
    rescued_count = sum(1 for p in wrapped_pages if p.rescued)
    model_counts = Counter(f"{p.provider}/{p.model}" for p in wrapped_pages if not p.failed_closed and p.model)
    status = (
        "halted_cost_ceiling"
        if ledger.cancelled_event.is_set()
        else ("partial_failed" if failed_count > 0 else "complete")
    )

    summary = {
        "version": "run-summary-v2",
        "book_id": book.book_id,
        "run_id": run_root.name,
        "run_kind": kind,
        "is_partial_pages": len(target_pages) < total_pdf_pages,
        "target_pages": target_pages,
        "target_pages_count": len(target_pages),
        "total_pdf_pages": total_pdf_pages,
        "status": status,
        "started_at": started_iso,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": duration_s,
        "extracted_pages_count": len(wrapped_pages) - failed_count,
        "failed_closed_count": failed_count,
        "rescued_pages_count": rescued_count,
        "block_count": len(flat_blocks),
        "line_wrap_sites": wrap_report.get("site_count", 0),
        "model_breakdown": dict(model_counts),
        "total_spend_usd": total_spend,
        "run_dir": str(run_root),
    }
    atomic_write_json(run_root / "run_summary.json", summary)
    return {
        "ok": failed_count == 0 and not ledger.cancelled_event.is_set(),
        "book_id": book.book_id,
        "run_id": run_root.name,
        "run_kind": kind,
        "run_dir": str(run_root),
        "total_pages": total_pdf_pages,
        "pages": serialized_pages,
        "summary": summary,
        "total_spend_usd": total_spend,
    }
