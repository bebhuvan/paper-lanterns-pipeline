"""Crash-resumable 50%-discount Batch VLM extraction runner with per-page FallbackRouter rescue.

ROLE: Orchestrate chunked Anthropic Message Batches & Gemini Batch API jobs with durable job-id checkpointing.
INPUTS: Candidate page list, ProviderRequest builder, primary provider, FallbackRouter, and CostLedger.
OUTPUTS: Persisted `batch_jobs.json`, `raw_wire/page_NNNN.json`, `pages/page_NNNN.json`, and populated `page_map`.
INVARIANT: Never resubmits an already-checkpointed `batch_id` on `--resume`; rescues any failed batch item via `FallbackRouter`.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lanterns.core.atomic import atomic_write_json
from lanterns.core.cost import CostCeilingExceeded, CostLedger
from lanterns.core.models import PageIR
from lanterns.extraction.normalize import normalize_wire_to_page_ir
from lanterns.extraction.pdf_ingest import PagePreflight
from lanterns.providers.base import (
    BatchItemRequest,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResult,
    ProviderSuccess,
)
from lanterns.providers.router import FallbackRouter, make_failed_closed_page_ir

GEMINI_TERMINAL_STATES = frozenset(
    {
        "JOB_STATE_SUCCEEDED",
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "EXPIRED",
    }
)


def supports_batch_extraction(provider: Any) -> bool:
    """Return True when `provider` supports 50%-discounted batch submission (or mock test mode)."""
    return (
        hasattr(provider, "submit_message_batch")
        or hasattr(provider, "submit_batch_job")
        or getattr(provider, "name", "") == "mock"
    )


def _load_batch_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.is_file():
        return {"version": "batch-jobs-v1", "chunks": {}}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": "batch-jobs-v1", "chunks": {}}


def _chunk_pages(pages: list[int], chunk_size: int) -> list[list[int]]:
    step = max(1, int(chunk_size))
    return [pages[idx : idx + step] for idx in range(0, len(pages), step)]


def _submit_provider_chunk(
    provider: Any,
    chunk_id: str,
    reqs_by_cid: dict[str, ProviderRequest],
    staging_dir: Path,
) -> tuple[str, str]:
    if hasattr(provider, "submit_message_batch"):
        info = provider.submit_message_batch(list(reqs_by_cid.items()))
        return "anthropic", str(info["id"])
    if hasattr(provider, "submit_batch_job"):
        items = [BatchItemRequest(key=cid, request=req) for cid, req in reqs_by_cid.items()]
        job_name = provider.submit_batch_job(
            items,
            staging_dir / f"{chunk_id}.input.jsonl",
            display_name=f"lanterns-{chunk_id}",
        )
        return "gemini", job_name
    if getattr(provider, "name", "") == "mock":
        return "mock", f"mockbatch-{chunk_id}"
    raise ValueError(f"Provider {getattr(provider, 'name', provider)!r} does not support `--batch` mode.")


def _is_payload_too_large(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    return status == 413 or "413" in msg or "request_too_large" in msg


def _submit_provider_chunk_list(
    provider: Any,
    chunk_id: str,
    reqs_by_cid: dict[str, ProviderRequest],
    staging_dir: Path,
) -> list[tuple[str, str, str, list[int], dict[str, ProviderRequest]]]:
    try:
        backend, job_id = _submit_provider_chunk(provider, chunk_id, reqs_by_cid, staging_dir)
        pgs = [int(cid.split("-", 1)[1]) for cid in reqs_by_cid]
        return [(chunk_id, backend, job_id, pgs, reqs_by_cid)]
    except Exception as exc:
        if _is_payload_too_large(exc) and len(reqs_by_cid) > 1:
            items = list(reqs_by_cid.items())
            mid = len(items) // 2
            left = _submit_provider_chunk_list(provider, f"{chunk_id}a", dict(items[:mid]), staging_dir)
            right = _submit_provider_chunk_list(provider, f"{chunk_id}b", dict(items[mid:]), staging_dir)
            return [*left, *right]
        raise


def _check_chunk_finished(provider: Any, backend: str, job_id: str) -> tuple[bool, str]:
    if backend == "mock":
        return True, "ended"
    if backend == "anthropic":
        st = provider.retrieve_message_batch(job_id)
        proc = str(st.get("processing_status") or "")
        counts = st.get("request_counts") or {}
        summary = f"{proc} (ok={counts.get('succeeded', 0)} proc={counts.get('processing', 0)} err={counts.get('errored', 0)})"
        return proc == "ended", summary
    st = provider.retrieve_batch_job(job_id)
    state = str(st.get("state") or "")
    return state in GEMINI_TERMINAL_STATES, state


def _collect_provider_chunk(
    provider: Any,
    backend: str,
    job_id: str,
    reqs_by_cid: dict[str, ProviderRequest],
) -> dict[str, ProviderResult]:
    if backend == "anthropic":
        return provider.collect_message_batch_results(job_id, reqs_by_cid)
    if backend == "gemini":
        return provider.collect_batch_job_results(job_id, reqs_by_cid)
    out: dict[str, ProviderResult] = {}
    for cid, req in reqs_by_cid.items():
        res = provider.generate(req)
        if isinstance(res, ProviderSuccess):
            prov = dict(res.provenance or {})
            prov["is_batch"] = True
            prov["cost_estimate_usd"] = round(float(prov.get("cost_estimate_usd") or 0.0) * 0.5, 8)
            res = ProviderSuccess(data=res.data, raw_response=res.raw_response, provenance=prov)
        out[cid] = res
    return out


def _persist_batch_success(
    pnum: int,
    succ: ProviderSuccess,
    pf: PagePreflight,
    reserved_usd: float,
    ledger: CostLedger,
    raw_wire_dir: Path,
    pages_out_dir: Path,
) -> PageIR:
    prov = dict(succ.provenance or {})
    cost = float(prov.get("cost_estimate_usd") or 0.0)
    prov_name = str(prov.get("provider") or "")
    model_name = str(prov.get("model") or "")
    harness = str(prov.get("harness") or "batch")
    ledger.release_and_record(
        reserved_usd,
        stage="tier1_batch",
        provider=prov_name,
        model=model_name,
        harness=harness,
        page_number=pnum,
        cost_usd=cost,
        input_tokens=int(prov.get("input_tokens") or 0),
        cached_input_tokens=int(prov.get("cached_input_tokens") or 0),
        output_tokens=int(prov.get("output_tokens") or 0),
        thoughts_tokens=int(prov.get("thoughts_tokens") or 0),
        is_batch=True,
        status="ok",
    )
    extra_warns = ["jbig2_lossy_mask_risk"] if pf.uses_jbig2 else []
    atomic_write_json(
        raw_wire_dir / f"page_{pnum:04d}.json",
        {"page_number": pnum, "wire": succ.data, "provenance": prov, "attempts": [{"stage": "tier1_batch", "status": "ok"}]},
    )
    page_ir = normalize_wire_to_page_ir(
        succ.data,
        page_number=pnum,
        source_page_sha256=pf.source_page_sha256,
        provider=prov_name,
        model=model_name,
        harness=harness,
        rescued=False,
        cost_usd=cost,
        extra_warnings=extra_warns,
    )
    atomic_write_json(pages_out_dir / f"page_{pnum:04d}.json", page_ir.model_dump(mode="json"))
    return page_ir


def _rescue_failed_batch_page(
    pnum: int,
    req: ProviderRequest,
    err: ProviderError,
    pf: PagePreflight,
    reserved_usd: float,
    primary_provider: Any,
    router: FallbackRouter,
    ledger: CostLedger,
    raw_wire_dir: Path,
    pages_out_dir: Path,
) -> PageIR:
    eprov = err.provenance or {}
    ledger.release_and_record(
        reserved_usd,
        stage="tier1_batch",
        provider=str(eprov.get("provider") or getattr(primary_provider, "name", "")),
        model=str(eprov.get("model") or getattr(primary_provider, "model", "")),
        harness="batch",
        page_number=pnum,
        cost_usd=float(eprov.get("cost_estimate_usd") or 0.0),
        is_batch=True,
        status=f"error:{err.kind.value}",
    )
    outcome = router.execute_page(req, page_number=pnum, ledger=ledger, initial_error=err)
    if outcome.failed_closed or outcome.success is None:
        failed_ir = make_failed_closed_page_ir(
            pnum,
            outcome.failure_reason or err.message,
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
        rescued=True,
        cost_usd=outcome.total_cost_usd,
        extra_warnings=extra_warns,
    )
    atomic_write_json(pages_out_dir / f"page_{pnum:04d}.json", page_ir.model_dump(mode="json"))
    return page_ir


def _submit_single_chunk(
    *,
    c_idx: int,
    c_pages: list[int],
    primary_provider: Any,
    build_request_fn: Callable[[int], ProviderRequest],
    ledger: CostLedger,
    preflights: dict[int, PagePreflight],
    pages_out_dir: Path,
    page_map: dict[int, PageIR],
    chunks_meta: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    staging_dir: Path,
    resume: bool,
) -> tuple[list[tuple[str, str, str, list[int], dict[str, ProviderRequest], dict[int, float]]], bool]:
    chunk_id = f"chunk_{c_idx:03d}_p{c_pages[0]:04d}_p{c_pages[-1]:04d}"
    batch_page_est = round(ledger.default_reservation_usd * 0.5, 6)
    reserved_by_p: dict[int, float] = {}
    halted = False
    for pnum in c_pages:
        try:
            reserved_by_p[pnum] = ledger.acquire_slot(batch_page_est)
        except CostCeilingExceeded as exc:
            halted = True
            failed_ir = make_failed_closed_page_ir(pnum, str(exc), source_page_sha256=preflights[pnum].source_page_sha256)
            atomic_write_json(pages_out_dir / f"page_{pnum:04d}.json", failed_ir.model_dump(mode="json"))
            page_map[pnum] = failed_ir

    valid_pages = [p for p in c_pages if p in reserved_by_p]
    if not valid_pages:
        return [], halted
    reqs_by_cid = {f"page-{p:04d}": build_request_fn(p) for p in valid_pages}
    existing = chunks_meta.get(chunk_id) if resume else None
    if existing and existing.get("job_id") and existing.get("pages") == valid_pages:
        backend, job_id = str(existing["backend"]), str(existing["job_id"])
        print(f"[batch] Resuming {chunk_id} -> {job_id}", file=sys.stderr, flush=True)
        return [(chunk_id, backend, job_id, valid_pages, reqs_by_cid, reserved_by_p)], halted

    submitted = _submit_provider_chunk_list(primary_provider, chunk_id, reqs_by_cid, staging_dir)
    entries: list[tuple[str, str, str, list[int], dict[str, ProviderRequest], dict[int, float]]] = []
    for sub_cid, backend, job_id, sub_pages, sub_reqs in submitted:
        sub_res = {p: reserved_by_p[p] for p in sub_pages}
        chunks_meta[sub_cid] = {
            "backend": backend,
            "job_id": job_id,
            "pages": sub_pages,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "status": "submitted",
        }
        print(f"[batch] Submitted {sub_cid} ({len(sub_pages)} pages) -> {job_id}", file=sys.stderr, flush=True)
        entries.append((sub_cid, backend, job_id, sub_pages, sub_reqs, sub_res))
    atomic_write_json(manifest_path, manifest)
    return entries, halted


def _finalize_completed_chunk(
    *,
    cid: str,
    backend: str,
    jid: str,
    pgs: list[int],
    reqs: dict[str, ProviderRequest],
    res_map: dict[int, float],
    primary_provider: Any,
    router: FallbackRouter,
    ledger: CostLedger,
    preflights: dict[int, PagePreflight],
    raw_wire_dir: Path,
    pages_out_dir: Path,
    page_map: dict[int, PageIR],
) -> None:
    batch_results = _collect_provider_chunk(primary_provider, backend, jid, reqs)
    for pnum in pgs:
        key = f"page-{pnum:04d}"
        r_item = batch_results.get(key)
        if isinstance(r_item, ProviderSuccess):
            page_ir = _persist_batch_success(
                pnum, r_item, preflights[pnum], res_map[pnum], ledger, raw_wire_dir, pages_out_dir
            )
        else:
            err_obj = (
                r_item
                if isinstance(r_item, ProviderError)
                else ProviderError(ProviderErrorKind.UNKNOWN, f"Missing batch item {key}")
            )
            page_ir = _rescue_failed_batch_page(
                pnum,
                reqs[key],
                err_obj,
                preflights[pnum],
                res_map[pnum],
                primary_provider,
                router,
                ledger,
                raw_wire_dir,
                pages_out_dir,
            )
        page_map[pnum] = page_ir


def execute_batch_candidate_pages(
    *,
    to_extract: list[int],
    primary_provider: Any,
    build_request_fn: Callable[[int], ProviderRequest],
    router: FallbackRouter,
    ledger: CostLedger,
    preflights: dict[int, PagePreflight],
    run_root: Path,
    raw_wire_dir: Path,
    pages_out_dir: Path,
    page_map: dict[int, PageIR],
    resume: bool = True,
    chunk_size: int = 25,
    poll_interval_s: float = 10.0,
    max_wait_s: float = 7200.0,
) -> None:
    """Submit, checkpoint, poll, and collect 50%-discounted VLM batches across `to_extract`."""
    if not to_extract:
        return
    if not supports_batch_extraction(primary_provider):
        raise ValueError(
            f"Primary provider {getattr(primary_provider, 'name', primary_provider)!r} does not support `--batch`."
        )

    manifest_path = run_root / "batch_jobs.json"
    staging_dir = run_root / "batch_staging"
    manifest = _load_batch_manifest(manifest_path) if resume else {"version": "batch-jobs-v1", "chunks": {}}
    chunks_meta: dict[str, Any] = manifest.setdefault("chunks", {})

    pending: dict[str, tuple[str, str, list[int], dict[str, ProviderRequest], dict[int, float]]] = {}
    for c_idx, c_pages in enumerate(_chunk_pages(to_extract, chunk_size), start=1):
        entries, halted = _submit_single_chunk(
            c_idx=c_idx,
            c_pages=c_pages,
            primary_provider=primary_provider,
            build_request_fn=build_request_fn,
            ledger=ledger,
            preflights=preflights,
            pages_out_dir=pages_out_dir,
            page_map=page_map,
            chunks_meta=chunks_meta,
            manifest=manifest,
            manifest_path=manifest_path,
            staging_dir=staging_dir,
            resume=resume,
        )
        for entry in entries:
            pending[entry[0]] = entry[1:]
        if halted:
            break

    deadline = time.monotonic() + max_wait_s
    while pending:
        for cid, (backend, jid, pgs, reqs, res_map) in list(pending.items()):
            done, status_txt = _check_chunk_finished(primary_provider, backend, jid)
            print(f"[batch] {cid} ({jid}): {status_txt}", file=sys.stderr, flush=True)
            if done:
                _finalize_completed_chunk(
                    cid=cid,
                    backend=backend,
                    jid=jid,
                    pgs=pgs,
                    reqs=reqs,
                    res_map=res_map,
                    primary_provider=primary_provider,
                    router=router,
                    ledger=ledger,
                    preflights=preflights,
                    raw_wire_dir=raw_wire_dir,
                    pages_out_dir=pages_out_dir,
                    page_map=page_map,
                )
                chunks_meta[cid]["status"] = "completed"
                chunks_meta[cid]["completed_at"] = datetime.now(timezone.utc).isoformat()
                atomic_write_json(manifest_path, manifest)
                pending.pop(cid, None)
        if pending:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Batch jobs still pending after {max_wait_s}s: {sorted(pending)}")
            time.sleep(poll_interval_s)
