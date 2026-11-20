"""Declarative TOML configuration, profile resolution, prompt loader, and JSON Schema helpers."""

from __future__ import annotations

import functools
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from lanterns.core.atomic import atomic_write_text
from lanterns.core.paths import RepoPaths


class SchemaValidationError(ValueError):
    """Raised when JSON payload violates a Draft 2020-12 schema in `schemas/`."""


@functools.lru_cache(maxsize=32)
def load_schema(schema_name: str, root: str | None = None) -> dict[str, Any]:
    filename = schema_name if schema_name.endswith(".json") else f"{schema_name}.schema.json"
    path = RepoPaths.from_root(root).schemas_dir / filename
    if not path.is_file():
        raise FileNotFoundError(f"Schema not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=32)
def load_prompt(prompt_name: str, root: str | None = None) -> str:
    filename = prompt_name if prompt_name.endswith(".md") else f"{prompt_name}.md"
    path = RepoPaths.from_root(root).prompts_dir / filename
    if not path.is_file():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def gemini_clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip `$schema` and `$id` keywords for strict VLM structured-output transport compatibility."""

    def _clean(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _clean(v) for k, v in node.items() if k not in {"$schema", "$id"}}
        if isinstance(node, list):
            return [_clean(item) for item in node]
        return node

    return _clean(schema)


def validate_data(schema_name: str, data: Any, *, root: str | None = None) -> None:
    schema = load_schema(schema_name, root=root)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda err: list(err.absolute_path))
    if errors:
        first = errors[0]
        loc = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise SchemaValidationError(f"{schema_name} invalid at {loc}: {first.message}")


@functools.lru_cache(maxsize=8)
def load_profiles(root: str | None = None) -> dict[str, dict[str, Any]]:
    path = RepoPaths.from_root(root).config_dir / "profiles.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def get_profile_settings(profile_name: str, *, root: str | None = None) -> dict[str, Any]:
    profiles = load_profiles(root)
    if profile_name not in profiles:
        available = ", ".join(sorted(profiles)) or "<none>"
        raise KeyError(f"Unknown reader profile {profile_name!r}; available: {available}")
    return dict(profiles[profile_name])


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


@dataclass
class CollectionConfig:
    slug: str
    title: str
    subtitle: str = ""
    language_focus: list[str] = field(default_factory=lambda: ["en"])
    source_prep: dict[str, Any] = field(default_factory=dict)
    extraction: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    publishing: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, slug: str, paths: RepoPaths | None = None) -> CollectionConfig:
        repo = paths or RepoPaths.from_root()
        toml_path = repo.collection_path(slug)
        if not toml_path.is_file():
            raise FileNotFoundError(f"Collection config not found: {toml_path}")
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        return cls(
            slug=str(raw.get("slug") or slug),
            title=str(raw.get("title") or slug),
            subtitle=str(raw.get("subtitle") or ""),
            language_focus=list(raw.get("language_focus") or ["en"]),
            source_prep=dict(raw.get("source_prep") or {}),
            extraction=dict(raw.get("extraction") or {}),
            validation=dict(raw.get("validation") or {}),
            publishing=dict(raw.get("publishing") or {}),
        )

    def save(self, paths: RepoPaths | None = None) -> Path:
        repo = paths or RepoPaths.from_root()
        target = repo.collection_path(self.slug)
        lines = [
            f"slug = {_toml_scalar(self.slug)}",
            f"title = {_toml_scalar(self.title)}",
            f"subtitle = {_toml_scalar(self.subtitle)}",
            f"language_focus = {_toml_scalar(self.language_focus)}",
        ]
        for sec_name, sec_dict in (
            ("source_prep", self.source_prep),
            ("extraction", self.extraction),
            ("validation", self.validation),
            ("publishing", self.publishing),
        ):
            if sec_dict:
                lines.extend(["", f"[{sec_name}]"])
                for k, v in sec_dict.items():
                    if v is not None:
                        lines.append(f"{k} = {_toml_scalar(v)}")
        return atomic_write_text(target, "\n".join(lines) + "\n")


def _first_str(*values: Any, default: str = "") -> str:
    for val in values:
        if val:
            return str(val)
    return default


@dataclass
class BookConfig:
    book_id: str
    title: str
    author_or_editor: str = ""
    authors: list[str] = field(default_factory=list)
    publication_year: int | None = None
    collection: str = "world-correspondence"
    language: str = "en"
    subtitle: str = ""
    imprint: str = ""
    archive_id: str = ""
    source_pdf: str = ""
    source_pdf_sha256: str = ""
    declared_scripts: list[str] = field(default_factory=list)
    page_count: int | None = None
    active_run_id: str = ""
    profile: str = "english_epistolary"
    extraction_overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def year(self) -> int | None:
        return self.publication_year

    @classmethod
    def load(cls, book_id: str, paths: RepoPaths | None = None) -> BookConfig:
        repo = paths or RepoPaths.from_root()
        toml_path = repo.book_dir(book_id) / "book.toml"
        if not toml_path.is_file():
            raise FileNotFoundError(f"Book config not found: {toml_path}")
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        meta = raw.get("book") if isinstance(raw.get("book"), dict) else raw
        source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        ext = dict(raw.get("extraction") or {})
        authors_list = list(meta.get("authors") or [])
        author_str = _first_str(meta.get("author_or_editor"), meta.get("author"), ", ".join(authors_list))
        if author_str and not authors_list:
            authors_list = [author_str]
        year_raw = meta.get("publication_year", meta.get("year"))
        profile_name = _first_str(
            ext.get("profile"), ext.get("reader_profile"), meta.get("profile"), default="english_epistolary"
        )
        return cls(
            book_id=_first_str(meta.get("book_id"), default=book_id),
            title=_first_str(meta.get("title"), default=book_id),
            author_or_editor=author_str,
            authors=authors_list,
            publication_year=int(year_raw) if year_raw is not None else None,
            subtitle=_first_str(meta.get("subtitle")),
            imprint=_first_str(meta.get("imprint")),
            collection=_first_str(meta.get("collection"), default="world-correspondence"),
            language=_first_str(meta.get("language"), default="en"),
            declared_scripts=list(meta.get("declared_scripts") or []),
            page_count=int(meta["page_count"]) if meta.get("page_count") is not None else None,
            active_run_id=_first_str(meta.get("active_run_id")),
            archive_id=_first_str(source.get("archive_id"), meta.get("archive_id")),
            source_pdf=_first_str(source.get("source_pdf"), meta.get("source_pdf")),
            source_pdf_sha256=_first_str(source.get("source_pdf_sha256"), meta.get("source_pdf_sha256")),
            profile=profile_name,
            extraction_overrides=ext,
        )

    def resolved_extraction_settings(
        self,
        paths: RepoPaths | None = None,
        *,
        profile_override: str | None = None,
    ) -> dict[str, Any]:
        """Merge collection defaults -> profile settings -> book explicit overrides."""
        repo = paths or RepoPaths.from_root()
        merged: dict[str, Any] = {}
        coll_path = repo.collection_path(self.collection)
        if coll_path.is_file():
            coll = CollectionConfig.load(self.collection, paths=repo)
            merged.update(coll.extraction)

        eff_profile = (
            profile_override
            or self.extraction_overrides.get("profile")
            or self.extraction_overrides.get("reader_profile")
            or self.profile
            or merged.get("profile")
        )
        if eff_profile:
            profiles = load_profiles(str(repo.root))
            if str(eff_profile) in profiles:
                merged.update(profiles[str(eff_profile)])
            merged["profile"] = str(eff_profile)

        merged.update(self.extraction_overrides)
        if profile_override:
            profiles = load_profiles(str(repo.root))
            if profile_override in profiles:
                merged.update(profiles[profile_override])
            merged["profile"] = profile_override
        return merged

    def resolve_run_id(self, explicit_run_id: str | None = None) -> str:
        chosen = (explicit_run_id or self.active_run_id or "").strip()
        if not chosen:
            raise ValueError(f"No `--run-id` provided and `active_run_id` is unset in books/{self.book_id}/book.toml.")
        return chosen

    def _candidate_source_pdfs(self, repo: RepoPaths) -> list[Path]:
        cands = [
            repo.sources_dir / f"{self.book_id}.originals.pdf",
            repo.sources_dir / f"{self.book_id}.pdf",
        ]
        if self.archive_id:
            cands.extend(
                [
                    repo.sources_dir / self.archive_id / f"{self.archive_id}.originals.pdf",
                    repo.sources_dir / self.archive_id / f"{self.archive_id}.pdf",
                    repo.sources_dir / f"{self.archive_id}.originals.pdf",
                ]
            )
        return cands

    def resolve_source_pdf_path(
        self,
        explicit_pdf: str | Path | None = None,
        paths: RepoPaths | None = None,
    ) -> Path | None:
        repo = paths or RepoPaths.from_root()
        raw = Path(explicit_pdf) if explicit_pdf else (Path(self.source_pdf) if self.source_pdf else None)
        if raw is not None:
            if raw.is_file():
                return raw
            cand = repo.root / raw
            if cand.is_file():
                return cand
        if not explicit_pdf:
            for fallback in self._candidate_source_pdfs(repo):
                if fallback.is_file():
                    return fallback
        return raw

    def save(self, paths: RepoPaths | None = None) -> Path:
        repo = paths or RepoPaths.from_root()
        target = repo.book_dir(self.book_id) / "book.toml"
        authors_val = self.authors or ([self.author_or_editor] if self.author_or_editor else [])
        author_str = self.author_or_editor or (", ".join(authors_val))
        lines = [
            "[book]",
            f"book_id = {_toml_scalar(self.book_id)}",
            f"title = {_toml_scalar(self.title)}",
            f"author_or_editor = {_toml_scalar(author_str)}",
            f"authors = {_toml_scalar(authors_val)}",
            *([f"publication_year = {self.publication_year}"] if self.publication_year is not None else []),
            f"subtitle = {_toml_scalar(self.subtitle)}",
            f"imprint = {_toml_scalar(self.imprint)}",
            f"collection = {_toml_scalar(self.collection)}",
            f"language = {_toml_scalar(self.language)}",
            f"declared_scripts = {_toml_scalar(self.declared_scripts)}",
            f"profile = {_toml_scalar(self.profile)}",
            *([f"active_run_id = {_toml_scalar(self.active_run_id)}"] if self.active_run_id else []),
            "",
            "[source]",
            f"archive_id = {_toml_scalar(self.archive_id)}",
            f"source_pdf = {_toml_scalar(self.source_pdf)}",
            f"source_pdf_sha256 = {_toml_scalar(self.source_pdf_sha256)}",
        ]
        ext_out = dict(self.extraction_overrides)
        ext_out.setdefault("profile", self.profile)
        lines.extend(["", "[extraction]"])
        for key, val in ext_out.items():
            if val is not None:
                lines.append(f"{key} = {_toml_scalar(val)}")
        return atomic_write_text(target, "\n".join(lines) + "\n")
