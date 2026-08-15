"""Property-based invariants (docs/05 §3, laag 2).

These are the machine-checkable form of the design's promises. Example-based tests show
the system works on the cases I thought of; these show it holds for cases I did not.

The monotonicity properties are the important ones — they are the executable statement
of "this system must not trade FOMO".
"""

from __future__ import annotations

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from fomo.adapters import scenarios
from fomo.adapters.scenarios import healthy_market, safe_security, trader
from fomo.domain import SignalState, TraderStats
from fomo.risk import assess_risk
from fomo.scoring.confirmation import correlation_key, effective_traders
from fomo.scoring.math_utils import center, logistic, logscale, sat
from fomo.scoring.trader import score_trader
from fomo.signals import evaluate

SLOW = settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])

finite = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)
positive = st.floats(min_value=1e-6, max_value=1e9, allow_nan=False, allow_infinity=False)
unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


# ── math primitives ──────────────────────────────────────────────────────────


@given(x=st.floats(min_value=0, max_value=1e12, allow_nan=False), k=positive)
def test_sat_is_bounded(x, k):
    assert 0.0 <= sat(x, k) <= 1.0


@given(finite)
def test_logistic_is_bounded(x):
    assert 0.0 <= logistic(x) <= 1.0


@given(x=positive, lo=positive, hi=positive)
def test_logscale_is_bounded(x, lo, hi):
    assume(hi > lo * 1.0001)
    assert 0.0 <= logscale(x, lo, hi) <= 1.0


@given(finite)
def test_center_is_bounded(s):
    assert -1.0 <= center(s) <= 1.0


# ── effective trader count ───────────────────────────────────────────────────


@given(
    qualities=st.lists(st.floats(min_value=0.01, max_value=1.0), min_size=1, max_size=8),
    rho=unit,
)
@SLOW
def test_effective_traders_never_exceeds_headcount(qualities, rho):
    ids = [f"t{i}" for i in range(len(qualities))]
    correlations = {correlation_key(a, b): rho for i, a in enumerate(ids) for b in ids[i + 1 :]}
    n_eff = effective_traders(qualities, ids, correlations)
    assert 0.0 < n_eff <= len(qualities) + 1e-9


@given(
    qualities=st.lists(st.floats(min_value=0.1, max_value=1.0), min_size=2, max_size=6),
    rho_low=st.floats(min_value=0.0, max_value=0.4),
    rho_high=st.floats(min_value=0.6, max_value=1.0),
)
@SLOW
def test_more_correlation_never_increases_effective_count(qualities, rho_low, rho_high):
    ids = [f"t{i}" for i in range(len(qualities))]
    low = {correlation_key(a, b): rho_low for i, a in enumerate(ids) for b in ids[i + 1 :]}
    high = {correlation_key(a, b): rho_high for i, a in enumerate(ids) for b in ids[i + 1 :]}
    assert effective_traders(qualities, ids, high) <= effective_traders(qualities, ids, low) + 1e-9


# ── trader score ─────────────────────────────────────────────────────────────


@given(
    n_trades=st.integers(min_value=0, max_value=5000),
    n_wins=st.integers(min_value=0, max_value=5000),
    exit_quality=unit,
    bot=unit,
    suspicious=unit,
    trades_per_day=st.floats(min_value=0.0, max_value=5000.0),
)
@SLOW
def test_trader_score_stays_in_range(n_trades, n_wins, exit_quality, bot, suspicious, trades_per_day):
    stats = TraderStats(
        trader_id="t",
        n_trades=n_trades,
        n_wins=min(n_wins, n_trades),
        exit_quality=exit_quality,
        bot_likeness=bot,
        suspicious=suspicious,
        trades_per_day=trades_per_day,
    )
    assert 0.0 <= score_trader(stats).score <= 100.0


@given(n=st.integers(min_value=1, max_value=500))
@SLOW
def test_shrinkage_moves_toward_the_prior_as_evidence_shrinks(n):
    """Fewer trades must always mean a score closer to 50."""
    def score_for(trades: int) -> float:
        return score_trader(
            TraderStats(
                trader_id="t",
                n_trades=trades,
                n_wins=trades,
                log_returns=tuple([0.6, 0.5] * max(trades // 2, 1)),
                largest_win_usd=1.0,
                total_win_usd=float(max(trades, 1)),
                exit_quality=0.8,
                median_entry_mcap_usd=50_000.0,
                median_entry_age_seconds=600.0,
            )
        ).score

    assert abs(score_for(n) - 50.0) <= abs(score_for(n * 4) - 50.0) + 1e-6


# ── master score & gating ────────────────────────────────────────────────────


@given(
    price_multiple=st.floats(min_value=1.0, max_value=50.0),
    n_traders=st.integers(min_value=1, max_value=4),
    quality=st.floats(min_value=0.3, max_value=1.0),
)
@SLOW
def test_all_scores_bounded(price_multiple, n_traders, quality):
    snapshot = scenarios.build(
        market=healthy_market(price_usd=0.00041 * price_multiple),
        activities=tuple(trader(f"t{i}", quality, 10_000) for i in range(n_traders)),
    )
    decision = evaluate(snapshot)
    for value in (
        decision.master_score,
        decision.smart_money_score,
        decision.social_score,
        decision.market_score,
        decision.liquidity_score,
    ):
        assert 0.0 <= value <= 100.0


@given(
    price_multiple=st.floats(min_value=1.0, max_value=40.0),
    usd=st.floats(min_value=1_000.0, max_value=500_000.0),
    quality=st.floats(min_value=0.2, max_value=1.0),
)
@SLOW
def test_a_veto_always_zeroes_the_master_score(price_multiple, usd, quality):
    """The absorbing property: no input combination survives a veto."""
    snapshot = scenarios.build(
        security=safe_security(mint_authority_revoked=False),
        market=healthy_market(price_usd=0.00041 * price_multiple),
        activities=tuple(trader(f"t{i}", quality, usd) for i in range(4)),
    )
    decision = evaluate(snapshot)
    assert decision.risk.veto is True
    assert decision.master_score == 0.0
    assert decision.state is SignalState.AVOID


@given(
    lower=st.floats(min_value=1.0, max_value=5.0),
    step=st.floats(min_value=0.2, max_value=20.0),
)
@SLOW
def test_master_score_never_rises_with_price_run_up(lower, step):
    """The anti-FOMO guarantee, stated as a property.

    Holding all evidence fixed, a token that has already run further must never score
    higher. If this ever fails, the system has started chasing pumps.
    """
    base = scenarios.happy_path()
    cheap = evaluate(base.model_copy(update={"market": healthy_market(price_usd=0.00041 * lower)}))
    dear = evaluate(base.model_copy(update={"market": healthy_market(price_usd=0.00041 * (lower + step))}))
    assert dear.master_score <= cheap.master_score + 1e-9


@given(extra_usd=st.floats(min_value=0.0, max_value=200_000.0))
@SLOW
def test_more_smart_money_never_lowers_the_smart_money_score(extra_usd):
    base_activities = (trader("A", 0.9, 20_000),)
    more_activities = (trader("A", 0.9, 20_000 + extra_usd),)
    low = evaluate(scenarios.build(activities=base_activities))
    high = evaluate(scenarios.build(activities=more_activities))
    assert high.smart_money_score >= low.smart_money_score - 1e-9


@given(top10=st.floats(min_value=0.0, max_value=44.0))
@SLOW
def test_soft_risk_multiplier_is_monotone_in_concentration(top10):
    lower = assess_risk(safe_security(top10_holder_pct=top10), healthy_market())
    higher = assess_risk(safe_security(top10_holder_pct=min(top10 + 5.0, 44.9)), healthy_market())
    assert higher.soft_multiplier <= lower.soft_multiplier + 1e-9


@given(
    liquidity=st.floats(min_value=1.0, max_value=5_000_000.0),
    top10=st.floats(min_value=0.0, max_value=100.0),
    creator=st.floats(min_value=0.0, max_value=100.0),
)
@SLOW
def test_risk_assessment_output_is_always_well_formed(liquidity, top10, creator):
    result = assess_risk(
        safe_security(top10_holder_pct=top10, creator_holding_pct=creator),
        healthy_market(liquidity_usd=liquidity, liquidity_usd_secondary=liquidity),
    )
    floor = 0.30
    assert floor - 1e-9 <= result.soft_multiplier <= 1.0
    assert 0.0 <= result.risk_score <= 100.0
    assert result.veto == bool(result.veto_reasons)


# ── portfolio ────────────────────────────────────────────────────────────────


@given(equity=st.floats(min_value=1_000.0, max_value=10_000_000.0))
@SLOW
def test_position_size_never_exceeds_the_equity_cap(equity):
    from fomo.trading.paper import PaperBroker

    broker = PaperBroker(starting_equity_usd=equity)
    market = healthy_market()
    decision = evaluate(scenarios.happy_path())
    size = broker.position_size_usd(decision, market, equity)
    cap = equity * broker.settings.thresholds.position.max_position_pct_of_equity / 100.0
    assert 0.0 <= size <= cap + 1e-6


@given(price_multiple=st.floats(min_value=0.05, max_value=20.0))
@SLOW
def test_cash_and_positions_always_reconcile(price_multiple):
    from fomo.adapters.scenarios import T0
    from fomo.trading.paper import PaperBroker

    broker = PaperBroker(starting_equity_usd=50_000.0)
    position = broker.open_position(evaluate(scenarios.happy_path()), healthy_market(), T0)
    assert position is not None
    price = position.entry_price * price_multiple
    prices = {position.token_address: price}
    assert broker.equity_usd(prices) == broker.cash_usd + broker.open_value_usd(prices)
