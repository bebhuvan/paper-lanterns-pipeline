"""Repository health, security, anti-sprawl, and architectural invariant gate (`lanterns doctor`).

ROLE: Verify documentation allowlists, zero-XSS DOM rules, 5-plane isolation, SDK floors, JSON schemas, and AST guards.
INPUTS: Repository filesystem at RepoPaths.from_root(repo_root).
OUTPUTS: Structured report `{"ok": bool, "error_count": int, "warning_count": int, "issues": [...]}`.
INVARIANT: Fails closed (`ok=False`) on any sprawl, security, schema, dependency, plane-isolation, or AST-guard violation.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
from pathlib import Path
from typing import Any

import jsonschema

from lanterns.core.config import load_profiles
from lanterns.core.paths import RepoPaths
from lanterns.core.pricing import ensure_priced_routes

ALLOWED_ROOT_MARKDOWN: frozenset[str] = frozenset(
    {
        "README.md",
        "AGENTS.md",
        "DECISIONS.md",
        "CATALOG_STATUS.md",
    }
)

ALLOWED_DOCS_MARKDOWN: frozenset[str] = frozenset(
    {
        "FOUNDER_INTENT_AND_MANIFESTO.md",
        "ARCHITECTURE.md",
        "ARCHIVAL_STANDARDS_AND_RUBRIC.md",
        "COMMUNITY_PROOFING_DESIGN.md",
        "ADVERSARIAL_REVIEW_FRAMEWORK.md",
        "LAB_NOTEBOOK.md",
    }
)

REQUIRED_SCHEMA_FILES: tuple[str, ...] = (
    "page_extract_wire.schema.json",
    "block_ir.schema.json",
    "collection_ast.schema.json",
    "letter_map.schema.json",
    "correction_v2.schema.json",
    "proof_ledger.schema.json",
)

# Built via concatenation so literal XSS sink tokens never appear inside renderers/ source files.
_UNSAFE_DOM_SINKS: tuple[str, ...] = (
    "inner" + "HTML",
    "outer" + "HTML",
    "insertAdjacent" + "HTML",
    "document." + "write(",
)

_BOOK_ID_BRANCH_RE = re.compile(r"\bif\s+[^:\n]*\bbook_id\b\s*(?:==|!=|\bin\b)")


def _check_markdown_allowlists(repo: RepoPaths, issues: list[dict[str, Any]]) -> None:
    for md_file in sorted(repo.root.glob("*.md")):
        if md_file.name not in ALLOWED_ROOT_MARKDOWN:
            issues.append(
                {
                    "severity": "error",
                    "check": "markdown_sprawl",
                    "path": md_file.name,
                    "message": f"Unauthorized root Markdown file `{md_file.name}`; allowed: {sorted(ALLOWED_ROOT_MARKDOWN)}",
                }
            )

    docs_dir = repo.root / "docs"
    if not docs_dir.is_dir():
        return
    allowed_ref_dirs = {
        (docs_dir / "reference" / sub).resolve()
        for sub in ("gemini-api", "qwen-api", "claude-api")
    }
    for path in sorted(docs_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(docs_dir).as_posix()
        if path.parent.resolve() in allowed_ref_dirs and (path.suffix == ".md" or path.name == "llms.txt"):
            continue
        if path.parent.resolve() == docs_dir.resolve() and path.name in ALLOWED_DOCS_MARKDOWN:
            continue
        if path.suffix.lower() in {".md", ".txt"}:
            issues.append(
                {
                    "severity": "error",
                    "check": "markdown_sprawl",
                    "path": f"docs/{rel}",
                    "message": (
                        f"Unauthorized docs file `docs/{rel}`; allowed in docs/: {sorted(ALLOWED_DOCS_MARKDOWN)} "
                        "+ docs/reference/{gemini-api,qwen-api,claude-api}/"
                    ),
                }
            )


def _check_unsafe_dom_and_antipatterns(repo: RepoPaths, issues: list[dict[str, Any]]) -> None:
    src_lanterns = repo.root / "src" / "lanterns"
    renderers_dir = src_lanterns / "renderers"
    if renderers_dir.is_dir():
        for py_file in sorted(renderers_dir.rglob("*.py")):
            text = py_file.read_text(encoding="utf-8")
            rel = py_file.relative_to(repo.root).as_posix()
            for sink in _UNSAFE_DOM_SINKS:
                if sink in text:
                    issues.append(
                        {
                            "severity": "error",
                            "check": "zero_inner_html",
                            "path": rel,
                            "message": f"Forbidden unsafe DOM sink `{sink}` found in `{rel}`",
                        }
                    )

    for sub_name in ("epistolary", "renderers"):
        sub_dir = src_lanterns / sub_name
        if not sub_dir.is_dir():
            continue
        for py_file in sorted(sub_dir.rglob("*.py")):
            text = py_file.read_text(encoding="utf-8")
            rel = py_file.relative_to(repo.root).as_posix()
            if _BOOK_ID_BRANCH_RE.search(text):
                issues.append(
                    {
                        "severity": "error",
                        "check": "anti_pattern_book_id",
                        "path": rel,
                        "message": f"Forbidden per-book conditional on `book_id` in `{rel}`",
                    }
                )


def _check_plane_isolation(repo: RepoPaths, issues: list[dict[str, Any]]) -> None:
    for msg in repo.check_plane_isolation():
        issues.append({"severity": "error", "check": "plane_isolation", "path": "workspace/", "message": msg})

    if repo.runs_dir.is_dir():
        for summary_file in sorted(repo.runs_dir.rglob("run_summary.json")):
            try:
                doc = json.loads(summary_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(doc, dict) and doc.get("kind") == "experiment":
                rel = summary_file.relative_to(repo.root).as_posix()
                issues.append(
                    {
                        "severity": "error",
                        "check": "plane_isolation",
                        "path": rel,
                        "message": f"Experiment run summary found inside canonical `workspace/runs/`: `{rel}`",
                    }
                )

    if repo.experiments_dir.is_dir():
        for summary_file in sorted(repo.experiments_dir.rglob("run_summary.json")):
            try:
                doc = json.loads(summary_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(doc, dict) and doc.get("kind") == "canonical":
                rel = summary_file.relative_to(repo.root).as_posix()
                issues.append(
                    {
                        "severity": "error",
                        "check": "plane_isolation",
                        "path": rel,
                        "message": f"Canonical run summary found inside `workspace/experiments/`: `{rel}`",
                    }
                )


def _parse_version_tuple(raw_ver: str) -> tuple[int, int, int]:
    nums: list[int] = []
    for part in raw_ver.split(".")[:3]:
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _check_sdk_version(issues: list[dict[str, Any]], *, version_override: str | None = None) -> str:
    try:
        ver = version_override if version_override is not None else importlib.metadata.version("google-genai")
    except importlib.metadata.PackageNotFoundError:
        issues.append(
            {
                "severity": "error",
                "check": "sdk_version",
                "path": "pyproject.toml",
                "message": "`google-genai` package is not installed; `google-genai >= 3.2.0` is required.",
            }
        )
        return "missing"

    if _parse_version_tuple(ver) < (3, 2, 0):
        issues.append(
            {
                "severity": "error",
                "check": "sdk_version",
                "path": "pyproject.toml",
                "message": f"`google-genai` version `{ver}` is outdated; `>= 3.2.0` is required.",
            }
        )

    try:
        legacy_ver = importlib.metadata.version("google-generativeai")
        issues.append(
            {
                "severity": "error",
                "check": "deprecated_sdk",
                "path": "pyproject.toml",
                "message": f"Deprecated `google-generativeai` ({legacy_ver}) is installed; use `google-genai >= 3.2.0` exclusively.",
            }
        )
    except importlib.metadata.PackageNotFoundError:
        pass

    try:
        anth_ver = importlib.metadata.version("anthropic")
        if _parse_version_tuple(anth_ver) < (2, 8, 0):
            issues.append(
                {
                    "severity": "error",
                    "check": "sdk_version",
                    "path": "pyproject.toml",
                    "message": f"`anthropic` version `{anth_ver}` is outdated; `>= 2.8.0` is required.",
                }
            )
    except importlib.metadata.PackageNotFoundError:
        issues.append(
            {
                "severity": "error",
                "check": "sdk_version",
                "path": "pyproject.toml",
                "message": "`anthropic` package is not installed; `anthropic >= 2.8.0` is required.",
            }
        )
    return ver


def _check_schemas(repo: RepoPaths, issues: list[dict[str, Any]]) -> None:
    is_full_repo = (repo.root / "pyproject.toml").is_file()
    if not repo.schemas_dir.is_dir():
        if is_full_repo:
            issues.append(
                {
                    "severity": "error",
                    "check": "schemas",
                    "path": "schemas/",
                    "message": "`schemas/` directory is missing.",
                }
            )
        return

    if is_full_repo:
        for req in REQUIRED_SCHEMA_FILES:
            if not (repo.schemas_dir / req).is_file():
                issues.append(
                    {
                        "severity": "error",
                        "check": "schemas",
                        "path": f"schemas/{req}",
                        "message": f"Required JSON Schema `schemas/{req}` is missing.",
                    }
                )

    for schema_path in sorted(repo.schemas_dir.glob("*.json")):
        rel = schema_path.relative_to(repo.root).as_posix()
        try:
            doc = json.loads(schema_path.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(doc)
        except (OSError, json.JSONDecodeError, jsonschema.exceptions.SchemaError) as exc:
            issues.append(
                {
                    "severity": "error",
                    "check": "schemas",
                    "path": rel,
                    "message": f"Invalid Draft 2020-12 JSON Schema `{rel}`: {exc}",
                }
            )


def _check_architecture_and_editions(repo: RepoPaths, issues: list[dict[str, Any]]) -> dict[str, Any]:
    arch_report: dict[str, Any] = {"ok": True, "violation_count": 0, "violations": []}
    if (repo.root / "src" / "lanterns").is_dir():
        try:
            from lanterns.validation.architecture_guard import run_architecture_guard

            arch_report = run_architecture_guard(repo.root)
            for v in arch_report.get("violations") or []:
                issues.append(
                    {
                        "severity": "error",
                        "check": f"architecture:{v.get('rule', 'guard')}",
                        "path": str(v.get("file") or v.get("path") or "src/lanterns"),
                        "message": str(v.get("message") or v),
                    }
                )
        except ImportError:
            pass

    if repo.library_dir.is_dir():
        try:
            from lanterns.validation.compiled_gate import validate_compiled_edition

            for manifest_file in sorted(repo.library_dir.rglob("edition_manifest.json")):
                ed_dir = manifest_file.parent
                res = validate_compiled_edition(ed_dir, require_proofing=False)
                for err in res.get("errors") or []:
                    issues.append(
                        {
                            "severity": "error",
                            "check": "compiled_edition",
                            "path": str(err.get("file") or ed_dir.relative_to(repo.root).as_posix()),
                            "message": str(err.get("message") or err),
                        }
                    )
        except ImportError:
            pass
    return arch_report


def _check_profiles_and_runtime(repo: RepoPaths, issues: list[dict[str, Any]]) -> None:
    profiles_path = repo.config_dir / "profiles.json"
    pricing_path = repo.config_dir / "pricing.json"
    if profiles_path.is_file() and pricing_path.is_file():
        try:
            profiles = load_profiles(str(repo.root))
            routes: list[str] = []
            for pdata in profiles.values():
                for key in ("primary", "hotspot_model", "boundary_model", "decision_model"):
                    if pdata.get(key):
                        routes.append(str(pdata[key]))
                routes.extend(str(r) for r in (pdata.get("fallback_chain") or []) if r)
            ensure_priced_routes(routes, root=str(repo.root))
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
            issues.append(
                {
                    "severity": "error",
                    "check": "pricing_routes",
                    "path": "config/profiles.json",
                    "message": str(exc),
                }
            )
    if (repo.config_dir / "profiles.json").is_file() and not (
        os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    ):
        issues.append(
            {
                "severity": "warning",
                "check": "api_keys",
                "path": ".env",
                "message": "Neither GEMINI_API_KEY nor ANTHROPIC_API_KEY is set; live API extraction disabled.",
            }
        )


def run_doctor(
    repo_root: str | Path | None = None,
    *,
    genai_version_override: str | None = None,
) -> dict[str, Any]:
    """Execute all repository, security, dependency, plane-isolation, schema, and AST health checks."""
    repo = RepoPaths.from_root(repo_root)
    issues: list[dict[str, Any]] = []

    _check_markdown_allowlists(repo, issues)
    _check_unsafe_dom_and_antipatterns(repo, issues)
    _check_plane_isolation(repo, issues)
    sdk_ver = _check_sdk_version(issues, version_override=genai_version_override)
    _check_schemas(repo, issues)
    _check_profiles_and_runtime(repo, issues)
    arch_report = _check_architecture_and_editions(repo, issues)

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    return {
        "ok": len(errors) == 0,
        "google_genai_version": sdk_ver,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "issues": issues,
        "architecture": arch_report,
    }
