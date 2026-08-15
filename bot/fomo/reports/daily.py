"""Daily digest for Telegram.

Design rule, same as the per-signal alerts: **say what happened and why, and say plainly
when nothing happened.** A daily push that invents activity to look useful trains you to
ignore it, and then it is worse than no push at all.

Specifically, when the ingestion layer is not live yet this report says so in one line
rather than rendering an empty dashboard that looks like a quiet market.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..store.jsonl import Store
from ..trading.analytics import Performance
from ..trading.paper import ClosedTrade

ENTRY_STATES = ("POTENTIAL_ENTRY", "STRONG_ENTRY")


@dataclass
class DailyDigest:
    period_start: datetime
    period_end: datetime
    n_signals: int
    state_counts: dict[str, int]
    top_signals: list[dict]
    veto_counts: dict[str, int]
    flag_counts: dict[str, int]
    trades: list[ClosedTrade]
    performance: Performance | None
    equity_start: float
    equity_end: float
    ingestion_live: bool
    notes: list[str] = field(default_factory=list)

    @property
    def equity_change_pct(self) -> float:
        if self.equity_start <= 0:
            return 0.0
        return 100.0 * (self.equity_end - self.equity_start) / self.equity_start


def _trade_from_record(record: dict) -> ClosedTrade | None:
    """Rebuild a ClosedTrade from its stored form, skipping anything malformed.

    A single corrupt row must not take down the daily report — it is the one job nobody
    is watching when it fails.
    """
    try:
        return ClosedTrade(
            token_address=record["token_address"],
            symbol=record["symbol"],
            opened_at=datetime.fromisoformat(record["opened_at"]),
            closed_at=datetime.fromisoformat(record["closed_at"]),
            entry_price_effective=float(record["entry_price_effective"]),
            exit_price_effective=float(record["exit_price_effective"]),
            size_usd=float(record["size_usd"]),
            gross_pnl_usd=float(record["gross_pnl_usd"]),
            fees_usd=float(record["fees_usd"]),
            slippage_usd=float(record["slippage_usd"]),
            net_pnl_usd=float(record["net_pnl_usd"]),
            return_pct=float(record["return_pct"]),
            hold_seconds=float(record["hold_seconds"]),
            exit_reason=record["exit_reason"],
            max_favorable_excursion=float(record.get("max_favorable_excursion", 0.0)),
            max_adverse_excursion=float(record.get("max_adverse_excursion", 0.0)),
            master_at_entry=float(record.get("master_at_entry", 0.0)),
            neff_at_entry=float(record.get("neff_at_entry", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def build_digest(
    store: Store,
    *,
    now: datetime | None = None,
    window: timedelta = timedelta(days=1),
    starting_equity: float = 10_000.0,
    top_n: int = 5,
) -> DailyDigest:
    from ..trading.analytics import evaluate as evaluate_performance

    end = now or datetime.now(UTC)
    start = end - window

    signal_records = store.signals.read(since=start, until=end)
    trade_records = store.trades.read(since=start, until=end)
    trades = [t for t in (_trade_from_record(r) for r in trade_records) if t is not None]

    state_counts = Counter(r.get("state", "UNKNOWN") for r in signal_records)
    veto_counts: Counter = Counter()
    flag_counts: Counter = Counter()
    for record in signal_records:
        veto_counts.update(record.get("veto_reasons") or [])
        flag_counts.update(record.get("flags") or [])

    # Best signal per token, not best signals overall — a token re-evaluated every tick
    # would otherwise fill the whole leaderboard with itself.
    best_per_token: dict[str, dict] = {}
    for record in signal_records:
        if record.get("state") == "AVOID":
            continue
        key = record.get("token_address") or record.get("symbol", "?")
        current = best_per_token.get(key)
        if current is None or float(record.get("master_score", 0.0)) > float(
            current.get("master_score", 0.0)
        ):
            best_per_token[key] = record
    ranked = sorted(
        best_per_token.values(), key=lambda r: float(r.get("master_score", 0.0)), reverse=True
    )[:top_n]

    equity_end = store.latest_equity(starting_equity)
    equity_start = equity_end - sum(t.net_pnl_usd for t in trades)

    notes: list[str] = []
    skipped = len(trade_records) - len(trades)
    if skipped:
        notes.append(f"{skipped} malformed trade record(s) skipped")

    return DailyDigest(
        period_start=start,
        period_end=end,
        n_signals=len(signal_records),
        state_counts=dict(state_counts),
        top_signals=ranked,
        veto_counts=dict(veto_counts),
        flag_counts=dict(flag_counts),
        trades=trades,
        performance=evaluate_performance(trades, equity_start) if trades else None,
        equity_start=equity_start,
        equity_end=equity_end,
        ingestion_live=bool(signal_records),
        notes=notes,
    )


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def render_telegram(digest: DailyDigest, *, mode: str = "paper") -> str:
    """HTML body for the daily push."""
    day = digest.period_end.strftime("%d %b %Y")
    lines = [
        f"📊 <b>FOMO daily digest</b> — {day}",
        f"<i>{digest.period_start:%d %b %H:%M} → {digest.period_end:%d %b %H:%M} UTC"
        f" · mode: {mode}</i>",
    ]

    if not digest.ingestion_live:
        lines += [
            "",
            "⚪ <b>Geen signalen vandaag.</b>",
            "",
            "De beslissingsengine draait, maar er is nog geen live data-ingest "
            "(roadmap fase 1–3). Zodra wallet- en marktdata binnenkomen, verschijnen "
            "hier de signalen, veto's en paper-trades van de afgelopen 24 uur.",
        ]
        if digest.trades:
            lines.append("")
        else:
            lines += ["", f"<code>{digest.period_end:%Y-%m-%d}</code>"]
            return "\n".join(lines)

    if digest.n_signals:
        entries = sum(digest.state_counts.get(s, 0) for s in ENTRY_STATES)
        lines += [
            "",
            f"🔎 <b>Signalen</b> — {digest.n_signals} beoordeeld, {entries} entry-waardig",
        ]
        for state, count in sorted(digest.state_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"• {state}: {count}")

    if digest.top_signals:
        lines += ["", "🏆 <b>Beste kandidaten</b>"]
        for record in digest.top_signals:
            symbol = record.get("symbol", "?")
            master = float(record.get("master_score", 0.0))
            state = record.get("state", "?")
            neff = float(record.get("n_effective", 0.0))
            lines.append(f"• <b>${symbol}</b> {master:.0f}/100 · {state} · N_eff {neff:.2f}")

    if digest.veto_counts:
        top_vetoes = sorted(digest.veto_counts.items(), key=lambda kv: -kv[1])[:5]
        lines += ["", "⛔ <b>Meest voorkomende veto's</b>"]
        lines += [f"• {rule}: {count}" for rule, count in top_vetoes]

    if digest.flag_counts:
        top_flags = sorted(digest.flag_counts.items(), key=lambda kv: -kv[1])[:5]
        lines += ["", "🚩 <b>False-positive flags</b>"]
        lines += [f"• {flag}: {count}" for flag, count in top_flags]

    if digest.performance is not None:
        p = digest.performance
        lines += [
            "",
            f"💼 <b>Paper trades</b> — {p.n_trades} gesloten",
            f"• win rate {p.win_rate:.0%} ({p.n_wins}W / {p.n_losses}L)",
            f"• expectancy {_pct(p.expectancy_pct)} per trade",
            f"• profit factor {p.profit_factor:.2f}",
            f"• netto P&L ${p.total_pnl_usd:+,.2f} "
            f"(fees ${p.total_fees_usd:.2f}, slippage ${p.total_slippage_usd:.2f})",
            f"• gem. holdtijd {p.avg_hold_seconds / 60:.0f} min",
        ]
        if p.exit_reason_counts:
            reasons = ", ".join(f"{k} {v}" for k, v in sorted(p.exit_reason_counts.items()))
            lines.append(f"• exits: {reasons}")
        lines.append(
            f"• MFE {_pct(p.avg_mfe_pct)} / MAE {_pct(p.avg_mae_pct)} "
            f"<i>(stops te strak? targets te ver?)</i>"
        )
    else:
        lines += ["", "💼 <b>Paper trades</b> — geen gesloten trades vandaag"]

    lines += [
        "",
        f"💰 <b>Portefeuille</b> ${digest.equity_end:,.2f} "
        f"({_pct(digest.equity_change_pct)} vandaag)",
    ]

    for note in digest.notes:
        lines.append(f"<i>⚠️ {note}</i>")

    lines += ["", "<i>Paper trading — er is geen live executie aangesloten.</i>"]
    return "\n".join(lines)


def render_text(digest: DailyDigest, *, mode: str = "paper") -> str:
    """Plain-text version for the terminal (strips the HTML tags)."""
    import re

    return re.sub(r"<[^>]+>", "", render_telegram(digest, mode=mode))
