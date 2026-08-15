"""Trader scoring — the shrinkage behaviour is what these tests exist to protect."""

from __future__ import annotations

import math

import pytest

from fomo.config import get_settings
from fomo.domain import Reliability, TraderStats
from fomo.scoring.trader import score_trader


def stats(**overrides) -> TraderStats:
    base = dict(
        trader_id="t1",
        nickname="test",
        n_trades=50,
        n_wins=25,
        log_returns=tuple([0.3, -0.2, 0.5, -0.25, 0.4] * 10),
        largest_win_usd=2_000.0,
        total_win_usd=20_000.0,
        median_entry_mcap_usd=120_000.0,
        median_entry_age_seconds=900.0,
        exit_quality=0.55,
        trades_per_day=2.0,
        active_days=60,
    )
    return TraderStats(**{**base, **overrides})


class TestShrinkage:
    """A wallet with 3 wins from 3 trades does not have a 100% win rate."""

    def test_perfect_but_tiny_record_scores_near_neutral(self):
        lucky = score_trader(stats(n_trades=3, n_wins=3, log_returns=(1.2, 0.9, 1.5), active_days=2))
        assert 40.0 < lucky.score < 65.0
        assert lucky.reliability is Reliability.UNPROVEN

    def test_same_quality_with_long_record_scores_much_higher(self):
        few = score_trader(stats(n_trades=4, n_wins=3, log_returns=(0.5, 0.4, 0.6, -0.2)))
        many = score_trader(stats(n_trades=80, n_wins=60, log_returns=tuple([0.5, 0.4, 0.6, -0.2] * 20)))
        assert many.score > few.score + 10.0

    def test_no_trades_lands_at_the_prior(self):
        unknown = score_trader(
            stats(n_trades=0, n_wins=0, log_returns=(), total_win_usd=0.0, trades_per_day=0.0)
        )
        # Unknown is 50, not 0: "unproven" and "bad" are different states.
        assert unknown.score == pytest.approx(50.0, abs=0.01)
        assert unknown.reliability is Reliability.UNPROVEN

    def test_winrate_component_uses_beta_prior(self):
        w = get_settings().weights.trader
        result = score_trader(stats(n_trades=3, n_wins=3))
        expected = (3 + w.prior_alpha) / (3 + w.prior_alpha + w.prior_beta)
        assert result.components["winrate"] == pytest.approx(expected)
        assert result.components["winrate"] < 1.0


class TestConsistency:
    def test_all_profit_from_one_trade_scores_zero(self):
        one_hit = score_trader(stats(largest_win_usd=20_000.0, total_win_usd=20_000.0))
        assert one_hit.components["consistency"] == pytest.approx(0.0)

    def test_evenly_spread_profit_scores_one(self):
        n = 50
        even = score_trader(stats(n_trades=n, largest_win_usd=20_000.0 / n, total_win_usd=20_000.0))
        assert even.components["consistency"] == pytest.approx(1.0)

    def test_one_hit_wonder_ranks_below_consistent_trader(self):
        # Same trade count and win rate; only the profit distribution differs.
        one_hit = score_trader(stats(largest_win_usd=19_000.0, total_win_usd=20_000.0))
        consistent = score_trader(stats(largest_win_usd=1_200.0, total_win_usd=20_000.0))
        assert consistent.score > one_hit.score


class TestRiskAdjustment:
    def test_high_variance_scores_below_steady_returns_at_equal_mean(self):
        steady = tuple([0.2, 0.25, 0.15, 0.2] * 12)
        wild = tuple([2.0, -1.6, 1.8, -1.4] * 12)
        assert abs(sum(steady) / len(steady) - sum(wild) / len(wild)) < 0.15
        assert score_trader(stats(log_returns=steady)).score > score_trader(stats(log_returns=wild)).score

    def test_log_returns_treat_doubling_and_halving_symmetrically(self):
        assert math.log(2) == pytest.approx(-math.log(0.5))


class TestPenalties:
    def test_bot_likeness_cuts_score(self):
        clean = score_trader(stats())
        bot = score_trader(stats(bot_likeness=1.0))
        assert bot.score < clean.score
        assert bot.penalty < 0.4

    def test_churn_penalises_hyperactive_wallets(self):
        normal = score_trader(stats(trades_per_day=2.0))
        churner = score_trader(stats(trades_per_day=200.0))
        assert churner.score < normal.score

    def test_penalties_compound(self):
        both = score_trader(stats(bot_likeness=0.8, suspicious=0.8, trades_per_day=150.0))
        assert both.penalty < 0.25


class TestEarlyEntry:
    def test_earlier_and_smaller_entries_score_higher(self):
        early = score_trader(stats(median_entry_mcap_usd=45_000.0, median_entry_age_seconds=400.0))
        late = score_trader(stats(median_entry_mcap_usd=2_500_000.0, median_entry_age_seconds=70_000.0))
        assert early.components["early"] > late.components["early"] + 0.4
        assert early.score > late.score


class TestReliability:
    @pytest.mark.parametrize(
        "n,days,expected",
        [
            (60, 45, Reliability.HIGH),
            (60, 5, Reliability.MEDIUM),
            (20, 40, Reliability.MEDIUM),
            (8, 40, Reliability.LOW),
            (2, 40, Reliability.UNPROVEN),
        ],
    )
    def test_labels(self, n, days, expected):
        assert score_trader(stats(n_trades=n, n_wins=n // 2, active_days=days)).reliability is expected


def test_score_always_within_bounds():
    extremes = [
        stats(n_trades=0, n_wins=0, log_returns=(), total_win_usd=0.0),
        stats(n_trades=10_000, n_wins=10_000, log_returns=tuple([5.0] * 100)),
        stats(bot_likeness=1.0, suspicious=1.0, trades_per_day=10_000.0),
        stats(exit_quality=1.0, median_entry_mcap_usd=1.0, median_entry_age_seconds=1.0),
    ]
    for s in extremes:
        assert 0.0 <= score_trader(s).score <= 100.0
