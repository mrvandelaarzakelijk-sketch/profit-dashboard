"""Performance analytics (docs/05, fase 7).

Metrics are computed on **net** P&L — after fees and slippage. Reporting gross numbers
on a strategy whose edge is a few percent per trade is self-deception.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..domain import ExitReason
from ..scoring.math_utils import mean, stdev
from .paper import ClosedTrade


@dataclass(frozen=True)
class Performance:
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    expectancy_pct: float
    """Expected % return per trade. The one number that decides if the strategy works."""
    profit_factor: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    total_pnl_usd: float
    roi_pct: float
    avg_hold_seconds: float
    total_fees_usd: float
    total_slippage_usd: float
    exit_reason_counts: dict[str, int]
    avg_mfe_pct: float
    avg_mae_pct: float

    def summary(self) -> str:
        return (
            f"{self.n_trades} trades | win rate {self.win_rate:.1%} | "
            f"expectancy {self.expectancy_pct:+.2f}% | PF {self.profit_factor:.2f} | "
            f"Sharpe {self.sharpe:.2f} | maxDD {self.max_drawdown_pct:.1f}% | "
            f"ROI {self.roi_pct:+.1f}%"
        )


def equity_curve(trades: Sequence[ClosedTrade], starting_equity: float) -> list[float]:
    equity = starting_equity
    curve = [equity]
    for trade in sorted(trades, key=lambda t: t.closed_at):
        equity += trade.net_pnl_usd
        curve.append(equity)
    return curve


def max_drawdown_pct(curve: Sequence[float]) -> float:
    peak = curve[0] if curve else 0.0
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, 100.0 * (peak - value) / peak)
    return worst


def _annualisation_factor(trades: Sequence[ClosedTrade]) -> float:
    """Trades per year, from the observed span. Used to annualise the per-trade ratio.

    Annualising a per-trade Sharpe on a strategy with a couple of months of history is
    already a stretch; doing it with an assumed 252 is worse. We derive it from the
    actual data and cap it, so the number stays comparable without pretending to a
    precision it does not have.
    """
    if len(trades) < 2:
        return 1.0
    ordered = sorted(trades, key=lambda t: t.closed_at)
    span_days = (ordered[-1].closed_at - ordered[0].closed_at).total_seconds() / 86400.0
    if span_days <= 0:
        return 1.0
    per_year = len(trades) * 365.0 / span_days
    return math.sqrt(min(per_year, 5000.0))


def evaluate(trades: Sequence[ClosedTrade], starting_equity: float = 10_000.0) -> Performance:
    if not trades:
        return Performance(
            0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, {}, 0.0, 0.0
        )

    returns = [t.return_pct for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    gross_profit = sum(t.net_pnl_usd for t in trades if t.net_pnl_usd > 0)
    gross_loss = abs(sum(t.net_pnl_usd for t in trades if t.net_pnl_usd <= 0))

    factor = _annualisation_factor(trades)

    def ratio(numerator: float, dispersion: float) -> float:
        """Risk-adjusted ratio, guarded against degenerate dispersion.

        When every trade returns the same thing — which happens in synthetic replays and
        whenever a run consists only of stop-outs — the standard deviation collapses to
        float noise and the ratio explodes into the millions. That is not an infinitely
        good strategy, it is an undefined ratio. If the spread of returns is under 0.1%
        of their mean magnitude, the returns are identical for practical purposes and we
        report 0 rather than a number that would be read as a result.
        """
        scale = max(abs(numerator), 1e-9)
        if dispersion / scale < 1e-3:
            return 0.0
        return (numerator / dispersion) * factor

    sharpe = ratio(mean(returns), stdev(returns))
    downside = [r for r in returns if r < 0]
    sortino = ratio(mean(returns), stdev(downside) if len(downside) > 1 else 0.0)

    curve = equity_curve(trades, starting_equity)
    total_pnl = sum(t.net_pnl_usd for t in trades)

    counts: dict[str, int] = {}
    for t in trades:
        key = t.exit_reason.value if isinstance(t.exit_reason, ExitReason) else str(t.exit_reason)
        counts[key] = counts.get(key, 0) + 1

    return Performance(
        n_trades=len(trades),
        n_wins=len(wins),
        n_losses=len(losses),
        win_rate=len(wins) / len(trades),
        avg_win_pct=mean(wins),
        avg_loss_pct=mean(losses),
        expectancy_pct=mean(returns),
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown_pct=max_drawdown_pct(curve),
        total_pnl_usd=total_pnl,
        roi_pct=100.0 * total_pnl / starting_equity if starting_equity > 0 else 0.0,
        avg_hold_seconds=mean([t.hold_seconds for t in trades]),
        total_fees_usd=sum(t.fees_usd for t in trades),
        total_slippage_usd=sum(t.slippage_usd for t in trades),
        exit_reason_counts=counts,
        avg_mfe_pct=mean([t.max_favorable_excursion for t in trades]),
        avg_mae_pct=mean([t.max_adverse_excursion for t in trades]),
    )


def attribution(trades: Sequence[ClosedTrade], bucket: str = "signal") -> dict[str, Performance]:
    """Break performance down by entry signal strength or by N_eff bucket.

    This is where you learn *which* signals pay. Aggregate performance can be positive
    while your STRONG_ENTRY bucket loses money — and you would never see it.
    """
    groups: dict[str, list[ClosedTrade]] = {}
    for t in trades:
        if bucket == "neff":
            key = "N_eff>=3" if t.neff_at_entry >= 3 else "N_eff>=2" if t.neff_at_entry >= 2 else "N_eff<2"
        else:
            key = (
                "master>=85"
                if t.master_at_entry >= 85
                else "master>=75"
                if t.master_at_entry >= 75
                else "master<75"
            )
        groups.setdefault(key, []).append(t)
    return {k: evaluate(v) for k, v in groups.items()}
