"""Paper trading engine: fills, costs, exits, circuit breakers."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fomo.adapters import scenarios
from fomo.adapters.scenarios import T0, healthy_market, trader
from fomo.domain import ExitReason, SignalState
from fomo.scoring.components import max_affordable_size_usd, price_impact_pct
from fomo.signals import evaluate
from fomo.trading.analytics import evaluate as evaluate_performance
from fomo.trading.paper import PaperBroker


def entry_decision():
    decision = evaluate(scenarios.happy_path())
    assert decision.is_entry
    return decision


def broker(equity: float = 10_000.0) -> PaperBroker:
    return PaperBroker(starting_equity_usd=equity)


class TestPriceImpact:
    def test_impact_grows_with_size(self):
        assert price_impact_pct(100, 100_000) < price_impact_pct(10_000, 100_000)

    def test_impact_shrinks_with_liquidity(self):
        assert price_impact_pct(1_000, 500_000) < price_impact_pct(1_000, 50_000)

    def test_empty_pool_is_total_impact(self):
        assert price_impact_pct(1_000, 0.0) == 100.0

    def test_max_affordable_size_respects_budget(self):
        from fomo.config import get_settings

        cfg = get_settings().thresholds.liquidity
        size = max_affordable_size_usd(300_000.0, cfg)
        from fomo.scoring.components import roundtrip_cost_pct

        assert roundtrip_cost_pct(size, 300_000.0, cfg) <= cfg.max_roundtrip_cost_pct + 0.01


class TestEntries:
    def test_opening_a_position_moves_cash(self):
        b = broker()
        market = healthy_market()
        position = b.open_position(entry_decision(), market, T0)
        assert position is not None
        assert b.cash_usd == pytest.approx(10_000.0 - position.cost_basis_usd)
        assert position.cost_basis_usd <= 500.0  # 5% of equity cap

    def test_effective_entry_price_is_worse_than_quote(self):
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        assert position.entry_price_effective > position.entry_price

    def test_non_entry_states_are_rejected(self):
        b = broker()
        decision = evaluate(scenarios.late_entry())
        assert decision.state is SignalState.AVOID
        assert b.open_position(decision, healthy_market(), T0) is None

    def test_duplicate_position_is_rejected(self):
        b = broker()
        decision = entry_decision()
        assert b.open_position(decision, healthy_market(), T0) is not None
        assert b.open_position(decision, healthy_market(), T0) is None

    def test_concurrent_position_limit(self):
        b = broker(equity=1_000_000.0)
        limit = b.settings.thresholds.position.max_concurrent_positions
        for i in range(limit + 3):
            snapshot = scenarios.happy_path().model_copy(update={"address": f"token{i}", "symbol": f"T{i}"})
            b.open_position(evaluate(snapshot), healthy_market(), T0)
        assert len(b.positions) == limit

    def test_size_is_capped_by_pool_depth_not_just_equity(self):
        """The bot shrinks its size rather than skipping the trade — but it really shrinks."""
        b = broker(equity=1_000_000.0)
        thin = healthy_market(liquidity_usd=60_000.0, liquidity_usd_secondary=60_000.0)
        decision = evaluate(scenarios.happy_path().model_copy(update={"market": thin}))
        size = b.position_size_usd(decision, thin, 1_000_000.0)
        assert size < 1_000_000.0 * 0.05


class TestExits:
    def test_stop_loss_closes_the_whole_position(self):
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        crashed = healthy_market(price_usd=position.entry_price_effective * 0.5)
        fills = b.on_tick(position.token_address, crashed, T0 + timedelta(minutes=5))
        assert [f.reason for f in fills] == [ExitReason.STOP_LOSS]
        assert b.positions == {}
        assert b.trades[0].net_pnl_usd < 0

    def test_scaled_take_profit_sells_in_tranches(self):
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        up = healthy_market(price_usd=position.entry_price_effective * 1.7)
        fills = b.on_tick(position.token_address, up, T0 + timedelta(minutes=10))
        assert len(fills) == 1
        assert fills[0].reason is ExitReason.TAKE_PROFIT
        assert 0.0 < position.remaining_fraction < 1.0

    def test_trailing_exit_after_a_take_profit(self):
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        entry = position.entry_price_effective
        b.on_tick(position.token_address, healthy_market(price_usd=entry * 1.8), T0 + timedelta(minutes=10))
        fills = b.on_tick(
            position.token_address, healthy_market(price_usd=entry * 1.2), T0 + timedelta(minutes=20)
        )
        assert any(f.reason is ExitReason.TRAILING for f in fills)
        assert b.positions == {}

    def test_smart_money_exit_closes_immediately(self):
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        fills = b.on_tick(
            position.token_address,
            healthy_market(),
            T0 + timedelta(minutes=3),
            smart_money_exit_share=0.6,
        )
        assert [f.reason for f in fills] == [ExitReason.SMART_MONEY_EXIT]

    def test_emergency_exit_on_liquidity_collapse(self):
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        draining = healthy_market(liquidity_drop_5m_pct=70.0)
        fills = b.on_tick(position.token_address, draining, T0 + timedelta(minutes=2))
        assert [f.reason for f in fills] == [ExitReason.EMERGENCY]

    def test_emergency_beats_take_profit(self):
        """Ordering matters: a rug in progress is not a profitable exit opportunity."""
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        both = healthy_market(price_usd=position.entry_price_effective * 2.0, liquidity_drop_5m_pct=80.0)
        fills = b.on_tick(position.token_address, both, T0 + timedelta(minutes=4))
        assert [f.reason for f in fills] == [ExitReason.EMERGENCY]

    def test_timeout_closes_stale_positions(self):
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        later = T0 + timedelta(seconds=b.settings.thresholds.exit.max_hold_seconds + 60)
        fills = b.on_tick(position.token_address, healthy_market(), later)
        assert [f.reason for f in fills] == [ExitReason.TIMEOUT]

    def test_tick_on_unknown_token_is_a_noop(self):
        assert broker().on_tick("nope", healthy_market(), T0) == []


class TestAccounting:
    def test_cash_plus_positions_equals_equity(self):
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        prices = {position.token_address: position.entry_price}
        equity = b.equity_usd(prices)
        assert equity == pytest.approx(b.cash_usd + b.open_value_usd(prices))
        # Entry costs are real: equity right after entry is below the starting balance.
        assert equity < 10_000.0

    def test_round_trip_at_flat_price_loses_the_costs(self):
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        b.on_tick(
            position.token_address, healthy_market(), T0 + timedelta(minutes=1), smart_money_exit_share=1.0
        )
        trade = b.trades[0]
        assert trade.net_pnl_usd < 0
        assert trade.fees_usd > 0
        assert trade.slippage_usd > 0

    def test_mfe_and_mae_are_tracked(self):
        b = broker()
        position = b.open_position(entry_decision(), healthy_market(), T0)
        entry = position.entry_price_effective
        b.on_tick(position.token_address, healthy_market(price_usd=entry * 1.3), T0 + timedelta(minutes=2))
        b.on_tick(position.token_address, healthy_market(price_usd=entry * 0.9), T0 + timedelta(minutes=4))
        assert position.max_favorable_excursion == pytest.approx(30.0, abs=0.5)
        assert position.max_adverse_excursion == pytest.approx(-10.0, abs=0.5)

    def test_daily_loss_circuit_breaker_halts_trading(self):
        b = broker()
        b._day_start_equity = 10_000.0
        b._current_day = T0.date().isoformat()
        reason = b.can_open(entry_decision(), T0, equity=8_000.0)  # -20%, limit is 15%
        assert reason is not None and "daily loss" in reason
        assert b.open_position(entry_decision(), healthy_market(), T0) is None


class TestAnalytics:
    def test_empty_trade_log(self):
        performance = evaluate_performance([])
        assert performance.n_trades == 0
        assert performance.expectancy_pct == 0.0

    def test_metrics_on_a_realistic_run(self):
        b = broker(equity=100_000.0)
        for i in range(6):
            snapshot = scenarios.happy_path().model_copy(update={"address": f"t{i}", "symbol": f"T{i}"})
            position = b.open_position(evaluate(snapshot), healthy_market(), T0 + timedelta(hours=i))
            assert position is not None
            multiple = 2.2 if i % 2 == 0 else 0.5
            b.on_tick(
                position.token_address,
                healthy_market(price_usd=position.entry_price_effective * multiple),
                T0 + timedelta(hours=i, minutes=30),
            )
            # Winners exit on the trailing stop after the take-profit tranche.
            if position.token_address in b.positions:
                b.on_tick(
                    position.token_address,
                    healthy_market(price_usd=position.entry_price_effective * 1.4),
                    T0 + timedelta(hours=i, minutes=45),
                )

        performance = evaluate_performance(b.trades, starting_equity=100_000.0)
        assert performance.n_trades == 6
        assert 0.0 <= performance.win_rate <= 1.0
        assert performance.max_drawdown_pct >= 0.0
        assert performance.total_fees_usd > 0
        assert "trades" in performance.summary()
