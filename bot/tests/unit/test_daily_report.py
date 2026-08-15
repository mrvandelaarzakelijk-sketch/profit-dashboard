"""Daily digest and the append-only store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fomo.domain import ExitReason
from fomo.reports.daily import build_digest, render_telegram, render_text
from fomo.store.jsonl import JsonlLog, Store

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def signal_record(symbol: str, at: datetime, **overrides) -> dict:
    return {
        "observed_at": at.isoformat(),
        "symbol": symbol,
        "token_address": f"addr_{symbol}",
        "state": "POTENTIAL_ENTRY",
        "master_score": 78.0,
        "n_effective": 2.4,
        "veto_reasons": [],
        "flags": [],
        **overrides,
    }


def trade_record(symbol: str, closed_at: datetime, pnl: float, **overrides) -> dict:
    return {
        "opened_at": (closed_at - timedelta(minutes=30)).isoformat(),
        "closed_at": closed_at.isoformat(),
        "token_address": f"addr_{symbol}",
        "symbol": symbol,
        "entry_price_effective": 0.0004,
        "exit_price_effective": 0.0005,
        "size_usd": 500.0,
        "gross_pnl_usd": pnl + 5.0,
        "fees_usd": 3.0,
        "slippage_usd": 2.0,
        "net_pnl_usd": pnl,
        "return_pct": 100.0 * pnl / 500.0,
        "hold_seconds": 1800.0,
        "exit_reason": ExitReason.TAKE_PROFIT.value,
        "max_favorable_excursion": 40.0,
        "max_adverse_excursion": -8.0,
        "master_at_entry": 78.0,
        "neff_at_entry": 2.4,
        **overrides,
    }


class TestJsonlLog:
    def test_append_and_read_round_trip(self, tmp_path):
        log = JsonlLog(tmp_path / "x.jsonl")
        log.append({"observed_at": NOW.isoformat(), "value": 1})
        log.append({"observed_at": NOW.isoformat(), "value": 2})
        assert [r["value"] for r in log] == [1, 2]

    def test_serialises_enums_without_infinite_recursion(self, tmp_path):
        """StrEnum members are also str; reflecting over one used to recurse forever."""
        log = JsonlLog(tmp_path / "x.jsonl")
        log.append({"observed_at": NOW.isoformat(), "reason": ExitReason.STOP_LOSS})
        assert list(log)[0]["reason"] == "STOP_LOSS"

    def test_adds_a_timestamp_when_missing(self, tmp_path):
        log = JsonlLog(tmp_path / "x.jsonl")
        log.append({"value": 1})
        assert "observed_at" in list(log)[0]

    def test_skips_a_truncated_final_line(self, tmp_path):
        path = tmp_path / "x.jsonl"
        path.write_text('{"observed_at":"2026-08-15T12:00:00+00:00","v":1}\n{"observed_at":"trun')
        assert len(list(JsonlLog(path))) == 1

    def test_window_filtering_is_half_open(self, tmp_path):
        log = JsonlLog(tmp_path / "x.jsonl")
        for hours in (0, 5, 30):
            log.append({"observed_at": (NOW - timedelta(hours=hours)).isoformat(), "h": hours})
        window = log.read(since=NOW - timedelta(days=1), until=NOW + timedelta(seconds=1))
        assert {r["h"] for r in window} == {0, 5}

    def test_missing_file_reads_as_empty(self, tmp_path):
        assert list(JsonlLog(tmp_path / "nope.jsonl")) == []

    def test_rewrite_is_atomic_and_replaces_content(self, tmp_path):
        log = JsonlLog(tmp_path / "x.jsonl")
        log.append({"observed_at": NOW.isoformat(), "v": 1})
        log.rewrite([{"observed_at": NOW.isoformat(), "v": 99}])
        assert [r["v"] for r in log] == [99]
        assert not list(tmp_path.glob("*.tmp"))


class TestDigest:
    def test_empty_store_reports_no_ingestion(self, tmp_path):
        digest = build_digest(Store(tmp_path), now=NOW)
        assert digest.ingestion_live is False
        assert digest.n_signals == 0
        message = render_text(digest)
        assert "Geen signalen" in message
        assert "fase 1–3" in message

    def test_counts_states_vetoes_and_flags(self, tmp_path):
        store = Store(tmp_path)
        store.signals.append(signal_record("AAA", NOW - timedelta(hours=1)))
        store.signals.append(
            signal_record(
                "BBB",
                NOW - timedelta(hours=2),
                state="AVOID",
                veto_reasons=["HONEYPOT", "LP_NOT_SECURED"],
            )
        )
        store.signals.append(
            signal_record("CCC", NOW - timedelta(hours=3), state="AVOID", flags=["CLUSTER_ILLUSION"])
        )
        digest = build_digest(store, now=NOW)
        assert digest.n_signals == 3
        assert digest.state_counts == {"POTENTIAL_ENTRY": 1, "AVOID": 2}
        assert digest.veto_counts["HONEYPOT"] == 1
        assert digest.flag_counts["CLUSTER_ILLUSION"] == 1

    def test_leaderboard_keeps_the_best_entry_per_token(self, tmp_path):
        """A token re-scored every tick must not fill the whole leaderboard."""
        store = Store(tmp_path)
        for score in (60.0, 91.0, 72.0):
            store.signals.append(
                signal_record("AAA", NOW - timedelta(minutes=score), master_score=score)
            )
        store.signals.append(signal_record("BBB", NOW - timedelta(hours=1), master_score=80.0))
        digest = build_digest(store, now=NOW)
        assert [r["symbol"] for r in digest.top_signals] == ["AAA", "BBB"]
        assert digest.top_signals[0]["master_score"] == 91.0

    def test_avoid_signals_stay_out_of_the_leaderboard(self, tmp_path):
        store = Store(tmp_path)
        store.signals.append(
            signal_record("AAA", NOW - timedelta(hours=1), state="AVOID", master_score=99.0)
        )
        assert build_digest(store, now=NOW).top_signals == []

    def test_performance_is_computed_from_trades(self, tmp_path):
        store = Store(tmp_path)
        store.trades.append(trade_record("AAA", NOW - timedelta(hours=1), 120.0))
        store.trades.append(trade_record("BBB", NOW - timedelta(hours=2), -60.0))
        store.signals.append(signal_record("AAA", NOW - timedelta(hours=1)))
        store.record_equity(10_060.0, at=NOW)

        digest = build_digest(store, now=NOW)
        assert digest.performance is not None
        assert digest.performance.n_trades == 2
        assert digest.performance.win_rate == pytest.approx(0.5)
        assert digest.equity_start == pytest.approx(10_000.0)
        assert digest.equity_change_pct == pytest.approx(0.6)

    def test_out_of_window_records_are_excluded(self, tmp_path):
        store = Store(tmp_path)
        store.signals.append(signal_record("OLD", NOW - timedelta(days=3)))
        store.trades.append(trade_record("OLD", NOW - timedelta(days=3), 500.0))
        digest = build_digest(store, now=NOW)
        assert digest.n_signals == 0
        assert digest.trades == []

    def test_malformed_trade_row_is_skipped_and_noted(self, tmp_path):
        """The daily job is the one nobody is watching when it fails."""
        store = Store(tmp_path)
        store.trades.append(trade_record("GOOD", NOW - timedelta(hours=1), 50.0))
        store.trades.append({"closed_at": (NOW - timedelta(hours=1)).isoformat(), "junk": True})
        digest = build_digest(store, now=NOW)
        assert len(digest.trades) == 1
        assert any("malformed" in note for note in digest.notes)


class TestRendering:
    def _full_digest(self, tmp_path):
        store = Store(tmp_path)
        store.signals.append(signal_record("AAA", NOW - timedelta(hours=1), master_score=88.0))
        store.signals.append(
            signal_record("BBB", NOW - timedelta(hours=2), state="AVOID", veto_reasons=["HONEYPOT"])
        )
        store.trades.append(trade_record("AAA", NOW - timedelta(hours=1), 120.0))
        store.record_equity(10_120.0, at=NOW)
        return build_digest(store, now=NOW)

    def test_message_contains_every_section(self, tmp_path):
        message = render_telegram(self._full_digest(tmp_path))
        for fragment in ("daily digest", "Signalen", "Beste kandidaten", "veto", "Paper trades", "Portefeuille"):
            assert fragment.lower() in message.lower()

    def test_message_states_paper_mode(self, tmp_path):
        assert "Paper trading" in render_telegram(self._full_digest(tmp_path))

    def test_html_tags_are_balanced(self, tmp_path):
        message = render_telegram(self._full_digest(tmp_path))
        for tag in ("b", "i", "code"):
            assert message.count(f"<{tag}>") == message.count(f"</{tag}>")

    def test_text_rendering_strips_tags(self, tmp_path):
        assert "<b>" not in render_text(self._full_digest(tmp_path))

    def test_message_fits_telegram_limits_after_splitting(self, tmp_path):
        from fomo.alerts.telegram import MAX_MESSAGE_CHARS, split_message

        store = Store(tmp_path)
        for i in range(400):
            store.signals.append(signal_record(f"T{i:03d}", NOW - timedelta(minutes=i)))
        message = render_telegram(build_digest(store, now=NOW))
        assert all(len(chunk) <= MAX_MESSAGE_CHARS for chunk in split_message(message))


class TestHtmlSafety:
    """Token symbols come from on-chain metadata, which anyone can set.

    A memecoin can legitimately be deployed with the symbol `<a href="https://evil">`.
    Interpolating that into our own alert would let the token's author put clickable
    links into a message the reader trusts — and an unescaped `&` makes Telegram reject
    the whole message with "can't parse entities", so the digest silently stops.
    """

    HOSTILE = '<a href="https://evil.example">FREE $$$</a> & <b>pump</b>'

    def test_hostile_symbol_cannot_inject_markup(self, tmp_path):
        store = Store(tmp_path)
        store.signals.append(signal_record(self.HOSTILE, NOW - timedelta(hours=1)))
        message = render_telegram(build_digest(store, now=NOW))
        assert "<a href=" not in message
        assert "&lt;a href=" in message
        assert "&amp;" in message

    def test_ampersand_in_our_own_copy_is_escaped(self, tmp_path):
        """"P&L" was a real bug: a bare & is not a valid entity."""
        store = Store(tmp_path)
        store.signals.append(signal_record("AAA", NOW - timedelta(hours=1)))
        store.trades.append(trade_record("AAA", NOW - timedelta(hours=1), 50.0))
        message = render_telegram(build_digest(store, now=NOW))
        import re

        # Every & must begin a real entity.
        for match in re.finditer(r"&(?!amp;|lt;|gt;)", message):
            raise AssertionError(f"unescaped & at offset {match.start()}: {message[match.start():match.start()+30]!r}")

    def test_only_our_own_tags_survive(self, tmp_path):
        store = Store(tmp_path)
        store.signals.append(signal_record(self.HOSTILE, NOW - timedelta(hours=1)))
        message = render_telegram(build_digest(store, now=NOW))
        import re

        allowed = {"b", "/b", "i", "/i", "code", "/code", "pre", "/pre"}
        assert {t for t in re.findall(r"<([^>]+)>", message)} <= allowed

    def test_text_rendering_reveals_the_hostile_symbol(self, tmp_path):
        """Stripping tags before unescaping — the other order would hide it."""
        store = Store(tmp_path)
        store.signals.append(signal_record(self.HOSTILE, NOW - timedelta(hours=1)))
        text = render_text(build_digest(store, now=NOW))
        assert "evil.example" in text
