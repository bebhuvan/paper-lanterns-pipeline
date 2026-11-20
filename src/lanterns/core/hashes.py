"""Deterministic SHA-256 hashing helpers over diplomatic NFC text, bytes, files, and JSON."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from lanterns.core.unicode_norm import nfc_text, normalize_diplomatic_text

__all__ = [
    "nfc_text",
    "normalize_diplomatic_text",
    "sha256_bytes",
    "sha256_file",
    "sha256_json",
    "sha256_text",
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(normalize_diplomatic_text(text).encode("utf-8"))


def sha256_json(data: Any) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(canonical.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
