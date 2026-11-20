"""Five-plane repository path resolution and strict experiment/canonical run isolation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

VALID_RUN_KINDS: frozenset[str] = frozenset({"canonical", "experiment"})


def repo_root(override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class RepoPaths:
    """Enforces the 5-plane Paper Lanterns v2 repository layout.

    Planes:
      1. `sources/` — immutable or symlinked input PDFs/JP2s (gitignored)
      2. `books/<book_id>/` & `collections/` — declarative metadata, maps, human corrections
      3. `workspace/` — regenerable `runs/` (canonical) vs `experiments/` (tests) vs `builds/previews/`
      4. `library/<collection>/<book_id>/` — published multi-format editions
      5. `config/`, `schemas/`, `prompts/`, `benchmarks/` — engine definitions
    """

    root: Path

    @classmethod
    def from_root(cls, root: str | Path | None = None) -> RepoPaths:
        return cls(repo_root(root))

    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    @property
    def books_dir(self) -> Path:
        return self.root / "books"

    @property
    def collections_dir(self) -> Path:
        return self.root / "collections"

    @property
    def workspace_dir(self) -> Path:
        return self.root / "workspace"

    @property
    def runs_dir(self) -> Path:
        return self.workspace_dir / "runs"

    @property
    def experiments_dir(self) -> Path:
        return self.workspace_dir / "experiments"

    @property
    def builds_dir(self) -> Path:
        return self.workspace_dir / "builds"

    @property
    def previews_dir(self) -> Path:
        return self.builds_dir / "previews"

    @property
    def library_dir(self) -> Path:
        return self.root / "library"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def schemas_dir(self) -> Path:
        return self.root / "schemas"

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    @property
    def benchmarks_dir(self) -> Path:
        return self.root / "benchmarks"

    def book_dir(self, book_id: str) -> Path:
        return self.books_dir / book_id

    def scan_checks_dir(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "scan_checks"

    def scan_overrides_dir(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "scan_overrides"

    def audit_checks_path(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "audit_checks.jsonl"

    def corrections_dir(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "corrections"

    def structure_overrides_path(self, book_id: str) -> Path:
        return self.book_dir(book_id) / "structure_overrides.json"

    def structure_auto_path(self, run_dir: str | Path) -> Path:
        return Path(run_dir) / "ir" / "structure_auto.json"

    def collection_path(self, slug: str) -> Path:
        return self.collections_dir / f"{slug}.toml"

    def run_dir(self, book_id: str, run_id: str, *, kind: str = "canonical") -> Path:
        if kind not in VALID_RUN_KINDS:
            raise ValueError(f"Unknown run kind {kind!r}; expected one of {sorted(VALID_RUN_KINDS)}")
        canonical_path = self.runs_dir / book_id / run_id
        experiment_path = self.experiments_dir / book_id / run_id
        if kind == "experiment":
            if canonical_path.exists():
                raise ValueError(
                    f"Plane isolation violation: run_id {run_id!r} for {book_id!r} already exists in canonical runs/"
                )
            return experiment_path
        if experiment_path.exists():
            raise ValueError(
                f"Plane isolation violation: run_id {run_id!r} for {book_id!r} already exists in experiments/"
            )
        return canonical_path

    def resolve_run_dir_with_kind(self, book_id: str, run_id: str) -> tuple[Path, str]:
        canonical_path = self.runs_dir / book_id / run_id
        experiment_path = self.experiments_dir / book_id / run_id
        if canonical_path.exists() and experiment_path.exists():
            raise ValueError(
                f"Plane isolation violation: {book_id}/{run_id} exists in BOTH workspace/runs/ and workspace/experiments/"
            )
        if canonical_path.exists():
            return canonical_path, "canonical"
        if experiment_path.exists():
            return experiment_path, "experiment"
        return canonical_path, "canonical"

    def find_run_dir(self, book_id: str, run_id: str) -> Path:
        path, _ = self.resolve_run_dir_with_kind(book_id, run_id)
        return path

    def preview_edition_dir(self, book_id: str, run_id: str) -> Path:
        return self.previews_dir / book_id / run_id

    def edition_dir(self, collection: str, book_id: str) -> Path:
        return self.library_dir / collection / book_id

    def check_plane_isolation(self) -> list[str]:
        """Verify zero run_id collisions or cross-links between `workspace/runs` and `workspace/experiments`."""
        issues: list[str] = []
        if not self.runs_dir.is_dir() or not self.experiments_dir.is_dir():
            return issues
        for book_runs in sorted(self.runs_dir.iterdir()):
            if not book_runs.is_dir():
                continue
            exp_book = self.experiments_dir / book_runs.name
            if not exp_book.is_dir():
                continue
            canonical_ids = {p.name for p in book_runs.iterdir() if p.is_dir()}
            experiment_ids = {p.name for p in exp_book.iterdir() if p.is_dir()}
            overlap = sorted(canonical_ids & experiment_ids)
            for rid in overlap:
                issues.append(
                    f"Duplicate run_id {rid!r} for book {book_runs.name!r} in both workspace/runs/ and workspace/experiments/"
                )
        return issues
