"""Deterministic scenario fixtures (docs/05 §3, laag 3).

These are the fake providers the integration tests and the demo run against. Every
scenario is a situation the system must get right; they double as executable
documentation of what each guard is for.

No randomness, no clock, no network — a scenario is a pure value.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..domain import (
    MarketSnapshot,
    SecuritySnapshot,
    SocialWindow,
    TokenSnapshot,
    TraderActivity,
    TxAction,
)

T0 = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def safe_security(**overrides) -> SecuritySnapshot:
    """A token that passes every hard veto. Start here and break one thing at a time."""
    base = dict(
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        lp_burned_pct=100.0,
        top10_holder_pct=18.0,
        creator_holding_pct=2.0,
        bundled_supply_pct=6.0,
        honeypot_sim_passed=True,
        creator_prior_rugs=0,
        observed_age_seconds=20.0,
    )
    return SecuritySnapshot(**{**base, **overrides})


def healthy_market(**overrides) -> MarketSnapshot:
    base = dict(
        price_usd=0.00041,
        mcap_usd=1_840_000.0,
        fdv_usd=1_840_000.0,
        liquidity_usd=312_000.0,
        liquidity_usd_secondary=308_000.0,
        volume_1h_usd=520_000.0,
        unique_buyers_15m=380,
        unique_sellers_15m=210,
        buy_count_15m=640,
        sell_count_15m=300,
        unique_buyers_growth_15m=64.0,
        holder_growth_15m=41.0,
        holders=2100,
        price_15m_ago=0.00037,
        ema20_1m=0.00039,
        token_age_seconds=15_120.0,
    )
    return MarketSnapshot(**{**base, **overrides})


def organic_social(**overrides) -> SocialWindow:
    """141 mentions from 118 effective accounts — what real interest looks like."""
    base = dict(
        mentions=141,
        mentions_per_account=tuple([3] * 8 + [2] * 15 + [1] * 87),
        quality_weighted_mentions=96.0,
        baseline_ewma=14.0,
        baseline_mad=6.0,
        high_quality_accounts=23,
        engagement_ratio=1.4,
        avg_sentiment=0.64,
        bot_share=0.06,
        duplicate_text_share=0.04,
        new_account_share=0.11,
        peak_quality_weighted=96.0,
    )
    return SocialWindow(**{**base, **overrides})


def bot_swarm_social(**overrides) -> SocialWindow:
    """1000 mentions from 50 accounts — the case the diversity term exists for."""
    base = dict(
        mentions=1000,
        mentions_per_account=tuple([20] * 50),
        quality_weighted_mentions=180.0,
        baseline_ewma=12.0,
        baseline_mad=5.0,
        high_quality_accounts=2,
        engagement_ratio=0.05,
        avg_sentiment=0.9,
        bot_share=0.78,
        duplicate_text_share=0.62,
        new_account_share=0.81,
        peak_quality_weighted=180.0,
    )
    return SocialWindow(**{**base, **overrides})


def trader(
    trader_id: str,
    quality: float,
    usd: float,
    *,
    pct_of_wallet: float = 0.06,
    age_at_entry: float = 900.0,
    price: float = 0.00041,
    still_holding: bool = True,
    action: TxAction = TxAction.OPEN,
    usd_sold: float = 0.0,
) -> TraderActivity:
    return TraderActivity(
        trader_id=trader_id,
        wallet_address=f"wallet_{trader_id}",
        quality=quality,
        action=action,
        usd_bought=usd,
        usd_sold=usd_sold,
        pct_of_wallet=pct_of_wallet,
        token_age_at_entry_seconds=age_at_entry,
        price_at_entry=price,
        still_holding=still_holding,
        event_at=T0,
    )


def build(
    *,
    symbol: str = "XYZ",
    market: MarketSnapshot | None = None,
    security: SecuritySnapshot | None = None,
    social: SocialWindow | None = None,
    activities: tuple[TraderActivity, ...] = (),
    correlations: dict[str, float] | None = None,
    first_smart_entry_price: float | None = 0.00041,
    intended_position_usd: float = 1000.0,
    at: datetime = T0,
) -> TokenSnapshot:
    return TokenSnapshot(
        address=f"So1{symbol}1111111111111111111111111111111111",
        symbol=symbol,
        observed_at=at,
        market=market or healthy_market(),
        security=security or safe_security(),
        social=social or organic_social(),
        activities=activities,
        correlations=correlations or {},
        first_smart_entry_price=first_smart_entry_price,
        intended_position_usd=intended_position_usd,
    )


# ── Named scenarios ──────────────────────────────────────────────────────────


def happy_path() -> TokenSnapshot:
    """Strong, independent smart money on a safe token, early in the move."""
    return build(
        activities=(
            trader("A", 0.91, 42_000, pct_of_wallet=0.11, age_at_entry=420),
            trader("B", 0.78, 18_500, pct_of_wallet=0.08, age_at_entry=900),
            trader("C", 0.72, 11_200, pct_of_wallet=0.05, age_at_entry=1500),
        ),
    )


def late_entry() -> TokenSnapshot:
    """Same evidence as happy_path, but the price already ran 4x past smart money."""
    snap = happy_path()
    return snap.model_copy(
        update={
            "market": healthy_market(
                price_usd=0.00164, price_15m_ago=0.00061, ema20_1m=0.00098, mcap_usd=7_360_000.0
            ),
            "first_smart_entry_price": 0.00041,
        }
    )


def cluster_illusion() -> TokenSnapshot:
    """Five wallets that are really one person. Raw counts say 'strongest signal today'."""
    ids = ["A", "B", "C", "D", "E"]
    correlations = {
        "|".join(sorted((a, b))): 0.97 for i, a in enumerate(ids) for b in ids[i + 1 :]
    }
    return build(
        activities=tuple(trader(i, 0.80, 9_000) for i in ids),
        correlations=correlations,
    )


def bot_swarm() -> TokenSnapshot:
    """Twitter is on fire, almost nobody is buying."""
    return build(
        social=bot_swarm_social(),
        market=healthy_market(unique_buyers_15m=11, unique_buyers_growth_15m=2.0, holder_growth_15m=1.0),
        activities=(trader("A", 0.52, 3_000),),
    )


def honeypot() -> TokenSnapshot:
    return build(
        security=safe_security(honeypot_sim_passed=False, sell_tax_bps=9500),
        activities=(
            trader("A", 0.91, 42_000),
            trader("B", 0.85, 30_000),
            trader("C", 0.80, 25_000),
        ),
    )


def mint_authority_live() -> TokenSnapshot:
    return build(
        security=safe_security(mint_authority_revoked=False),
        activities=(trader("A", 0.91, 42_000), trader("B", 0.85, 30_000)),
    )


def rug_setup() -> TokenSnapshot:
    """LP not secured and the creator is holding a fifth of supply."""
    return build(
        security=safe_security(lp_burned_pct=0.0, lp_locked_seconds_remaining=None, creator_holding_pct=21.0),
        activities=(trader("A", 0.88, 20_000),),
    )


def thin_liquidity() -> TokenSnapshot:
    return build(
        market=healthy_market(liquidity_usd=9_000.0, liquidity_usd_secondary=8_800.0, mcap_usd=400_000.0),
        activities=(trader("A", 0.9, 5_000), trader("B", 0.8, 4_000)),
    )


def data_disagreement() -> TokenSnapshot:
    return build(
        market=healthy_market(liquidity_usd=312_000.0, liquidity_usd_secondary=120_000.0),
        activities=(trader("A", 0.9, 20_000), trader("B", 0.85, 15_000)),
    )


def insider_distribution() -> TokenSnapshot:
    return build(
        market=healthy_market(insider_net_flow_usd=-85_000.0),
        activities=(trader("A", 0.9, 20_000), trader("B", 0.85, 15_000)),
    )


ALL_SCENARIOS = {
    "happy_path": happy_path,
    "late_entry": late_entry,
    "cluster_illusion": cluster_illusion,
    "bot_swarm": bot_swarm,
    "honeypot": honeypot,
    "mint_authority_live": mint_authority_live,
    "rug_setup": rug_setup,
    "thin_liquidity": thin_liquidity,
    "data_disagreement": data_disagreement,
    "insider_distribution": insider_distribution,
}
