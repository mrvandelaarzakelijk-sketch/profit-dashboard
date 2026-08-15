"""Telegram transport — retries, splitting, and above all token redaction.

No network: the transport is injected. A test suite that needs a real bot token is a
test suite nobody runs.
"""

from __future__ import annotations

import json

import pytest

from fomo.alerts.telegram import (
    MAX_MESSAGE_CHARS,
    TelegramClient,
    TelegramError,
    redact,
    split_message,
)

FAKE_TOKEN = "1234567890:AAFAKEfakeFAKEfakeFAKEfakeFAKEfake123"


class FakeTransport:
    """Scriptable (status, body) responses; records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, timeout):
        self.calls.append({"url": url, "payload": json.loads(body), "timeout": timeout})
        status, data = self.responses.pop(0) if self.responses else (200, {"ok": True, "result": {}})
        return status, json.dumps(data).encode()


def client(responses, **kwargs) -> TelegramClient:
    return TelegramClient(
        bot_token=FAKE_TOKEN,
        chat_id="-1001234567890",
        transport=FakeTransport(responses),
        sleep=lambda _: None,
        **kwargs,
    )


class TestRedaction:
    """The token sits in the URL path, so an unredacted traceback leaks it into CI logs."""

    def test_redacts_a_token_anywhere_in_a_string(self):
        message = f"failed calling https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
        assert FAKE_TOKEN not in redact(message)
        assert "<redacted-bot-token>" in redact(message)

    def test_leaves_ordinary_text_alone(self):
        assert redact("liquidity dropped 40% in 5m") == "liquidity dropped 40% in 5m"

    def test_repr_never_contains_the_token(self):
        c = client([])
        assert FAKE_TOKEN not in repr(c)
        assert "<redacted>" in repr(c)

    def test_error_message_from_a_failed_call_is_redacted(self):
        c = client([(400, {"ok": False, "description": f"bad token {FAKE_TOKEN}"})])
        with pytest.raises(TelegramError) as exc:
            c.send_message("hi")
        assert FAKE_TOKEN not in str(exc.value)


class TestSplitting:
    def test_short_message_is_not_split(self):
        assert split_message("hello") == ["hello"]

    def test_splits_on_line_boundaries(self):
        text = "\n".join(f"line {i}" for i in range(2000))
        chunks = split_message(text)
        assert len(chunks) > 1
        assert all(len(c) <= MAX_MESSAGE_CHARS for c in chunks)
        # Nothing lost: every line survives somewhere.
        assert sum(c.count("line ") for c in chunks) == 2000

    def test_never_splits_inside_a_tag(self):
        text = "\n".join(f"<b>bold line {i}</b>" for i in range(1000))
        for chunk in split_message(text):
            assert chunk.count("<b>") == chunk.count("</b>")

    def test_hard_splits_a_single_overlong_line(self):
        chunks = split_message("x" * (MAX_MESSAGE_CHARS * 2 + 50))
        assert all(len(c) <= MAX_MESSAGE_CHARS for c in chunks)
        assert sum(len(c) for c in chunks) == MAX_MESSAGE_CHARS * 2 + 50

    def test_long_message_becomes_multiple_api_calls(self):
        c = client([(200, {"ok": True, "result": {"message_id": i}}) for i in range(5)])
        results = c.send_message("\n".join(f"row {i}" for i in range(3000)))
        assert len(results) == len(c.transport.calls) > 1


class TestRetries:
    def test_respects_retry_after_on_429(self):
        waits = []
        c = TelegramClient(
            bot_token=FAKE_TOKEN,
            chat_id="-100",
            transport=FakeTransport(
                [
                    (429, {"ok": False, "parameters": {"retry_after": 7}}),
                    (200, {"ok": True, "result": {"message_id": 1}}),
                ]
            ),
            sleep=waits.append,
        )
        c.send_message("hi")
        assert waits == [7.0]

    def test_retries_server_errors(self):
        c = client([(500, {}), (503, {}), (200, {"ok": True, "result": {}})])
        c.send_message("hi")
        assert len(c.transport.calls) == 3

    def test_gives_up_after_max_retries(self):
        c = client([(500, {})] * 6, max_retries=3)
        with pytest.raises(TelegramError, match="after 3 attempts"):
            c.send_message("hi")

    def test_client_errors_are_not_retried(self):
        """A 400 means the request is wrong; retrying just burns time."""
        c = client([(400, {"ok": False, "description": "chat not found"})])
        with pytest.raises(TelegramError, match="chat not found"):
            c.send_message("hi")
        assert len(c.transport.calls) == 1


class TestPayload:
    def test_sends_expected_fields(self):
        c = client([(200, {"ok": True, "result": {}})])
        c.send_message("<b>hi</b>")
        payload = c.transport.calls[0]["payload"]
        assert payload["chat_id"] == "-1001234567890"
        assert payload["text"] == "<b>hi</b>"
        assert payload["parse_mode"] == "HTML"
        assert payload["disable_web_page_preview"] is True

    def test_parse_mode_can_be_disabled(self):
        c = client([(200, {"ok": True, "result": {}})])
        c.send_message("plain", parse_mode=None)
        assert "parse_mode" not in c.transport.calls[0]["payload"]

    def test_get_me(self):
        c = client([(200, {"ok": True, "result": {"username": "fomo_bot", "id": 42}})])
        assert c.get_me()["username"] == "fomo_bot"


class TestDiscoverChats:
    def test_extracts_unique_chats_from_updates(self):
        updates = [
            {"message": {"chat": {"id": -1001, "type": "supergroup", "title": "Alpha"}}},
            {"message": {"chat": {"id": -1001, "type": "supergroup", "title": "Alpha"}}},
            {"my_chat_member": {"chat": {"id": 999, "type": "private", "username": "me"}}},
        ]
        c = client([(200, {"ok": True, "result": updates})])
        chats = c.discover_chats()
        assert len(chats) == 2
        assert {-1001, 999} == {chat["chat_id"] for chat in chats}

    def test_empty_updates(self):
        c = client([(200, {"ok": True, "result": []})])
        assert c.discover_chats() == []
