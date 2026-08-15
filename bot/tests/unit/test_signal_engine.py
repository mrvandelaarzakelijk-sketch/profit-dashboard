"""Signal engine: master score composition, state transitions, anti-FOMO."""

from __future__ import annotations

import pytest

from fomo.adapters import scenarios
from fomo.adapters.scenarios import healthy_market, safe_security, trader
from fomo.domain import SignalState
from fomo.signals import evaluate
from fomo.signals.timing import assess_timing


class TestGating:
    """A veto must be absorbing: no amount of good news survives it."""

    @pytest.mark.parametrize(
        "name",
        ["honeypot", "mint_authority_live", "rug_setup", "thin_liquidity", "data_disagreement"],
    )
    def test_vetoed_scenarios_score_zero_and_avoid(self, name):
        decision = evaluate(scenarios.ALL_SCENARIOS[name]())
        assert decision.master_score == 0.0
        assert decision.state is SignalState.AVOID
        assert decision.risk.veto is True

    def test_perfect_evidence_cannot_beat_a_veto(self):
        snapshot = scenarios.build(
            security=safe_security(mint_authority_revoked=False),
            activities=tuple(
                trader(str(i), 0.99, 100_000, pct_of_wallet=0.3, age_at_entry=300) for i in "ABCDE"
            ),
        )
        decision = evaluate(snapshot)
        assert decision.smart_money_score > 90.0
        assert decision.master_score == 0.0
        assert decision.state is SignalState.AVOID

    def test_veto_reason_appears_in_the_explanation(self):
        decision = evaluate(scenarios.honeypot())
        assert "veto" in decision.explanation.lower()


class TestAntiFomo:
    """The system is named after FOMO so that it does not trade on it."""

    def test_late_entry_is_demoted_despite_identical_evidence(self):
        early = evaluate(scenarios.happy_path())
        late = evaluate(scenarios.late_entry())
        assert early.smart_money_score == pytest.approx(late.smart_money_score)
        assert late.master_score < early.master_score * 0.5
        assert early.state is SignalState.POTENTIAL_ENTRY
        assert late.state is SignalState.AVOID

    def test_master_score_is_non_increasing_in_price_run_up(self):
        base = scenarios.happy_path()
        scores = []
        for multiple in (1.0, 1.5, 2.0, 3.0, 5.0, 10.0):
            market = healthy_market(price_usd=0.00041 * multiple)
            scores.append(evaluate(base.model_copy(update={"market": market})).master_score)
        assert scores == sorted(scores, reverse=True)

    def test_timing_multiplier_is_floored_not_zeroed(self):
        market = healthy_market(price_usd=0.00041 * 1000)
        timing = assess_timing(market, first_smart_entry_price=0.00041)
        assert timing.multiplier == pytest.approx(0.15)

    def test_missing_price_history_does_not_penalise(self):
        market = healthy_market(price_15m_ago=None, ema20_1m=None)
        timing = assess_timing(market, first_smart_entry_price=None)
        assert timing.multiplier == 1.0

    def test_binding_factor_is_reported(self):
        market = healthy_market(price_usd=0.00041, price_15m_ago=0.00015, ema20_1m=0.00040)
        timing = assess_timing(market, first_smart_entry_price=0.00041)
        assert timing.binding_factor == "runup_15m"


class TestFalsePositives:
    def test_cluster_illusion_forces_avoid_despite_high_score(self):
        decision = evaluate(scenarios.cluster_illusion())
        assert "CLUSTER_ILLUSION" in decision.flags
        assert decision.state is SignalState.AVOID
        assert decision.confirmation.n_traders == 5
        assert decision.confirmation.n_effective < 1.5

    def test_bot_swarm_is_rejected(self):
        decision = evaluate(scenarios.bot_swarm())
        assert "SOCIAL_WITHOUT_BUYERS" in decision.flags
        assert decision.state is SignalState.AVOID

    def test_insider_distribution_forces_avoid(self):
        decision = evaluate(scenarios.insider_distribution())
        assert "INSIDER_DISTRIBUTION" in decision.flags
        assert decision.state is SignalState.AVOID

    def test_thousand_bot_tweets_score_below_hundred_real_ones(self):
        botty = evaluate(scenarios.build(social=scenarios.bot_swarm_social()))
        organic = evaluate(scenarios.build(social=scenarios.organic_social()))
        assert organic.social_score > botty.social_score


class TestStateTransitions:
    def test_more_independent_traders_raise_the_state(self):
        one = evaluate(scenarios.build(activities=(trader("A", 0.9, 42_000),)))
        three = evaluate(scenarios.happy_path())
        assert three.master_score > one.master_score
        assert three.confirmation.n_effective > one.confirmation.n_effective

    def test_structural_conditions_can_block_a_high_score(self):
        """A high score alone must never be enough — that is how you buy an $8k pool."""
        snapshot = scenarios.build(
            market=healthy_market(liquidity_usd=20_000.0, liquidity_usd_secondary=20_000.0),
            activities=tuple(trader(i, 0.95, 40_000, pct_of_wallet=0.2) for i in "ABC"),
            intended_position_usd=5_000.0,
        )
        decision = evaluate(snapshot)
        assert decision.liquidity_score < 60.0
        assert decision.state not in (SignalState.POTENTIAL_ENTRY, SignalState.STRONG_ENTRY)

    def test_no_activity_yields_low_state(self):
        decision = evaluate(scenarios.build(activities=()))
        assert decision.state in (SignalState.AVOID, SignalState.WATCH)


class TestOutputContract:
    def test_all_scores_within_bounds_across_scenarios(self):
        for name, build in scenarios.ALL_SCENARIOS.items():
            d = evaluate(build())
            for field in (
                d.master_score,
                d.smart_money_score,
                d.social_score,
                d.market_score,
                d.liquidity_score,
            ):
                assert 0.0 <= field <= 100.0, name

    def test_feature_vector_is_populated(self):
        """This dict is the phase-10 training set; an empty one is a silent data loss."""
        decision = evaluate(scenarios.happy_path())
        for key in ("score_smart", "n_effective", "timing_multiplier", "risk_soft_multiplier", "master"):
            assert key in decision.features

    def test_decision_records_config_version(self):
        assert evaluate(scenarios.happy_path()).config_version.startswith("w")

    def test_explanation_is_human_readable(self):
        decision = evaluate(scenarios.late_entry())
        assert "late" in decision.explanation.lower()
        assert "x the first smart-money entry" in decision.explanation
