"""Numeric primitives shared by every scorer (docs/04 §0).

Kept in one place because these four functions encode the model's core assumption:
every raw quantity in this market is heavy-tailed, so nothing enters a score linearly.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = [
    "clip",
    "sat",
    "logistic",
    "logscale",
    "center",
    "shannon_entropy",
    "effective_count",
    "median",
    "mad",
    "mean",
    "stdev",
]


def clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def sat(x: float, k: float) -> float:
    """Saturating map ``x / (x + k)`` -> [0, 1].

    ``k`` is the half-point: ``sat(k, k) == 0.5``. Negative inputs clamp to 0, which
    is what we want everywhere it is used (you cannot have negative evidence *count*).

    Mathematically the result is strictly below 1, but when ``x`` is many orders of
    magnitude larger than ``k`` the division rounds to exactly 1.0 in floating point.
    Callers may therefore rely on ``0 <= sat(...) <= 1``, not on a strict upper bound.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    if x <= 0:
        return 0.0
    return x / (x + k)


def logistic(x: float) -> float:
    """Numerically stable sigmoid."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def logscale(x: float, lo: float, hi: float) -> float:
    """Position of ``x`` between ``lo`` and ``hi`` on a log axis, clipped to [0, 1].

    Used for liquidity, market cap and token age: quantities that span orders of
    magnitude, where the interesting difference is multiplicative, not additive.
    """
    if lo <= 0 or hi <= lo:
        raise ValueError("require 0 < lo < hi")
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    return (math.log(x) - math.log(lo)) / (math.log(hi) - math.log(lo))


def center(score_0_100: float) -> float:
    """Map a 0-100 subscore to [-1, 1] evidence.

    The reason the master score uses this instead of the raw subscore: a weak
    component must *cost* points, not contribute a small positive amount.
    """
    return (clip(score_0_100, 0.0, 100.0) / 100.0 - 0.5) * 2.0


def shannon_entropy(counts: Sequence[float]) -> float:
    """Shannon entropy (nats) of a count distribution. Empty/degenerate -> 0.0."""
    positive = [c for c in counts if c > 0]
    total = sum(positive)
    if total <= 0 or len(positive) <= 1:
        return 0.0
    h = 0.0
    for c in positive:
        p = c / total
        h -= p * math.log(p)
    return h


def effective_count(counts: Sequence[float]) -> float:
    """Perplexity ``exp(H)`` — the "effective number of distinct sources".

    This is what makes "1000 tweets from 50 bots" score low without any bot-specific
    rule: the effective count is 50, not 1000 (docs/04 §3.3).
    """
    positive = [c for c in counts if c > 0]
    if not positive:
        return 0.0
    if len(positive) == 1:
        return 1.0
    return math.exp(shannon_entropy(positive))


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def stdev(values: Sequence[float]) -> float:
    """Population standard deviation (we describe the sample, we don't infer from it)."""
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def mad(values: Sequence[float]) -> float:
    """Median absolute deviation.

    Preferred over stdev for social time series: a single viral tweet inflates the
    standard deviation exactly when you need the scale estimate to be stable.
    """
    if not values:
        return 0.0
    m = median(values)
    return median([abs(v - m) for v in values])
