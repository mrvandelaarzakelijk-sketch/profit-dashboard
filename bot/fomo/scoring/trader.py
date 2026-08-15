"""Trader scoring (docs/04 §2).

The single most important idea in this file is **shrinkage**. Memecoin wallets have
tiny, heavy-tailed track records; a wallet with 3 wins out of 3 does not have a 100%
win rate, it has three data points. Every component here is either shrunk toward a
prior or defaults to 0.5 ("unknown") when the evidence is absent — never to 0 and
never to 1.
"""

from __future__ import annotations

from ..config import TraderWeights, get_settings
from ..domain import Reliability, TraderScore, TraderStats
from .math_utils import clip, logistic, logscale, mean, sat, stdev

UNKNOWN = 0.5
"""The value a component takes when we have no evidence. Deliberately neutral."""


def _perf(stats: TraderStats, w: TraderWeights) -> float:
    """Risk-adjusted return via per-trade information ratio."""
    r = list(stats.log_returns)
    if len(r) < 2:
        return UNKNOWN
    sd = stdev(r)
    if sd == 0.0:
        # Identical returns every trade: real traders do not do this. Treat the
        # direction as the only information and stay close to neutral.
        return 0.75 if mean(r) > 0 else 0.25
    return logistic(w.k_ir * (mean(r) / sd))


def _winrate(stats: TraderStats, w: TraderWeights) -> float:
    """Beta-shrunk win rate. With no trades this returns the prior mean."""
    wins = min(stats.n_wins, stats.n_trades)
    return (wins + w.prior_alpha) / (stats.n_trades + w.prior_alpha + w.prior_beta)


def _consistency(stats: TraderStats) -> float:
    """1 = profit spread evenly across trades, 0 = all profit from a single trade."""
    n = stats.n_trades
    if n < 2 or stats.total_win_usd <= 0:
        return UNKNOWN
    concentration = clip(stats.largest_win_usd / stats.total_win_usd, 0.0, 1.0)
    baseline = 1.0 / n  # concentration you would see under a perfectly even split
    return clip((1.0 - concentration) / (1.0 - baseline), 0.0, 1.0)


def _early(stats: TraderStats, w: TraderWeights) -> float:
    """How early the trader enters: lower market cap and lower token age are better."""
    parts: list[float] = []
    if stats.median_entry_mcap_usd > 0:
        parts.append(1.0 - logscale(stats.median_entry_mcap_usd, w.entry_mcap_lo, w.entry_mcap_hi))
    if stats.median_entry_age_seconds > 0:
        parts.append(1.0 - logscale(stats.median_entry_age_seconds, w.entry_age_lo, w.entry_age_hi))
    return mean(parts) if parts else UNKNOWN


def _penalty(stats: TraderStats, w: TraderWeights) -> float:
    """Multiplicative, because bot-like behaviour invalidates the signal rather than
    slightly reducing it (docs/04 §2.2)."""
    bot = 1.0 - w.penalty_bot * stats.bot_likeness
    churn = 1.0 - w.penalty_churn * sat(stats.trades_per_day, w.churn_k)
    suspicious = 1.0 - w.penalty_suspicious * stats.suspicious
    return clip(bot * churn * suspicious, 0.0, 1.0)


def _reliability(stats: TraderStats, w: TraderWeights) -> Reliability:
    if stats.n_trades >= w.reliability_high_n and stats.active_days >= w.reliability_high_days:
        return Reliability.HIGH
    if stats.n_trades >= w.reliability_medium_n:
        return Reliability.MEDIUM
    if stats.n_trades >= w.reliability_low_n:
        return Reliability.LOW
    return Reliability.UNPROVEN


def score_trader(stats: TraderStats, weights: TraderWeights | None = None) -> TraderScore:
    """Score one trader 0-100.

    An unproven trader is pulled toward 50, not toward 0: "unknown" and "bad" are
    different states and the signal engine treats them differently.
    """
    w = weights or get_settings().weights.trader

    components = {
        "perf": _perf(stats, w),
        "winrate": _winrate(stats, w),
        "consistency": _consistency(stats),
        "early": _early(stats, w),
        "exit_quality": stats.exit_quality if stats.n_trades > 0 else UNKNOWN,
    }

    evidence = (
        w.bias
        + w.w_perf * components["perf"]
        + w.w_winrate * components["winrate"]
        + w.w_consistency * components["consistency"]
        + w.w_early * components["early"]
        + w.w_exit_quality * components["exit_quality"]
    )
    raw = logistic(evidence)

    shrink = stats.n_trades / (stats.n_trades + w.shrink_k)
    shrunk = 0.5 + (raw - 0.5) * shrink
    penalty = _penalty(stats, w)

    return TraderScore(
        trader_id=stats.trader_id,
        nickname=stats.nickname,
        score=clip(100.0 * shrunk * penalty, 0.0, 100.0),
        reliability=_reliability(stats, w),
        components={**components, "raw": raw, "shrink": shrink},
        penalty=penalty,
        weights_version=get_settings().weights.version,
    )
