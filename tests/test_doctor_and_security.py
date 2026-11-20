"""Adversarial tests for architecture_guard (7 AST anti-bloat laws), lanterns doctor, 5-plane isolation, and SDK versions."""

from __future__ import annotations

import importlib.metadata
import shutil
from pathlib import Path

import pytest

from lanterns.core.paths import RepoPaths
from lanterns.ops.doctor import run_doctor
from lanterns.validation.architecture_guard import evaluate_epistolary_exam, run_architecture_guard

REPO_ROOT = Path(__file__).resolve().parents[1]


def _copy_minimal_repo_tree(dst: Path) -> Path:
    """Create a fast isolated copy of src/, config/, schemas/, prompts/, docs/ in `dst` for mutation tests."""
    for rel in ("src", "config", "schemas", "collections", "prompts", "docs"):
        src_dir = REPO_ROOT / rel
        if src_dir.is_dir():
            shutil.copytree(
                src_dir,
                dst / rel,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
            )
    for root_file in ("pyproject.toml", "README.md", "AGENTS.md", "DECISIONS.md", "CATALOG_STATUS.md"):
        p = REPO_ROOT / root_file
        if p.is_file():
            shutil.copy2(p, dst / root_file)
    return dst


class TestRuntimeDependenciesAndFivePlaneIsolation:
    def test_google_genai_is_modern_3_2_plus_and_no_deprecated_fitz_or_generativeai(self) -> None:
        version_str = importlib.metadata.version("google-genai")
        parts = [int(x) for x in version_str.split(".")[:2]]
        assert (parts[0], parts[1]) >= (3, 2), f"Expected google-genai >= 3.2.0, got {version_str}"

        with pytest.raises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("google-generativeai")

        for py_file in (REPO_ROOT / "src" / "lanterns").rglob("*.py"):
            src = py_file.read_text(encoding="utf-8")
            assert "import fitz" not in src and "from fitz" not in src, (
                f"Deprecated `fitz` import found in {py_file}; use `import pymupdf`!"
            )
            assert "google.generativeai" not in src, f"Deprecated `google.generativeai` found in {py_file}!"

    def test_five_plane_runs_vs_experiments_strict_isolation(self, tmp_path: Path) -> None:
        paths = RepoPaths.from_root(tmp_path)
        exp_dir = paths.run_dir("newton-1756", "probe-01", kind="experiment")
        exp_dir.mkdir(parents=True)
        assert exp_dir == tmp_path / "workspace" / "experiments" / "newton-1756" / "probe-01"

        with pytest.raises(ValueError, match="Plane isolation violation"):
            paths.run_dir("newton-1756", "probe-01", kind="canonical")

        (tmp_path / "workspace" / "runs" / "newton-1756" / "probe-01").mkdir(parents=True)
        issues = paths.check_plane_isolation()
        assert len(issues) == 1
        assert "probe-01" in issues[0]


class TestArchitectureGuardAdversarialMutations:
    def test_real_repository_passes_architecture_guard_and_exam_cleanly(self) -> None:
        res = run_architecture_guard(REPO_ROOT)
        assert res["ok"] is True, f"Real repo violated architecture_guard: {res}"
        assert res["violation_count"] == 0

        exam_res = evaluate_epistolary_exam(REPO_ROOT)
        assert exam_res["manifest_present"] is True
        assert exam_res["ok"] is True, f"Stratified epistolary exam check failed: {exam_res}"

    def test_catches_planted_content_word_regex_in_epistolary(self, tmp_path: Path) -> None:
        fake_repo = _copy_minimal_repo_tree(tmp_path / "repo_regex")
        bad_file = fake_repo / "src" / "lanterns" / "epistolary" / "_planted_hack.py"
        bad_file.write_text(
            'import re\nBAD_RE = re.compile(r"^(?:my\\s+dear|yours\\s+sincerely)", re.I)\n',
            encoding="utf-8",
        )
        res = run_architecture_guard(fake_repo)
        assert res["ok"] is False, "architecture_guard failed to catch content-word regex in epistolary/!"

    def test_catches_planted_book_id_conditional(self, tmp_path: Path) -> None:
        fake_repo = _copy_minimal_repo_tree(tmp_path / "repo_book_id")
        bad_file = fake_repo / "src" / "lanterns" / "epistolary" / "_planted_book_branch.py"
        bad_file.write_text(
            'def route(book_id: str) -> bool:\n    if book_id == "erasmus-epistles":\n        return True\n    return False\n',
            encoding="utf-8",
        )
        res = run_architecture_guard(fake_repo)
        assert res["ok"] is False, "architecture_guard failed to catch `if book_id == ...` special-case!"

    def test_catches_planted_cyclomatic_complexity_and_branch_budget_gaming(self, tmp_path: Path) -> None:
        fake_repo = _copy_minimal_repo_tree(tmp_path / "repo_complexity")
        bad_file = fake_repo / "src" / "lanterns" / "epistolary" / "_planted_spaghetti.py"
        branches = "\n".join(f"    if x == {i}:\n        total += {i}" for i in range(25))
        bad_file.write_text(f"def mega_branch(x: int) -> int:\n    total = 0\n{branches}\n    return total\n", encoding="utf-8")
        res = run_architecture_guard(fake_repo)
        assert res["ok"] is False, "architecture_guard failed to catch cyclomatic complexity > 18!"

    def test_catches_planted_acyclic_layer_violation_and_silent_except(self, tmp_path: Path) -> None:
        fake_repo = _copy_minimal_repo_tree(tmp_path / "repo_layer")
        bad_layer = fake_repo / "src" / "lanterns" / "core" / "_planted_sdk_import.py"
        bad_layer.write_text("from google import genai\n", encoding="utf-8")
        res = run_architecture_guard(fake_repo)
        assert res["ok"] is False, "architecture_guard failed to catch core importing google.genai!"

        fake_repo2 = _copy_minimal_repo_tree(tmp_path / "repo_silent_except")
        bad_exc = fake_repo2 / "src" / "lanterns" / "epistolary" / "_planted_silent.py"
        bad_exc.write_text(
            "def swallow(fn):\n    try:\n        return fn()\n    except Exception:\n        pass\n",
            encoding="utf-8",
        )
        res2 = run_architecture_guard(fake_repo2)
        assert res2["ok"] is False, "architecture_guard failed to catch `except Exception: pass`!"


class TestDoctorMarkdownCapAndInnerHTMLBan:
    def test_doctor_catches_sprawl_markdown_and_innerhtml(self, tmp_path: Path) -> None:
        fake_repo = _copy_minimal_repo_tree(tmp_path / "repo_sprawl")
        (fake_repo / "RANDOM_NOTES_V2_FINAL.md").write_text("# Unapproved Sprawl\n", encoding="utf-8")
        res = run_doctor(fake_repo)
        assert res["ok"] is False, "lanterns doctor failed to catch unapproved root .md sprawl!"

        fake_repo2 = _copy_minimal_repo_tree(tmp_path / "repo_innerhtml")
        bad_renderer = fake_repo2 / "src" / "lanterns" / "renderers" / "_bad_dom.py"
        bad_renderer.write_text('JS = "panel.innerHTML = userText;"\n', encoding="utf-8")
        res2 = run_doctor(fake_repo2)
        assert res2["ok"] is False, "lanterns doctor failed to catch innerHTML in renderers/!"
