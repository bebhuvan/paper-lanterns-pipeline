"""Thread-safe, crash-durable CostLedger enforcing `--cost-ceiling-usd` via `cost_ledger.jsonl`."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lanterns.core.atomic import atomic_write_json


class CostCeilingExceeded(RuntimeError):
    """Raised when cumulative or reserved spend reaches `ceiling_usd`."""


def _read_existing_entries(jsonl_path: Path, snapshot_path: Path) -> list[dict[str, Any]]:
    jsonl_entries: list[dict[str, Any]] = []
    if jsonl_path.is_file():
        for raw_line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                jsonl_entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    snap_entries: list[dict[str, Any]] = []
    if snapshot_path.is_file():
        try:
            snap_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snap_entries = list(snap_data.get("entries") or [])
        except (OSError, json.JSONDecodeError):
            snap_entries = []
    return jsonl_entries if len(jsonl_entries) >= len(snap_entries) else snap_entries


class CostLedger:
    """Thread-safe spend tracker with pre-call reservation, append-only JSONL durability, and hard ceiling."""

    def __init__(
        self,
        ledger_path: str | Path,
        *,
        ceiling_usd: float | None = None,
        default_reservation_usd: float = 0.006,
    ) -> None:
        raw_path = Path(ledger_path)
        if raw_path.suffix == ".jsonl":
            self.jsonl_path = raw_path
            self.ledger_path = raw_path.with_suffix(".json")
        else:
            self.ledger_path = raw_path
            self.jsonl_path = raw_path.with_suffix(".jsonl")

        self.ceiling_usd = float(ceiling_usd) if ceiling_usd is not None else None
        self.default_reservation_usd = float(default_reservation_usd)
        self._lock = threading.Lock()
        self.cancelled_event = threading.Event()
        self._reserved_usd = 0.0
        self._entries: list[dict[str, Any]] = _read_existing_entries(self.jsonl_path, self.ledger_path)
        self._spent_usd = round(sum(float(item.get("cost_usd") or 0.0) for item in self._entries), 8)
        if self.ceiling_usd is not None and self._spent_usd >= self.ceiling_usd and self._spent_usd > 0:
            self.cancelled_event.set()

    @property
    def total_spent_usd(self) -> float:
        with self._lock:
            return round(self._spent_usd, 8)

    @property
    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)

    def acquire_slot(self, estimated_usd: float | None = None) -> float:
        """Atomically reserve budget before an API call; raise `CostCeilingExceeded` if over ceiling."""
        reserve = self.default_reservation_usd if estimated_usd is None else max(0.0, float(estimated_usd))
        with self._lock:
            if self.cancelled_event.is_set():
                raise CostCeilingExceeded(f"Cost ceiling (${self.ceiling_usd or 0.0:.4f}) already reached.")
            if self.ceiling_usd is not None:
                if self.ceiling_usd <= 0.0 and reserve > 0.0:
                    self.cancelled_event.set()
                    raise CostCeilingExceeded("Cost ceiling is $0.0000; paid calls are blocked.")
                projected = self._spent_usd + self._reserved_usd + reserve
                if projected > self.ceiling_usd and ((self._spent_usd + self._reserved_usd) > 0 or reserve > self.ceiling_usd):
                    self.cancelled_event.set()
                    raise CostCeilingExceeded(
                        f"Projected spend ${projected:.4f} would exceed ceiling ${self.ceiling_usd:.4f}."
                    )
            self._reserved_usd += reserve
            return reserve

    def release_and_record(
        self,
        reserved_usd: float,
        *,
        stage: str,
        provider: str,
        model: str,
        cost_usd: float,
        harness: str = "sdk",
        page_number: int | None = None,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        thoughts_tokens: int = 0,
        is_batch: bool = False,
        status: str = "ok",
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "page_number": page_number,
            "provider": provider,
            "model": model,
            "harness": harness,
            "is_batch": is_batch,
            "status": status,
            "input_tokens": int(input_tokens),
            "cached_input_tokens": int(cached_input_tokens),
            "output_tokens": int(output_tokens),
            "thoughts_tokens": int(thoughts_tokens),
            "cost_usd": round(float(cost_usd), 8),
        }
        with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - float(reserved_usd))
            self._spent_usd = round(self._spent_usd + float(cost_usd), 8)
            self._entries.append(entry)
            if self.ceiling_usd is not None and self._spent_usd >= self.ceiling_usd:
                self.cancelled_event.set()
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if len(self._entries) <= 50 or len(self._entries) % 15 == 0 or self.cancelled_event.is_set():
                self._write_snapshot_unlocked()
        return entry

    def release_without_spend(self, reserved_usd: float) -> None:
        with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - float(reserved_usd))

    def flush(self) -> None:
        with self._lock:
            if self._entries or self.ledger_path.is_file() or self.jsonl_path.is_file():
                self._write_snapshot_unlocked()

    def _write_snapshot_unlocked(self) -> None:
        snapshot = {
            "version": "cost-ledger-v2",
            "ceiling_usd": self.ceiling_usd,
            "total_spent_usd": self._spent_usd,
            "entry_count": len(self._entries),
            "entries": list(self._entries),
        }
        atomic_write_json(self.ledger_path, snapshot)
