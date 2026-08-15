"""Append-only state store (phase-1 stand-in for PostgreSQL, docs/02)."""

from .jsonl import JsonlLog, Store

__all__ = ["JsonlLog", "Store"]
