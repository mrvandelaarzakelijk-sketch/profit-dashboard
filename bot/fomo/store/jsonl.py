"""Append-only JSONL logs.

This is the phase-1 stand-in for PostgreSQL (docs/02). It is deliberately the simplest
thing that preserves the property the database design is built on: **facts are
append-only and carry the time we observed them**. A daily job needs state that survives
across process runs, and until the real ingestion layer exists a file is enough.

Swapping this for the Postgres repositories later changes only this module — the report
builder takes plain dicts.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("FOMO_DATA_DIR", "data"))


def _serialise(value):
    """Convert a domain object to JSON-safe primitives.

    The type checks are ordered deliberately. ``StrEnum`` members are *also* ``str``
    instances, and reflecting over one with ``vars()`` reaches ``__objclass__``, which
    points back at the enum class — an infinite recursion. Strings and enums are
    therefore resolved before any structural reflection happens.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):  # includes StrEnum members
        return str(value)
    if isinstance(value, Enum):
        return _serialise(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):  # pydantic
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _serialise(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): _serialise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_serialise(v) for v in value]
    return str(value)


class JsonlLog:
    """One append-only file. Concurrent appends from a single process are safe."""

    def __init__(self, path: Path, timestamp_field: str = "observed_at") -> None:
        self.path = Path(path)
        self.timestamp_field = timestamp_field

    def append(self, record) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = _serialise(record)
        if isinstance(payload, dict) and self.timestamp_field not in payload:
            payload[self.timestamp_field] = datetime.now(UTC).isoformat()
        # Open in append mode per write: an interrupted run leaves complete lines behind,
        # and a partially written last line is skipped on read rather than crashing.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def __iter__(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # truncated final line from an interrupted write

    def read(self, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
        """Records whose timestamp falls in [since, until). Unparseable rows are skipped."""
        out = []
        for record in self:
            stamp = record.get(self.timestamp_field)
            if stamp is None:
                continue
            try:
                when = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            if since and when < since:
                continue
            if until and when >= until:
                continue
            out.append(record)
        return out

    def rewrite(self, records: list[dict]) -> None:
        """Atomic full rewrite — used only for compaction, never on the ingest path."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(_serialise(record), separators=(",", ":")) + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class Store:
    """The three logs the daily digest reads from."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        self.signals = JsonlLog(self.data_dir / "signals.jsonl", "observed_at")
        self.trades = JsonlLog(self.data_dir / "paper_trades.jsonl", "closed_at")
        self.equity = JsonlLog(self.data_dir / "equity.jsonl", "at")

    def record_equity(self, value_usd: float, at: datetime | None = None) -> None:
        self.equity.append({"at": (at or datetime.now(UTC)).isoformat(), "equity_usd": value_usd})

    def latest_equity(self, default: float) -> float:
        records = list(self.equity)
        return float(records[-1]["equity_usd"]) if records else default
