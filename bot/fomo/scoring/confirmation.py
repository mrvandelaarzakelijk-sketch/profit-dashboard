"""Effective independent trader count (docs/04 §4).

Five wallets run by one person are not five signals. This is the Kish effective
sample size, the standard correction for correlated observations:

              (sum q_i)^2
    N_eff = -------------------
            sum_i sum_j q_i q_j rho_ij

with q_i the trader's quality weight and rho_ii = 1. Fully independent traders give
N_eff = n; perfectly correlated ones give N_eff = 1. The behaviour you want falls out
of the formula instead of out of a list of special cases.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..domain import ConfirmationAssessment, TraderActivity, TxAction
from .math_utils import clip, mean

BUY_ACTIONS = (TxAction.OPEN, TxAction.ADD)


def correlation_key(a: str, b: str) -> str:
    """Stable, order-independent key for a trader pair."""
    return "|".join(sorted((a, b)))


def rho(correlations: dict[str, float], a: str, b: str) -> float:
    if a == b:
        return 1.0
    return clip(correlations.get(correlation_key(a, b), 0.0), 0.0, 1.0)


def effective_traders(
    qualities: Sequence[float],
    ids: Sequence[str],
    correlations: dict[str, float],
) -> float:
    """Kish effective sample size for a set of weighted, correlated traders."""
    if len(qualities) != len(ids):
        raise ValueError("qualities and ids must be the same length")
    weights = [max(q, 0.0) for q in qualities]
    total = sum(weights)
    if total <= 0:
        return 0.0

    denominator = 0.0
    for i, qi in enumerate(weights):
        for j, qj in enumerate(weights):
            denominator += qi * qj * rho(correlations, ids[i], ids[j])

    if denominator <= 0:
        return 0.0
    # Numerically, N_eff can exceed n by a hair through float error; clamp it.
    return min(total * total / denominator, float(len(weights)))


def assess_confirmation(
    activities: Sequence[TraderActivity],
    correlations: dict[str, float],
    *,
    holders_only: bool = False,
) -> ConfirmationAssessment:
    """Confirmation from traders currently buying this token.

    ``holders_only=True`` restricts to traders who still hold, which is what the exit
    logic needs: confirmation that has already sold is not confirmation any more.
    """
    buyers = [
        a
        for a in activities
        if a.action in BUY_ACTIONS and a.usd_bought > 0 and (a.still_holding or not holders_only)
    ]
    if not buyers:
        return ConfirmationAssessment(
            n_traders=0, n_effective=0.0, mean_quality=0.0, detail="no confirming traders"
        )

    # One trader can appear more than once (several buys); collapse to the best entry
    # per trader, otherwise a single wallet buying three times looks like three traders.
    by_trader: dict[str, TraderActivity] = {}
    for a in buyers:
        prev = by_trader.get(a.trader_id)
        if prev is None or a.usd_bought > prev.usd_bought:
            by_trader[a.trader_id] = a

    ids = list(by_trader)
    qualities = [by_trader[i].quality for i in ids]
    n_eff = effective_traders(qualities, ids, correlations)

    n = len(ids)
    if n > 1 and n_eff < n * 0.7:
        detail = f"{n} wallets but only {n_eff:.2f} independent (correlated)"
    else:
        detail = f"{n} traders, {n_eff:.2f} effectively independent"

    return ConfirmationAssessment(
        n_traders=n,
        n_effective=n_eff,
        mean_quality=mean(qualities),
        detail=detail,
    )
