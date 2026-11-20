"""Orthogonal Gemini harness supporting committed native-vision `scan_checks/` and CLI subprocess."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lanterns.core.atomic import atomic_write_json
from lanterns.core.hashes import sha256_json
from lanterns.providers.base import (
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResult,
    ProviderSuccess,
    validate_wire_if_requested,
)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def scan_override_path(book_dir: str | Path, page_number: int) -> Path:
    return Path(book_dir) / "scan_overrides" / f"page_{page_number:04d}.json"


def scan_check_path(book_dir: str | Path, page_number: int) -> Path:
    override = scan_override_path(book_dir, page_number)
    legacy = Path(book_dir) / "scan_checks" / f"page_{page_number:04d}.json"
    return legacy if (not override.is_file() and legacy.is_file()) else override


def _coerce_scan_check_to_wire(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accept either direct `PageExtractWire`, wrapped `{'wire': {...}}`, or `PageIR`-shaped scan check."""
    meta = {
        "reviewer": str(payload.get("reviewer") or "native-vision-agent"),
        "checked_at": str(payload.get("checked_at") or ""),
        "notes": str(payload.get("notes") or ""),
    }
    if isinstance(payload.get("wire"), dict):
        return dict(payload["wire"]), meta
    blocks = payload.get("blocks")
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict) and "tagged_text" in blocks[0]:
        wire_blocks: list[dict[str, Any]] = []
        for blk in blocks:
            wblk: dict[str, Any] = {
                "type": blk.get("type", "body"),
                "bbox": list(blk.get("bbox") or [0, 0, 1000, 1000]),
                "text": str(blk.get("tagged_text") or blk.get("plain_text") or ""),
                "alignment": blk.get("alignment", "left"),
                "epistolary_transition": blk.get("epistolary_transition", "none"),
                "line_breaks_preserved": bool(blk.get("line_breaks_preserved", False)),
                "heading_level": blk.get("heading_level"),
                "note_label": blk.get("note_label"),
                "continues_from_prev": bool(blk.get("continues_from_prev", False)),
                "continues_to_next": bool(blk.get("continues_to_next", False)),
                "table_role": blk.get("table_role"),
                "cells": blk.get("cells"),
            }
            wire_blocks.append(wblk)
        wire = {
            "printed_folio": payload.get("printed_folio"),
            "page_orientation": payload.get("page_orientation", "upright"),
            "warning_codes": list(payload.get("warning_codes") or []),
            "blocks": wire_blocks,
        }
        return wire, meta
    return dict(payload), meta


def load_native_scan_check(
    book_dir: str | Path,
    page_number: int,
    *,
    validate_schema: bool = True,
) -> ProviderSuccess | None:
    """Load a committed wire scan override (`books/<book_id>/scan_overrides/page_NNNN.json`) if present."""
    path = scan_check_path(book_dir, page_number)
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        isinstance(raw, dict)
        and "checks" in raw
        and not isinstance(raw.get("wire"), dict)
        and not (
            isinstance(raw.get("blocks"), list)
            and raw["blocks"]
            and isinstance(raw["blocks"][0], dict)
            and "tagged_text" in raw["blocks"][0]
        )
    ):
        return None
    wire_data, meta = _coerce_scan_check_to_wire(raw)
    prov = {
        "provider": "gemini_cli",
        "model": "scan-check",
        "harness": "native_scan_check",
        "reviewer": meta["reviewer"],
        "notes": meta["notes"],
        "scan_check_path": str(path),
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "thoughts_tokens": 0,
        "cost_estimate_usd": 0.0,
        "timestamp": meta["checked_at"] or datetime.now(timezone.utc).isoformat(),
        "response_hash": sha256_json(raw),
    }
    if validate_schema:
        dummy_req = ProviderRequest(prompt="", schema_name="page_extract_wire")
        err = validate_wire_if_requested(dummy_req, wire_data, raw_response=raw, provenance=prov)
        if err is not None:
            raise ValueError(f"Invalid scan check at {path}: {err.message}")
    return ProviderSuccess(data=wire_data, raw_response=raw, provenance=prov)


def save_native_scan_check(
    book_dir: str | Path,
    page_number: int,
    wire_data: dict[str, Any],
    *,
    reviewer: str = "native-vision-agent",
    notes: str = "",
) -> Path:
    """Validate and atomically persist a native-vision scan override under `books/<book_id>/scan_overrides/`."""
    target = scan_override_path(book_dir, page_number)
    dummy_req = ProviderRequest(prompt="", schema_name="page_extract_wire")
    err = validate_wire_if_requested(dummy_req, wire_data, raw_response=wire_data, provenance={})
    if err is not None:
        raise ValueError(f"Cannot save invalid wire data to {target}: {err.message}")
    envelope = {
        "version": "scan-check-v1",
        "page_number": page_number,
        "reviewer": reviewer,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "notes": notes,
        "wire": wire_data,
    }
    return atomic_write_json(target, envelope)


class GeminiCLIHarnessProvider:
    """Orthogonal Gemini harness: reads `scan_checks/page_NNNN.json` or invokes `gemini-api` CLI subprocess."""

    name = "gemini_cli"
    pricing_family = "gemini_cli"
    harness = "cli"

    def __init__(
        self,
        model: str = "gemini-3.8-flash",
        *,
        book_dir: str | Path | None = None,
        cli_binary: str | None = None,
        timeout_s: float = 180.0,
    ) -> None:
        self.model = model
        self.book_dir = Path(book_dir) if book_dir is not None else None
        self.cli_binary = cli_binary or shutil.which("gemini-api") or shutil.which("gemini")
        self.timeout_s = timeout_s

    def generate(self, request: ProviderRequest) -> ProviderResult:
        page_num = request.settings.get("page_number")
        book_dir = request.settings.get("book_dir") or self.book_dir
        if book_dir is not None and page_num is not None:
            try:
                checked = load_native_scan_check(book_dir, int(page_num))
            except ValueError as exc:
                return ProviderError(ProviderErrorKind.SCHEMA_INVALID, str(exc))
            if checked is not None:
                return checked

        if not self.cli_binary:
            return ProviderError(
                ProviderErrorKind.PROVIDER_UNAVAILABLE,
                "Neither committed scan_checks/page_NNNN.json nor `gemini-api` CLI binary is available.",
            )
        return self._invoke_cli(request)

    def _invoke_cli(self, request: ProviderRequest) -> ProviderResult:
        assert self.cli_binary is not None
        started = time.time()
        full_prompt = request.prompt
        if request.dynamic_suffix:
            full_prompt = f"{full_prompt}\n\n{request.dynamic_suffix}"
        cmd = [self.cli_binary, "generate", "--model", self.model, "--json", "--prompt", full_prompt]
        for img_path in request.image_paths:
            cmd.extend(["--file", str(img_path)])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ProviderError(ProviderErrorKind.TIMEOUT, f"gemini CLI timed out after {self.timeout_s}s")
        except OSError as exc:
            return ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, f"gemini CLI execution failed: {exc}")

        if proc.returncode != 0:
            err_msg = (proc.stderr or proc.stdout or "").strip()[:300]
            return ProviderError(ProviderErrorKind.PROVIDER_UNAVAILABLE, f"gemini CLI exit {proc.returncode}: {err_msg}")

        cleaned = _FENCE_RE.sub("", proc.stdout).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return ProviderError(
                ProviderErrorKind.SCHEMA_INVALID,
                "gemini CLI output was not valid JSON",
                raw_response={"stdout": proc.stdout[:500]},
            )

        raw_resp = {"cli_binary": self.cli_binary, "parsed": data}
        prov = {
            "provider": self.name,
            "model": "scan-check" if self.model == "scan-check" else self.model,
            "harness": "gemini_cli",
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "thoughts_tokens": 0,
            "cost_estimate_usd": 0.0,
            "latency_ms": int((time.time() - started) * 1000),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "response_hash": sha256_json(raw_resp),
        }
        err = validate_wire_if_requested(request, data, raw_response=raw_resp, provenance=prov)
        if err is not None:
            return err
        return ProviderSuccess(data=data, raw_response=raw_resp, provenance=prov)
