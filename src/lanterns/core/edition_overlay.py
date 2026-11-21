"""Editorial overlay for one edition: `books/<book_id>/edition.toml`.

ROLE: Load hand-curated, cited presentation data the compiler cannot infer: life periods, the full form of
correspondents' names, the default writer of a single-author volume, translators and publisher.
INPUTS: Optional TOML file beside book.toml. Kept separate because `BookConfig.save()` rewrites book.toml
from its own fields and would silently drop any extra table.
OUTPUTS: A frozen `EditionOverlay`; malformed data raises `EditionOverlayError` (never guessed around).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ISO_RE = re.compile(r"\d{4}(?:-\d{2}(?:-\d{2})?)?")


class EditionOverlayError(ValueError):
    """edition.toml is present but malformed."""


@dataclass(frozen=True)
class LifePeriod:
    label: str
    start: str
    end: str
    source: str


@dataclass(frozen=True)
class EditionOverlay:
    periods: tuple[LifePeriod, ...] = ()
    name_forms: dict[str, str] = field(default_factory=dict)
    default_sender: str = ""
    default_sender_source: str = ""
    translators: tuple[str, ...] = ()
    publisher: str = ""

    def full_name(self, printed: str | None) -> str:
        """The overlay's full form of a printed name, or the printed name unchanged."""
        text = (printed or "").strip()
        return self.name_forms.get(text, text)


def _text(row: dict[str, Any], key: str, where: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EditionOverlayError(f"{where}: `{key}` must be a non-empty string")
    return value.strip()


def _parse_periods(rows: Any) -> tuple[LifePeriod, ...]:
    if not isinstance(rows, list):
        raise EditionOverlayError("`life_periods` must be an array of tables")
    periods: list[LifePeriod] = []
    for idx, row in enumerate(rows):
        where = f"life_periods[{idx}]"
        if not isinstance(row, dict):
            raise EditionOverlayError(f"{where} must be a table")
        period = LifePeriod(*(_text(row, k, where) for k in ("label", "start", "end", "source")))
        if not (_ISO_RE.fullmatch(period.start) and _ISO_RE.fullmatch(period.end)):
            raise EditionOverlayError(f"{where}: start and end must be ISO dates (YYYY, YYYY-MM or YYYY-MM-DD)")
        if period.end <= period.start or (periods and period.start < periods[-1].end):
            raise EditionOverlayError(f"{where}: periods must be non-empty and in order without overlap")
        periods.append(period)
    return tuple(periods)


def _parse_names(rows: Any) -> dict[str, str]:
    if not isinstance(rows, list):
        raise EditionOverlayError("`correspondents` must be an array of tables")
    forms: dict[str, str] = {}
    for idx, row in enumerate(rows):
        where = f"correspondents[{idx}]"
        if not isinstance(row, dict) or not isinstance(row.get("printed", []), list):
            raise EditionOverlayError(f"{where} must be a table with a `printed` list")
        full = _text(row, "name", where)
        for printed in [full, *row.get("printed", [])]:
            key = str(printed).strip()
            if forms.get(key, full) != full:
                raise EditionOverlayError(f"{where}: printed form {key!r} is already mapped to {forms[key]!r}")
            forms[key] = full
    return forms


def parse_edition_overlay(raw: dict[str, Any]) -> EditionOverlay:
    """Validate the parsed TOML of an edition.toml."""
    sender = raw.get("default_sender") or {}
    if not isinstance(sender, dict):
        raise EditionOverlayError("`default_sender` must be a table with `name` and `source`")
    edition = raw.get("edition") or {}
    return EditionOverlay(
        periods=_parse_periods(raw.get("life_periods", [])),
        name_forms=_parse_names(raw.get("correspondents", [])),
        default_sender=_text(sender, "name", "default_sender") if sender else "",
        default_sender_source=_text(sender, "source", "default_sender") if sender else "",
        translators=tuple(str(t).strip() for t in edition.get("translators", []) if str(t).strip()),
        publisher=str(edition.get("publisher", "")).strip(),
    )


def load_edition_overlay(path: Path) -> EditionOverlay:
    """Read `books/<id>/edition.toml`; a missing file means no overlay."""
    if not path.is_file():
        return EditionOverlay()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise EditionOverlayError(f"{path}: {exc}") from exc
    return parse_edition_overlay(raw)
