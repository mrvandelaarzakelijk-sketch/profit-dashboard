"""Alert rendering and delivery (docs/01 §1.J)."""

from .format import dedupe_key, render_discord, render_telegram

__all__ = ["render_telegram", "render_discord", "dedupe_key"]
