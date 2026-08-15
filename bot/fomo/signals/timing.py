"""Anti-FOMO timing multiplier (docs/04 §6).

The system is named after FOMO; this module exists so it does not trade on it.

Three independent measures of "how much already happened", combined with ``min``
rather than a product. The three are strongly correlated — a parabolic candle also
shows up as a high run-up and a high multiple — so multiplying punishes one event
three times and crushes every strong move to zero. ``min`` says "the tightest binding
observation decides", which is how a trader actually reads a chart.
"""

from __future__ import annotations

from ..config import TimingWeights, get_settings
from ..domain import MarketSnapshot, TimingAssessment
from ..scoring.math_utils import clip, sat

NO_PENALTY = 1.0
"""Used when a measure is unavailable. Missing data must not *reward* a token, but it
must not punish it either — the risk engine is where missing data becomes a veto."""


def _decay(excess: float, k: float) -> float:
    """1.0 at no excess, falling toward 0 as the move extends. ``k`` is the half-point."""
    return 1.0 - sat(max(excess, 0.0), k)


def assess_timing(
    market: MarketSnapshot,
    first_smart_entry_price: float | None,
    weights: TimingWeights | None = None,
) -> TimingAssessment:
    w = weights or get_settings().weights.timing
    price = market.price_usd

    # How far above the price the smart money paid are we? This is the one that
    # matters most: buying 4x above the trader you are copying means taking their
    # risk for a quarter of their reward.
    if first_smart_entry_price and first_smart_entry_price > 0:
        smart_multiple = price / first_smart_entry_price
        f_smart = _decay(smart_multiple - 1.0, w.k_smart_multiple)
    else:
        smart_multiple = 1.0
        f_smart = NO_PENALTY

    if market.price_15m_ago and market.price_15m_ago > 0:
        runup = price / market.price_15m_ago - 1.0
        f_runup = _decay(runup, w.k_runup_15m)
    else:
        runup = 0.0
        f_runup = NO_PENALTY

    if market.ema20_1m and market.ema20_1m > 0:
        extension = price / market.ema20_1m - 1.0
        f_para = _decay(extension, w.k_parabolic)
    else:
        extension = 0.0
        f_para = NO_PENALTY

    factors = {"smart_multiple": f_smart, "runup_15m": f_runup, "parabolic": f_para}
    binding = min(factors, key=lambda name: factors[name])

    # Floored, not zeroed: a token that already ran is demoted, not forbidden. At
    # exceptional evidence it can still surface as a WATCH, which is honest — sometimes
    # the move genuinely just started.
    multiplier = clip(factors[binding], w.floor, 1.0)

    return TimingAssessment(
        multiplier=multiplier,
        smart_multiple=smart_multiple,
        runup_15m=runup,
        parabolic_extension=extension,
        binding_factor=binding,
    )
