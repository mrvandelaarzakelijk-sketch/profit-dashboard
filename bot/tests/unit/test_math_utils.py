"""Numeric primitives — edge cases first, because that is where scorers break."""

from __future__ import annotations

import math

import pytest

from fomo.scoring.math_utils import (
    center,
    clip,
    effective_count,
    logistic,
    logscale,
    mad,
    median,
    sat,
    shannon_entropy,
    stdev,
)


class TestSat:
    def test_half_point(self):
        assert sat(5.0, 5.0) == pytest.approx(0.5)

    def test_zero_and_negative_clamp_to_zero(self):
        assert sat(0.0, 3.0) == 0.0
        assert sat(-10.0, 3.0) == 0.0

    def test_saturates_toward_one(self):
        assert sat(1e9, 1.0) < 1.0
        assert sat(1e9, 1.0) > 0.999
        # At extreme ratios the division rounds to exactly 1.0; bounded, not strict.
        assert sat(1e18, 1e-6) <= 1.0

    def test_monotonic(self):
        values = [sat(x, 2.0) for x in range(0, 50)]
        assert values == sorted(values)

    def test_rejects_nonpositive_k(self):
        with pytest.raises(ValueError):
            sat(1.0, 0.0)


class TestLogistic:
    def test_midpoint(self):
        assert logistic(0.0) == pytest.approx(0.5)

    def test_stable_at_extremes(self):
        # A naive 1/(1+exp(-x)) overflows here; ours must not.
        assert logistic(-1000.0) == pytest.approx(0.0, abs=1e-12)
        assert logistic(1000.0) == pytest.approx(1.0, abs=1e-12)

    def test_symmetry(self):
        assert logistic(2.5) + logistic(-2.5) == pytest.approx(1.0)


class TestLogscale:
    def test_bounds(self):
        assert logscale(10.0, 10.0, 1000.0) == 0.0
        assert logscale(1000.0, 10.0, 1000.0) == 1.0
        assert logscale(5.0, 10.0, 1000.0) == 0.0
        assert logscale(5000.0, 10.0, 1000.0) == 1.0

    def test_geometric_midpoint_is_half(self):
        assert logscale(100.0, 10.0, 1000.0) == pytest.approx(0.5)

    def test_rejects_bad_range(self):
        with pytest.raises(ValueError):
            logscale(5.0, 0.0, 10.0)
        with pytest.raises(ValueError):
            logscale(5.0, 10.0, 10.0)


class TestCenter:
    def test_maps_to_signed_evidence(self):
        assert center(0.0) == -1.0
        assert center(50.0) == 0.0
        assert center(100.0) == 1.0

    def test_clips_out_of_range(self):
        assert center(150.0) == 1.0
        assert center(-20.0) == -1.0


class TestDiversity:
    """The 'is this a bot swarm' arithmetic (docs/04 §3.3)."""

    def test_entropy_of_uniform(self):
        assert shannon_entropy([1] * 10) == pytest.approx(math.log(10))

    def test_entropy_of_single_source_is_zero(self):
        assert shannon_entropy([500]) == 0.0
        assert shannon_entropy([]) == 0.0

    def test_effective_count_equals_n_when_uniform(self):
        assert effective_count([5] * 50) == pytest.approx(50.0)

    def test_bot_swarm_vs_organic(self):
        # 1000 mentions from 50 accounts vs 100 mentions from 100 accounts.
        swarm_eff = effective_count([20] * 50)
        organic_eff = effective_count([1] * 100)
        assert swarm_eff / 1000 == pytest.approx(0.05, abs=0.01)
        assert organic_eff / 100 == pytest.approx(1.0, abs=0.01)

    def test_one_spammer_dominating_collapses_effective_count(self):
        counts = [200] + [1] * 39  # 240 mentions, one account posts 200
        assert effective_count(counts) < 15


class TestRobustStats:
    def test_median_even_and_odd(self):
        assert median([3, 1, 2]) == 2
        assert median([4, 1, 2, 3]) == pytest.approx(2.5)

    def test_mad_ignores_outlier_that_ruins_stdev(self):
        values = [10.0, 11.0, 9.0, 10.5, 9.5, 500.0]
        assert mad(values) < 2.0
        assert stdev(values) > 100.0

    def test_empty_inputs_do_not_raise(self):
        assert median([]) == 0.0
        assert mad([]) == 0.0
        assert stdev([]) == 0.0


def test_clip():
    assert clip(5.0, 0.0, 1.0) == 1.0
    assert clip(-5.0, 0.0, 1.0) == 0.0
    assert clip(0.5, 0.0, 1.0) == 0.5
