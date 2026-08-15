"""End-to-end: snapshot -> decision -> paper trade -> performance, plus backtest and alerts.

No network, no clock, no database — the decision core is pure, so "integration" here
means the modules wired together, not infrastructure.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from fomo.adapters import scenarios
from fomo.adapters.scenarios import T0, healthy_market
from fomo.alerts.format import bar, dedupe_key, render_discord, render_telegram
from fomo.backtest.harness import ReplayEvent, run, sweep, walk_forward
from fomo.cli import main as cli_main
from fomo.config import get_settings, load_settings
from fomo.domain import SignalState
from fomo.signals import evaluate
from fomo.trading.analytics import attribution
from fomo.trading.analytics import evaluate as evaluate_performance
from fomo.trading.paper import PaperBroker


class TestConfig:
    def test_config_loads_and_validates(self):
        settings = load_settings()
        assert settings.version.startswith("w")
        assert settings.thresholds.position.max_position_pct_of_equity > 0

    def test_trader_bias_keeps_average_components_at_fifty(self):
        """The documented invariant of the weight set (config/weights.toml)."""
        w = get_settings().weights.trader
        total = w.w_perf + w.w_winrate + w.w_consistency + w.w_early + w.w_exit_quality
        assert w.bias == pytest.approx(-total / 2.0, abs=0.01)


class TestFullPipeline:
    def test_entry_to_closed_trade(self):
        decision = evaluate(scenarios.happy_path())
        assert decision.state is SignalState.POTENTIAL_ENTRY

        broker = PaperBroker(starting_equity_usd=25_000.0)
        position = broker.open_position(decision, healthy_market(), T0)
        assert position is not None

        winner = healthy_market(price_usd=position.entry_price_effective * 1.9)
        broker.on_tick(decision.token_address, winner, T0 + timedelta(minutes=12))
        pullback = healthy_market(price_usd=position.entry_price_effective * 1.3)
        broker.on_tick(decision.token_address, pullback, T0 + timedelta(minutes=25))

        assert broker.positions == {}
        assert len(broker.trades) == 1
        performance = evaluate_performance(broker.trades, 25_000.0)
        assert performance.n_trades == 1
        assert performance.total_fees_usd > 0

    def test_rejected_scenarios_never_open_a_position(self):
        broker = PaperBroker()
        for name in ("honeypot", "rug_setup", "bot_swarm", "cluster_illusion", "late_entry"):
            decision = evaluate(scenarios.ALL_SCENARIOS[name]())
            assert broker.open_position(decision, healthy_market(), T0) is None, name
        assert broker.positions == {}
        assert broker.cash_usd == 10_000.0

    def test_emergency_exit_when_a_veto_appears_mid_position(self):
        """A token can become unsafe after you are in it. The position must react."""
        broker = PaperBroker()
        decision = evaluate(scenarios.happy_path())
        position = broker.open_position(decision, healthy_market(), T0)
        assert position is not None

        rugging = evaluate(
            scenarios.happy_path().model_copy(
                update={"security": scenarios.safe_security(lp_burned_pct=0.0)}
            )
        )
        assert rugging.risk.veto is True
        fills = broker.on_tick(
            decision.token_address, healthy_market(), T0 + timedelta(minutes=2), decision=rugging
        )
        assert fills and fills[0].reason.value == "EMERGENCY"


class TestBacktest:
    def _events(self, n: int = 8) -> list[ReplayEvent]:
        events: list[ReplayEvent] = []
        for i in range(n):
            base = scenarios.happy_path().model_copy(
                update={"address": f"Token{i:02d}", "symbol": f"T{i:02d}"}
            )
            start = T0 + timedelta(hours=i)
            events.append(ReplayEvent(observed_at=start, snapshot=base))
            path = [1.4, 1.9, 1.3] if i % 2 == 0 else [0.8, 0.55]
            for step, multiple in enumerate(path, start=1):
                market = healthy_market(
                    price_usd=0.00041 * multiple,
                    price_15m_ago=0.00041 * multiple,
                    ema20_1m=0.00041 * multiple,
                )
                events.append(
                    ReplayEvent(
                        observed_at=start + timedelta(minutes=15 * step),
                        snapshot=base.model_copy(update={"market": market}),
                    )
                )
        return events

    def test_backtest_produces_trades_and_metrics(self):
        result = run(self._events(), starting_equity=20_000.0)
        assert result.n_events > 0
        assert result.performance.n_trades > 0
        assert 0.0 <= result.performance.win_rate <= 1.0
        assert "trades" in result.summary()

    def test_backtest_is_deterministic(self):
        """Same data twice must give byte-identical trades, or no backtest result means
        anything (docs/05 §3)."""
        events = self._events()
        first = run(events, starting_equity=20_000.0)
        second = run(list(reversed(events)), starting_equity=20_000.0)
        assert [t.net_pnl_usd for t in first.trades] == [t.net_pnl_usd for t in second.trades]
        assert first.final_equity == second.final_equity

    def test_events_are_replayed_in_observation_order(self):
        result = run(self._events(), starting_equity=20_000.0)
        times = [d.observed_at for d in result.decisions]
        assert times == sorted(times)

    def test_sweep_and_walk_forward_run(self):
        events = self._events()
        base = get_settings()
        variants = {"default": base}
        rows = sweep(events, variants, starting_equity=20_000.0)
        assert len(rows) == 1
        windows = walk_forward(events, variants, n_windows=2, starting_equity=20_000.0)
        assert len(windows["default"]) <= 2

    def test_attribution_splits_by_score_bucket(self):
        result = run(self._events(), starting_equity=20_000.0)
        buckets = attribution(result.trades)
        assert buckets
        assert sum(p.n_trades for p in buckets.values()) == result.performance.n_trades


class TestAlerts:
    def test_telegram_message_contains_the_essentials(self):
        message = render_telegram(evaluate(scenarios.happy_path()))
        assert "$XYZ" in message
        assert "Master Score" in message
        assert "independent" in message
        assert "A bought" in message  # real amounts, not an invented split

    def test_veto_alert_leads_with_the_reason(self):
        message = render_telegram(evaluate(scenarios.honeypot()))
        assert "HARD VETO" in message
        assert "sell" in message.lower()

    def test_discord_payload_shape(self):
        payload = render_discord(evaluate(scenarios.happy_path()))
        assert payload["embeds"][0]["fields"]
        assert "XYZ" in payload["embeds"][0]["title"]

    def test_dedupe_key_is_stable_within_a_bucket(self):
        decision = evaluate(scenarios.happy_path())
        later = decision.model_copy(update={"observed_at": T0 + timedelta(minutes=5)})
        much_later = decision.model_copy(update={"observed_at": T0 + timedelta(minutes=40)})
        assert dedupe_key(decision) == dedupe_key(later)
        assert dedupe_key(decision) != dedupe_key(much_later)

    def test_bar_rendering(self):
        assert len(bar(50.0)) == 10
        assert bar(0.0) == "░" * 10
        assert bar(100.0) == "█" * 10


class TestCli:
    @pytest.mark.parametrize(
        "argv",
        [
            ["scenarios"],
            ["config"],
            ["traders"],
            ["backtest"],
            ["sweep"],
            ["explain", "happy_path"],
            ["explain", "late_entry"],
            ["alert", "happy_path"],
        ],
    )
    def test_commands_exit_zero(self, argv, capsys):
        assert cli_main(argv) == 0
        assert capsys.readouterr().out.strip()

    def test_unknown_scenario_exits_nonzero(self, capsys):
        assert cli_main(["explain", "nope"]) == 1
        assert "unknown scenario" in capsys.readouterr().out
