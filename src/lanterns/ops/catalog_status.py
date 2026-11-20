"""Master portfolio ledger (`CATALOG_STATUS.md`) generator and safe workspace garbage collector (`gc`).

ROLE: Inspect books/, workspace/{runs,experiments}/, and library/ to generate CATALOG_STATUS.md and reclaim cache space.
INPUTS: RepoPaths, BookConfig, run summaries, cost ledgers, edition manifests, and proof ledgers.
OUTPUTS: Root CATALOG_STATUS.md and structured status/gc dictionaries.
INVARIANT: Strictly separates canonical runs (workspace/runs/) from experiments (workspace/experiments/); never deletes source or paid IR.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lanterns.core.atomic import atomic_write_text
from lanterns.core.config import BookConfig
from lanterns.core.paths import RepoPaths


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _, filenames in os.walk(path):
        for fname in filenames:
            fp = Path(root) / fname
            if fp.is_file() and not fp.is_symlink():
                total += fp.stat().st_size
    return total


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _sum_cost_ledger_jsonl(path: Path) -> float:
    if not path.is_file():
        return 0.0
    total = 0.0
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            total += float(entry.get("cost_usd") or 0.0)
    return total


def _inspect_run_dir(run_dir: Path, book_id: str, kind: str) -> dict[str, Any]:
    summary = _read_json_dict(run_dir / "run_summary.json")
    ledger_json = _read_json_dict(run_dir / "cost_ledger.json")
    jsonl_spend = _sum_cost_ledger_jsonl(run_dir / "cost_ledger.jsonl")
    summary_spend = float(ledger_json.get("total_spent_usd") or summary.get("total_spent_usd") or 0.0)
    spend_usd = max(jsonl_spend, summary_spend)

    pages_extracted = int(summary.get("extracted_pages") or summary.get("pages_extracted") or 0)
    total_pages = int(summary.get("total_pages") or summary.get("pdf_page_count") or 0)
    pages_ir_file = run_dir / "ir" / "pages.json"
    blocks_ir_file = run_dir / "ir" / "blocks.json"
    raw_pages_dir = run_dir / "pages"
    if pages_extracted == 0:
        if pages_ir_file.is_file():
            ir_doc = _read_json_dict(pages_ir_file)
            pages_list = ir_doc.get("pages") if isinstance(ir_doc.get("pages"), list) else []
            pages_extracted = sum(1 for p in pages_list if isinstance(p, dict) and not p.get("failed_closed"))
            total_pages = total_pages or len(pages_list)
        elif raw_pages_dir.is_dir():
            pages_extracted = len(list(raw_pages_dir.glob("page_*.json")))

    has_ir = pages_ir_file.is_file() or blocks_ir_file.is_file() or (pages_extracted > 0)
    status = str(summary.get("status") or ("complete" if has_ir else "incomplete"))
    mtime = run_dir.stat().st_mtime
    ts_str = str(summary.get("completed_at") or summary.get("timestamp") or "")
    if not ts_str:
        ts_str = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    renders_bytes = _dir_size_bytes(run_dir / "renders") + _dir_size_bytes(run_dir / "evidence" / "renders")
    return {
        "book_id": book_id,
        "run_id": run_dir.name,
        "kind": kind,
        "status": status,
        "extracted_pages": pages_extracted,
        "total_pages": total_pages,
        "spend_usd": round(spend_usd, 4),
        "renders_bytes": renders_bytes,
        "has_ir": has_ir,
        "mtime": mtime,
        "timestamp": ts_str,
        "path": str(run_dir),
    }


def _next_action_for_tier(
    book_id: str,
    tier: str,
    active_run: str,
    hotspots: int,
    blocker_count: int,
) -> str:
    tier_actions: dict[str, str] = {
        "registered": f"Run `lanterns extract {book_id} --kind experiment`",
        "experiment_only": f"Promote to `--kind canonical` & `lanterns compile {book_id} --set-active`",
        "uncompiled": f"Run `lanterns compile {book_id} --run-id {active_run} --set-active`",
        "blocked": f"Resolve blockers (`{blocker_count}`) and re-compile",
    }
    if tier in tier_actions:
        return tier_actions[tier]
    if hotspots > 0:
        return f"Verify {hotspots} hotspot(s) in Proofing Studio"
    return "Ready for P1 → P2 community proofreading"


def _default_tier_from_runs(canon_runs: list[dict[str, Any]], exp_runs: list[dict[str, Any]]) -> str:
    if canon_runs:
        return "uncompiled"
    if exp_runs:
        return "experiment_only"
    return "registered"


def _compute_book_row(
    cfg: BookConfig,
    book_runs: list[dict[str, Any]],
    repo: RepoPaths,
) -> dict[str, Any]:
    canon_runs = [r for r in book_runs if r["kind"] == "canonical"]
    exp_runs = [r for r in book_runs if r["kind"] == "experiment"]

    ed_dir = repo.edition_dir(cfg.collection, cfg.book_id)
    manifest = _read_json_dict(ed_dir / "edition_manifest.json")
    rubric_doc = _read_json_dict(ed_dir / "rubric_score.json")
    ast_doc = _read_json_dict(ed_dir / "collection_ast.json")
    proof_doc = _read_json_dict(repo.book_dir(cfg.book_id) / "proof_ledger.json")

    active_run = cfg.active_run_id or str(manifest.get("active_run_id") or "")
    if not active_run and canon_runs:
        active_run = str(canon_runs[0]["run_id"])
    active_info = next((r for r in book_runs if r["run_id"] == active_run), {})

    pages_count = int(
        ast_doc.get("total_pages")
        or manifest.get("total_pages")
        or active_info.get("total_pages")
        or active_info.get("extracted_pages")
        or 0
    )
    ast_letters = ast_doc.get("letters")
    letters_count = int(manifest.get("letter_count") or (len(ast_letters) if isinstance(ast_letters, list) else 0))
    spend_total = round(sum(r["spend_usd"] for r in book_runs) or float(ast_doc.get("total_spend_usd") or 0.0), 4)

    score_val = rubric_doc.get("total_score", manifest.get("rubric_score"))
    tier = str(
        rubric_doc.get("provenance_tier")
        or manifest.get("provenance_tier")
        or _default_tier_from_runs(canon_runs, exp_runs)
    )
    hotspots = int(rubric_doc.get("hotspot_count") or manifest.get("hotspot_count") or 0)
    blockers = int(rubric_doc.get("blocker_count") or 1)

    ledger_pages = proof_doc.get("pages") if isinstance(proof_doc.get("pages"), dict) else {}
    verified_cnt = sum(
        1 for v in ledger_pages.values() if isinstance(v, dict) and v.get("status") in {"p1_proofed", "verified"}
    )
    proof_pct = round((verified_cnt * 100.0 / pages_count), 1) if pages_count > 0 else 0.0
    next_action = _next_action_for_tier(cfg.book_id, tier, active_run, hotspots, blockers)

    return {
        "book_id": cfg.book_id,
        "title": cfg.title,
        "year": cfg.publication_year,
        "collection": cfg.collection,
        "active_run_id": active_run or "—",
        "pages": pages_count,
        "letters_extracted": letters_count,
        "spend_usd": spend_total,
        "rubric_score": round(float(score_val), 1) if score_val is not None else None,
        "provenance_tier": tier,
        "hotspots": hotspots,
        "proofed_pct": proof_pct,
        "next_action": next_action,
    }


def _format_catalog_markdown(
    book_rows: list[dict[str, Any]],
    canon_runs: list[dict[str, Any]],
    exp_runs: list[dict[str, Any]],
) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    canon_spend = sum(r["spend_usd"] for r in canon_runs)
    exp_spend = sum(r["spend_usd"] for r in exp_runs)
    total_letters = sum(b["letters_extracted"] for b in book_rows)

    lines: list[str] = [
        "# Paper Lanterns — Master Correspondence Catalog & Run Ledger",
        "",
        f"*Auto-generated by `uv run lanterns status` at {now_utc}.*",
        "",
        f"- **Registered Volumes**: {len(book_rows)} · **Compiled Letters**: {total_letters}",
        f"- **Canonical Run Spend (`workspace/runs/`)**: `${canon_spend:.4f}` ({len(canon_runs)} run(s))",
        f"- **Experiment Spend (`workspace/experiments/`)**: `${exp_spend:.4f}` ({len(exp_runs)} experiment(s))",
        "",
        "## 1. Registered Correspondence Collections & Volumes",
        "",
        "| Book ID | Year | Collection | Active Run | Pages | Letters Extracted | Spend | Rubric Score | Provenance Tier | Hotspots | Proofed % | Next Action |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |",
    ]
    if not book_rows:
        lines.append("| *(none registered)* | — | — | — | 0 | 0 | $0.0000 | — | — | 0 | 0.0% | `lanterns init-book` |")
    for row in book_rows:
        yr = str(row["year"]) if row["year"] is not None else "n.d."
        score_txt = f"{row['rubric_score']:.1f}" if row["rubric_score"] is not None else "—"
        lines.append(
            f"| `{row['book_id']}` | {yr} | `{row['collection']}` | `{row['active_run_id']}` "
            f"| {row['pages']} | {row['letters_extracted']} | ${row['spend_usd']:.4f} "
            f"| {score_txt} | `{row['provenance_tier']}` | {row['hotspots']} "
            f"| {row['proofed_pct']:.1f}% | {row['next_action']} |"
        )

    lines.extend(
        [
            "",
            "## 2. Canonical Production Run Ledger (`workspace/runs/`)",
            "",
            "| Timestamp | Book ID | Run ID | Status | Extracted Pages | Spend (USD) |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    if not canon_runs:
        lines.append("| — | *(no canonical runs yet)* | — | — | 0 | $0.0000 |")
    for r in canon_runs:
        lines.append(
            f"| {r['timestamp']} | `{r['book_id']}` | `{r['run_id']}` | `{r['status']}` "
            f"| {r['extracted_pages']} | ${r['spend_usd']:.4f} |"
        )

    lines.extend(
        [
            "",
            "## 3. Empirical Experiment & Canary Ledger (`workspace/experiments/`)",
            "",
            "| Timestamp | Book ID | Experiment ID | Status | Extracted Pages | Spend (USD) |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    if not exp_runs:
        lines.append("| — | *(no experiments yet)* | — | — | 0 | $0.0000 |")
    for r in exp_runs:
        lines.append(
            f"| {r['timestamp']} | `{r['book_id']}` | `{r['run_id']}` | `{r['status']}` "
            f"| {r['extracted_pages']} | ${r['spend_usd']:.4f} |"
        )
    lines.append("")
    return "\n".join(lines)


def generate_catalog_status(
    repo_root: str | Path | None = None,
    *,
    write_markdown: bool = True,
) -> dict[str, Any]:
    """Scan `books/`, `workspace/{runs,experiments}/`, and `library/` to write `CATALOG_STATUS.md`."""
    repo = RepoPaths.from_root(repo_root)
    runs_by_book: dict[str, list[dict[str, Any]]] = {}
    canon_runs: list[dict[str, Any]] = []
    exp_runs: list[dict[str, Any]] = []

    for base_dir, kind, bucket in (
        (repo.runs_dir, "canonical", canon_runs),
        (repo.experiments_dir, "experiment", exp_runs),
    ):
        if not base_dir.is_dir():
            continue
        for b_dir in sorted(d for d in base_dir.iterdir() if d.is_dir() and not d.name.startswith("_")):
            for r_dir in sorted(d for d in b_dir.iterdir() if d.is_dir()):
                info = _inspect_run_dir(r_dir, b_dir.name, kind)
                bucket.append(info)
                runs_by_book.setdefault(b_dir.name, []).append(info)

    canon_runs.sort(key=lambda r: (r["mtime"], r["run_id"]), reverse=True)
    exp_runs.sort(key=lambda r: (r["mtime"], r["run_id"]), reverse=True)

    book_rows: list[dict[str, Any]] = []
    if repo.books_dir.is_dir():
        for b_dir in sorted(d for d in repo.books_dir.iterdir() if d.is_dir() and (d / "book.toml").is_file()):
            cfg = BookConfig.load(b_dir.name, paths=repo)
            b_runs = sorted(runs_by_book.get(cfg.book_id, []), key=lambda r: r["mtime"], reverse=True)
            book_rows.append(_compute_book_row(cfg, b_runs, repo))

    markdown = _format_catalog_markdown(book_rows, canon_runs, exp_runs)
    out_path = repo.root / "CATALOG_STATUS.md"
    if write_markdown:
        atomic_write_text(out_path, markdown)

    return {
        "catalog_status_path": str(out_path),
        "books": book_rows,
        "canonical_runs": canon_runs,
        "experiment_runs": exp_runs,
    }


_TERMINAL_BATCH_STATES: frozenset[str] = frozenset(
    {
        "completed",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
        "job_state_succeeded",
        "job_state_failed",
        "job_state_cancelled",
        "job_state_expired",
    }
)


def _is_active_run_dir(run_dir: Path) -> bool:
    if (run_dir / "run.lock").exists():
        return True
    batch_file = run_dir / "batch_jobs.json"
    if batch_file.is_file():
        data = _read_json_dict(batch_file)
        chunks = data.get("chunks") if isinstance(data.get("chunks"), dict) else {}
        for item in chunks.values():
            if not isinstance(item, dict):
                continue
            st = str(item.get("status") or item.get("state") or "").strip().lower()
            if st and st not in _TERMINAL_BATCH_STATES:
                return True
    return False


def garbage_collect_workspace(
    repo_root: str | Path | None = None,
    *,
    apply: bool = False,
    prune_previews: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Prune regenerable PNG render caches inside completed runs (dry-run by default)."""
    repo = RepoPaths.from_root(repo_root)
    ws_resolved = repo.workspace_dir.resolve()
    candidates: list[dict[str, Any]] = []
    skipped_active: list[str] = []

    for plane_dir in (repo.runs_dir, repo.experiments_dir):
        if not plane_dir.is_dir():
            continue
        for b_dir in sorted(d for d in plane_dir.iterdir() if d.is_dir()):
            for r_dir in sorted(d for d in b_dir.iterdir() if d.is_dir()):
                if not force and _is_active_run_dir(r_dir):
                    skipped_active.append(r_dir.resolve().relative_to(repo.root.resolve()).as_posix())
                    continue
                info = _inspect_run_dir(r_dir, b_dir.name, "run")
                if not info["has_ir"]:
                    continue
                for r_sub in (r_dir / "renders", r_dir / "evidence" / "renders"):
                    if r_sub.is_dir() and any(r_sub.iterdir()):
                        sz = _dir_size_bytes(r_sub)
                        candidates.append({"path": r_sub, "bytes": sz, "kind": "png_render_cache"})

    if prune_previews and repo.previews_dir.is_dir():
        for prev in sorted(d for d in repo.previews_dir.iterdir() if d.is_dir()):
            candidates.append({"path": prev, "bytes": _dir_size_bytes(prev), "kind": "preview_build"})

    reclaimed = 0
    deleted: list[str] = []
    for item in candidates:
        target: Path = item["path"].resolve()
        if ws_resolved not in target.parents:
            raise RuntimeError(f"Refusing to delete path outside workspace/: {target}")
        reclaimed += int(item["bytes"])
        rel = target.relative_to(repo.root.resolve()).as_posix()
        if apply and target.exists():
            shutil.rmtree(target)
            deleted.append(rel)

    return {
        "mode": "applied" if apply else "dry_run",
        "candidate_count": len(candidates),
        "reclaimable_bytes": reclaimed,
        "reclaimable_mb": round(reclaimed / (1024 * 1024), 2),
        "skipped_active": skipped_active,
        "candidates": [
            {"path": c["path"].resolve().relative_to(repo.root.resolve()).as_posix(), "bytes": c["bytes"], "kind": c["kind"]}
            for c in candidates
        ],
        "deleted": deleted,
    }
