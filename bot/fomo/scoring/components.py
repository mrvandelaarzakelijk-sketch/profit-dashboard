"""The four evidence subscores: smart money, social, market, liquidity.

All four return 0-100 and none of them look at price appreciation as a positive —
that lives in the timing multiplier (docs/04 §6). Putting price gain in a score is
literally FOMO in code.
"""

from __future__ import annotations

from ..config import (
    LiquidityThresholds,
    MarketWeights,
    SmartMoneyWeights,
    SocialWeights,
    get_settings,
)
from ..domain import ConfirmationAssessment, MarketSnapshot, SocialWindow, TokenSnapshot
from .confirmation import BUY_ACTIONS
from .math_utils import clip, effective_count, logistic, logscale, sat


# ── Smart money ──────────────────────────────────────────────────────────────


def score_smart_money(
    snapshot: TokenSnapshot,
    confirmation: ConfirmationAssessment,
    weights: SmartMoneyWeights | None = None,
) -> tuple[float, dict[str, float]]:
    w = weights or get_settings().weights.smart_money

    buys = [a for a in snapshot.activities if a.action in BUY_ACTIONS]
    usd_in = sum(a.usd_bought for a in buys)
    usd_out = sum(a.usd_sold for a in snapshot.activities)
    conviction = max((a.pct_of_wallet for a in buys), default=0.0)
    earliest_age = min((a.token_age_at_entry_seconds for a in buys), default=w.age_hi)

    f_neff = sat(confirmation.n_effective - 1.0, w.k_neff)
    f_quality = confirmation.mean_quality
    f_usd = sat(usd_in, w.k_usd)
    f_conviction = sat(conviction, w.k_conviction)
    f_early = 1.0 - logscale(max(earliest_age, w.age_lo), w.age_lo, w.age_hi)
    # Selling by the same cohort is rotation, not accumulation, and it counts against.
    f_sell = sat(usd_out / usd_in, 1.0) if usd_in > 0 else (1.0 if usd_out > 0 else 0.0)

    evidence = (
        w.bias
        + w.c_neff * f_neff
        + w.c_quality * f_quality
        + w.c_usd * f_usd
        + w.c_conviction * f_conviction
        + w.c_early * f_early
        - w.c_sell * f_sell
    )
    features = {
        "sm_neff": f_neff,
        "sm_quality": f_quality,
        "sm_usd": f_usd,
        "sm_conviction": f_conviction,
        "sm_early": f_early,
        "sm_sell": f_sell,
        "sm_usd_in": usd_in,
        "sm_usd_out": usd_out,
    }
    return 100.0 * logistic(evidence), features


# ── Social ───────────────────────────────────────────────────────────────────


def social_velocity_z(social: SocialWindow, floor: float = 1.0) -> float:
    """Robust velocity: deviation from the token's own baseline in MAD units.

    A percentage change is unusable here — every new token has a baseline of zero and
    would show +infinity. The MAD floor keeps a brand-new token's first mentions from
    producing a meaningless spike (docs/04 §3.2).
    """
    scale = max(social.baseline_mad, floor)
    return (social.quality_weighted_mentions - social.baseline_ewma) / scale


def social_diversity(social: SocialWindow) -> tuple[float, float]:
    """(diversity, effective account count) — how "1000 tweets from 50 bots" dies."""
    counts = list(social.mentions_per_account)
    if not counts:
        return 0.0, 0.0
    n_eff_accounts = effective_count(counts)
    total = sum(counts)
    diversity = n_eff_accounts / total if total > 0 else 0.0
    return clip(diversity, 0.0, 1.0), n_eff_accounts


def score_social(
    social: SocialWindow, weights: SocialWeights | None = None
) -> tuple[float, dict[str, float]]:
    w = weights or get_settings().weights.social

    if social.mentions == 0:
        # No data is not bearish; it is absent. Return the neutral-low anchor so that
        # center() makes it a mild negative rather than a catastrophic one.
        return 100.0 * logistic(w.bias), {
            "soc_velocity_z": 0.0,
            "soc_diversity": 0.0,
            "soc_accounts_eff": 0.0,
            "soc_engagement": 0.0,
            "soc_sentiment": 0.0,
            "soc_bot_share": social.bot_share,
            "soc_duplicate_share": social.duplicate_text_share,
        }

    velocity_z = social_velocity_z(social)
    diversity, n_eff_accounts = social_diversity(social)

    evidence = (
        w.bias
        + w.a_velocity * sat(max(velocity_z, 0.0), w.k_velocity)
        + w.a_diversity * diversity
        + w.a_accounts * sat(n_eff_accounts, w.k_accounts)
        + w.a_engagement * sat(social.engagement_ratio, 1.0)
        + w.a_sentiment * social.avg_sentiment
    )
    # Coordinated shilling does not make the signal weaker, it makes it invalid.
    penalty = (1.0 - social.bot_share) * (1.0 - social.duplicate_text_share)
    score = 100.0 * logistic(evidence) * penalty

    features = {
        "soc_velocity_z": velocity_z,
        "soc_diversity": diversity,
        "soc_accounts_eff": n_eff_accounts,
        "soc_engagement": social.engagement_ratio,
        "soc_sentiment": social.avg_sentiment,
        "soc_bot_share": social.bot_share,
        "soc_duplicate_share": social.duplicate_text_share,
    }
    return clip(score, 0.0, 100.0), features


# ── Market ───────────────────────────────────────────────────────────────────


def score_market(
    market: MarketSnapshot, weights: MarketWeights | None = None
) -> tuple[float, dict[str, float]]:
    w = weights or get_settings().weights.market

    turnover = market.volume_1h_usd / market.liquidity_usd if market.liquidity_usd > 0 else 0.0
    # unique buyers, not buy count: 500 buys from 4 wallets is wash trading.
    buyer_ratio = market.unique_buyers_15m / max(market.unique_sellers_15m, 1)

    evidence = (
        w.bias
        + w.m_volume * sat(turnover, w.k_volume)
        + w.m_buyer_ratio * clip(buyer_ratio - 1.0, -1.0, 1.0)
        + w.m_buyer_growth * sat(market.unique_buyers_growth_15m, w.k_buyer_growth)
        + w.m_holder_growth * sat(market.holder_growth_15m, w.k_holder_growth)
    )
    features = {
        "mkt_turnover": turnover,
        "mkt_buyer_ratio": buyer_ratio,
        "mkt_buyer_growth": market.unique_buyers_growth_15m,
        "mkt_holder_growth": market.holder_growth_15m,
        "mkt_unique_buyers_15m": float(market.unique_buyers_15m),
    }
    return 100.0 * logistic(evidence), features


# ── Liquidity ────────────────────────────────────────────────────────────────


def price_impact_pct(size_usd: float, liquidity_usd: float) -> float:
    """Constant-product price impact for a trade of ``size_usd``.

    With total pool liquidity L (both sides), the quote reserve is ~L/2, so
    impact = S / (L/2 + S). Not exact for concentrated liquidity, but it is the right
    shape and it is *conservative*, which is the only direction you want to be wrong.
    """
    if liquidity_usd <= 0:
        return 100.0
    quote_reserve = liquidity_usd / 2.0
    return 100.0 * size_usd / (quote_reserve + size_usd)


def roundtrip_cost_pct(size_usd: float, liquidity_usd: float, cfg: LiquidityThresholds) -> float:
    """Full cost of getting in and back out: impact twice, fees twice, priority fee."""
    impact = price_impact_pct(size_usd, liquidity_usd)
    fees = 2.0 * cfg.dex_fee_pct
    priority = 100.0 * cfg.priority_fee_usd / size_usd if size_usd > 0 else 0.0
    return 2.0 * impact + fees + priority


def score_liquidity(
    market: MarketSnapshot,
    size_usd: float,
    cfg: LiquidityThresholds | None = None,
) -> tuple[float, dict[str, float]]:
    """Not "how much liquidity is there" but "can I trade *my* size affordably".

    A $40k pool is fine for $300 and useless for $20,000. A score that ignores your
    intended size is not a liquidity score, it is a number.
    """
    c = cfg or get_settings().thresholds.liquidity

    cost = roundtrip_cost_pct(size_usd, market.liquidity_usd, c)
    affordability = clip(1.0 - cost / c.max_roundtrip_cost_pct, 0.0, 1.0)
    depth = logscale(max(market.liquidity_usd, 1.0), c.liq_floor_usd, c.liq_good_usd)
    score = 100.0 * affordability * depth

    features = {
        "liq_usd": market.liquidity_usd,
        "liq_roundtrip_cost_pct": cost,
        "liq_affordability": affordability,
        "liq_depth": depth,
        "liq_mcap_ratio": (
            market.liquidity_usd / market.mcap_usd if market.mcap_usd > 0 else 0.0
        ),
    }
    return clip(score, 0.0, 100.0), features


def max_affordable_size_usd(liquidity_usd: float, cfg: LiquidityThresholds) -> float:
    """Largest position whose roundtrip cost stays within budget.

    Solving ``200*S/(L/2+S) + 2*dex_fee + 100*priority/S = budget`` for S. The priority
    fee term makes S appear on both sides, so we solve by fixed-point iteration: start
    from the closed-form solution that ignores the fixed fee, then refine. It converges
    in a handful of steps and, unlike the closed form, never returns a size that
    overshoots the budget — which matters, because this number caps real position sizes.
    """
    if liquidity_usd <= 0:
        return 0.0

    def _size_for(budget: float) -> float:
        if budget <= 0:
            return 0.0
        if budget >= 200.0:
            return float("inf")
        return budget * liquidity_usd / (2.0 * (200.0 - budget))

    variable_budget = cfg.max_roundtrip_cost_pct - 2.0 * cfg.dex_fee_pct
    size = _size_for(variable_budget)
    if size <= 0 or size == float("inf"):
        return max(size, 0.0)

    for _ in range(24):
        budget = variable_budget - 100.0 * cfg.priority_fee_usd / size
        next_size = _size_for(budget)
        if next_size <= 0:
            return 0.0
        if abs(next_size - size) < 1e-9 * max(size, 1.0):
            size = next_size
            break
        size = next_size

    return max(size, 0.0)
