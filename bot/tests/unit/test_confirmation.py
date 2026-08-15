"""Effective trader count — 'five wallets, one person' must never read as five signals."""

from __future__ import annotations

import pytest

from fomo.adapters.scenarios import trader
from fomo.domain import TxAction
from fomo.scoring.confirmation import assess_confirmation, correlation_key, effective_traders


def pair(a: str, b: str, rho: float) -> dict[str, float]:
    return {correlation_key(a, b): rho}


class TestEffectiveTraders:
    def test_independent_traders_count_fully(self):
        n_eff = effective_traders([1.0, 1.0, 1.0], ["a", "b", "c"], {})
        assert n_eff == pytest.approx(3.0)

    def test_perfectly_correlated_traders_count_as_one(self):
        ids = ["a", "b", "c"]
        correlations = {correlation_key(x, y): 1.0 for i, x in enumerate(ids) for y in ids[i + 1 :]}
        assert effective_traders([1.0, 1.0, 1.0], ids, correlations) == pytest.approx(1.0)

    def test_partial_correlation_lands_between(self):
        ids = ["a", "b", "c"]
        correlations = {correlation_key(x, y): 0.5 for i, x in enumerate(ids) for y in ids[i + 1 :]}
        n_eff = effective_traders([1.0, 1.0, 1.0], ids, correlations)
        assert 1.0 < n_eff < 3.0
        assert n_eff == pytest.approx(1.5)

    def test_five_wallets_three_in_one_cluster(self):
        ids = ["a", "b", "c", "d", "e"]
        cluster = ["a", "b", "c"]
        correlations = {
            correlation_key(x, y): 0.95 for i, x in enumerate(cluster) for y in cluster[i + 1 :]
        }
        n_eff = effective_traders([1.0] * 5, ids, correlations)
        assert 2.0 < n_eff < 3.2

    def test_low_quality_traders_contribute_less(self):
        strong = effective_traders([0.9, 0.9], ["a", "b"], {})
        mixed = effective_traders([0.9, 0.1], ["a", "b"], {})
        assert strong > mixed

    def test_never_exceeds_headcount(self):
        assert effective_traders([1.0, 1.0], ["a", "b"], {}) <= 2.0

    def test_empty_and_zero_weight_inputs(self):
        assert effective_traders([], [], {}) == 0.0
        assert effective_traders([0.0, 0.0], ["a", "b"], {}) == 0.0

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError):
            effective_traders([1.0], ["a", "b"], {})


class TestAssessConfirmation:
    def test_counts_only_buyers(self):
        activities = (
            trader("a", 0.9, 10_000),
            trader("b", 0.0, 0.0, action=TxAction.CLOSE, usd_sold=5_000),
        )
        result = assess_confirmation(activities, {})
        assert result.n_traders == 1

    def test_repeat_buys_by_one_wallet_are_not_two_traders(self):
        activities = (
            trader("a", 0.9, 10_000),
            trader("a", 0.9, 5_000, action=TxAction.ADD),
        )
        assert assess_confirmation(activities, {}).n_traders == 1

    def test_holders_only_excludes_traders_who_sold(self):
        activities = (
            trader("a", 0.9, 10_000, still_holding=True),
            trader("b", 0.9, 10_000, still_holding=False),
        )
        assert assess_confirmation(activities, {}, holders_only=False).n_traders == 2
        assert assess_confirmation(activities, {}, holders_only=True).n_traders == 1

    def test_detail_calls_out_correlation(self):
        ids = ["a", "b", "c"]
        correlations = {correlation_key(x, y): 0.97 for i, x in enumerate(ids) for y in ids[i + 1 :]}
        result = assess_confirmation(tuple(trader(i, 0.8, 9_000) for i in ids), correlations)
        assert "independent" in result.detail
        assert result.n_effective < 1.5

    def test_no_activity(self):
        result = assess_confirmation((), {})
        assert result.n_traders == 0
        assert result.n_effective == 0.0
