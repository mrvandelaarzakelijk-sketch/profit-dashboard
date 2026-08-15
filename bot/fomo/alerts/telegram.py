"""Telegram transport.

Standard library only — no httpx, no SDK. The daily job runs in CI where every extra
dependency is another thing that can break a scheduled run you are not watching.

Two rules this module enforces rather than trusts:

1. **The bot token never reaches a log.** Every error message and every repr goes through
   ``redact()``. Telegram puts the token in the URL path, so an unredacted exception
   traceback leaks it straight into CI output, which is world-readable on public repos.
2. **Failures are loud but not fatal.** A daily digest that cannot be delivered must not
   crash the job silently; it raises a typed error the caller can report on.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

API_ROOT = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096
"""Telegram's hard limit. Longer messages are rejected, not truncated, so we split."""

# Deliberately no \b anchors. The token's most dangerous appearance is inside the API
# URL — ".../bot1234567890:AAF.../sendMessage" — where "t" meets "1" and a word-boundary
# anchor does not match, leaving the token intact in the very string most likely to end
# up in a CI log.
_TOKEN_PATTERN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


class TelegramError(RuntimeError):
    """Delivery failed. The message is always redacted."""


def redact(text: str) -> str:
    """Replace anything shaped like a bot token with a placeholder."""
    return _TOKEN_PATTERN.sub("<redacted-bot-token>", text)


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on line boundaries so HTML tags are never cut in half.

    A naive character split can land inside ``<b>`` and Telegram then rejects the whole
    message with a parse error — which is exactly the kind of failure you discover three
    days later when you wonder why the digest stopped arriving.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        # A single line longer than the limit has to be hard-split; rare, but possible.
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, length = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if length + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


Transport = Callable[[str, bytes, float], tuple[int, bytes]]
"""(url, body, timeout) -> (status_code, response_body). Injectable so the tests never
touch the network."""


def _urllib_transport(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise TelegramError(redact(f"network error talking to Telegram: {exc.reason}")) from None


@dataclass
class TelegramClient:
    bot_token: str
    chat_id: str
    timeout: float = 15.0
    max_retries: int = 4
    transport: Transport = field(default=_urllib_transport, repr=False)
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def __repr__(self) -> str:  # never let the token into a traceback
        return f"TelegramClient(chat_id={self.chat_id!r}, bot_token='<redacted>')"

    def _call(self, method: str, payload: dict) -> dict:
        url = f"{API_ROOT}/bot{self.bot_token}/{method}"
        body = json.dumps(payload).encode()

        for attempt in range(self.max_retries):
            status, raw = self.transport(url, body, self.timeout)
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                data = {}

            if status == 200 and data.get("ok"):
                return data.get("result", {})

            # 429: Telegram tells us exactly how long to wait. Respect it.
            if status == 429:
                wait = float(data.get("parameters", {}).get("retry_after", 2**attempt))
                self.sleep(min(wait, 60.0))
                continue
            if status >= 500:
                self.sleep(2**attempt)
                continue

            # 4xx other than 429 is a request problem; retrying will not fix it.
            description = data.get("description", f"HTTP {status}")
            raise TelegramError(redact(f"Telegram {method} failed: {description}"))

        raise TelegramError(f"Telegram {method} failed after {self.max_retries} attempts")

    def send_message(self, text: str, *, parse_mode: str | None = "HTML") -> list[dict]:
        """Send one logical message, split across API calls if needed."""
        results = []
        for chunk in split_message(text):
            payload: dict = {
                "chat_id": self.chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            results.append(self._call("sendMessage", payload))
        return results

    def get_me(self) -> dict:
        """Verify the token works. Returns the bot's own account info."""
        return self._call("getMe", {})

    def discover_chats(self) -> list[dict]:
        """List chats the bot has recently seen, via getUpdates.

        How to use it: add the bot to your group, post any message there, then run this.
        The group's ``chat_id`` is a negative number (supergroups start with ``-100``).

        Note that ``getUpdates`` only works while no webhook is set, and it only returns
        recent updates — if the list is empty, post in the group again and retry.
        """
        updates = self._call("getUpdates", {"limit": 100})
        seen: dict[str, dict] = {}
        for update in updates if isinstance(updates, list) else []:
            for key in ("message", "channel_post", "my_chat_member", "edited_message"):
                chat = (update.get(key) or {}).get("chat")
                if chat:
                    seen[str(chat.get("id"))] = {
                        "chat_id": chat.get("id"),
                        "type": chat.get("type"),
                        "title": chat.get("title") or chat.get("username") or "",
                    }
        return list(seen.values())
