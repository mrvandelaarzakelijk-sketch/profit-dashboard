"""Risk engine — every veto tested at the boundary, plus the fail-closed behaviour.

The fail-closed tests are the important ones. A safety check whose failure mode is
"proceed anyway" is worse than no check, because it creates false confidence.
"""

from __future__ import annotations

import pytest

from fomo.adapters.scenarios import healthy_market, safe_security
from fomo.config import get_settings
from fomo.risk import assess_risk
from fomo.domain import Severity


def veto_ids(security=None, market=None) -> set[str]:
    result = assess_risk(security or safe_security(), market or healthy_market())
    return set(result.veto_reasons)


class TestBaseline:
    def test_safe_token_has_no_veto(self):
        result = assess_risk(safe_security(), healthy_market())
        assert result.veto is False
        assert result.veto_reasons == ()
        assert result.soft_multiplier > 0.9

    def test_risk_score_is_zero_when_vetoed(self):
        result = assess_risk(safe_security(honeypot_sim_passed=False), healthy_market())
        assert result.risk_score == 0.0


class TestFailClosed:
    """Unknown must never be treated as safe."""

    def test_unknown_mint_authority_vetoes(self):
        assert "MINT_AUTHORITY_ACTIVE" in veto_ids(safe_security(mint_authority_revoked=None))

    def test_unknown_freeze_authority_vetoes(self):
        assert "FREEZE_AUTHORITY_ACTIVE" in veto_ids(safe_security(freeze_authority_revoked=None))

    def test_unknown_honeypot_result_vetoes(self):
        assert "HONEYPOT" in veto_ids(safe_security(honeypot_sim_passed=None))

    def test_stale_security_data_vetoes(self):
        stale = get_settings().thresholds.veto.max_security_staleness_seconds + 1
        assert "STALE_DATA" in veto_ids(safe_security(observed_age_seconds=stale))

    def test_provider_disagreement_vetoes(self):
        market = healthy_market(liquidity_usd=300_000.0, liquidity_usd_secondary=150_000.0)
        assert "DATA_DISAGREEMENT" in veto_ids(market=market)

    def test_small_provider_disagreement_is_tolerated(self):
        market = healthy_market(liquidity_usd=300_000.0, liquidity_usd_secondary=290_000.0)
        assert "DATA_DISAGREEMENT" not in veto_ids(market=market)


class TestHardVetoes:
    def test_active_mint_authority(self):
        assert "MINT_AUTHORITY_ACTIVE" in veto_ids(safe_security(mint_authority_revoked=False))

    def test_active_freeze_authority(self):
        assert "FREEZE_AUTHORITY_ACTIVE" in veto_ids(safe_security(freeze_authority_revoked=False))

    def test_unsecured_lp(self):
        assert "LP_NOT_SECURED" in veto_ids(safe_security(lp_burned_pct=10.0))

    def test_locked_lp_counts_as_secured(self):
        secured = safe_security(lp_burned_pct=0.0, lp_locked_seconds_remaining=90 * 86400)
        assert "LP_NOT_SECURED" not in veto_ids(secured)

    def test_extreme_sell_tax(self):
        assert "SELL_TAX_EXTREME" in veto_ids(safe_security(sell_tax_bps=2500))

    def test_unknown_transfer_hook(self):
        assert "TRANSFER_HOOK_UNKNOWN" in veto_ids(safe_security(has_transfer_hook=True))

    def test_whitelisted_transfer_hook_is_allowed(self):
        ok = safe_security(has_transfer_hook=True, transfer_hook_whitelisted=True)
        assert "TRANSFER_HOOK_UNKNOWN" not in veto_ids(ok)

    def test_holder_concentration(self):
        assert "HOLDER_CONCENTRATION" in veto_ids(safe_security(top10_holder_pct=60.0))

    def test_creator_holding(self):
        assert "CREATOR_HOLDING" in veto_ids(safe_security(creator_holding_pct=25.0))

    def test_liquidity_floor(self):
        assert "LIQUIDITY_FLOOR" in veto_ids(market=healthy_market(liquidity_usd=5_000.0))

    def test_serial_rugger(self):
        assert "SERIAL_RUGGER" in veto_ids(safe_security(creator_prior_rugs=2))


class TestBoundaries:
    """Just under and just over each threshold."""

    def test_top10_holder_boundary(self):
        limit = get_settings().thresholds.veto.max_top10_holder_pct
        assert "HOLDER_CONCENTRATION" not in veto_ids(safe_security(top10_holder_pct=limit - 0.1))
        assert "HOLDER_CONCENTRATION" in veto_ids(safe_security(top10_holder_pct=limit + 0.1))

    def test_liquidity_floor_boundary(self):
        limit = get_settings().thresholds.veto.min_liquidity_usd
        assert "LIQUIDITY_FLOOR" not in veto_ids(market=healthy_market(liquidity_usd=limit))
        assert "LIQUIDITY_FLOOR" in veto_ids(market=healthy_market(liquidity_usd=limit - 1))

    def test_sell_tax_boundary(self):
        limit = get_settings().thresholds.veto.max_sell_tax_bps
        assert "SELL_TAX_EXTREME" not in veto_ids(safe_security(sell_tax_bps=limit))
        assert "SELL_TAX_EXTREME" in veto_ids(safe_security(sell_tax_bps=limit + 1))


class TestSoftPenalties:
    def test_concentration_ramps_gradually(self):
        clean = assess_risk(safe_security(top10_holder_pct=20.0), healthy_market())
        medium = assess_risk(safe_security(top10_holder_pct=35.0), healthy_market())
        heavy = assess_risk(safe_security(top10_holder_pct=44.0), healthy_market())
        assert clean.soft_multiplier > medium.soft_multiplier > heavy.soft_multiplier
        assert not any(r.veto for r in (clean, medium, heavy))

    def test_wash_trading_volume_is_penalised(self):
        washy = healthy_market(volume_1h_usd=312_000.0 * 60)  # 60x liquidity in one hour
        result = assess_risk(safe_security(), washy)
        assert result.soft_multiplier < 0.9
        assert "WASH_SUSPICION" in {f.rule_id for f in result.findings}

    def test_very_young_token_is_penalised(self):
        result = assess_risk(safe_security(), healthy_market(token_age_seconds=60.0))
        assert "VERY_YOUNG_TOKEN" in {f.rule_id for f in result.findings}

    def test_bundled_supply_is_penalised(self):
        result = assess_risk(safe_security(bundled_supply_pct=30.0), healthy_market())
        assert "BUNDLED_SUPPLY" in {f.rule_id for f in result.findings}

    def test_soft_multiplier_never_falls_below_floor(self):
        awful = safe_security(top10_holder_pct=44.9, bundled_supply_pct=34.9, buy_tax_bps=490)
        market = healthy_market(volume_1h_usd=312_000.0 * 100, mcap_usd=50_000_000.0, fdv_usd=500_000_000.0)
        result = assess_risk(awful, market)
        floor = get_settings().thresholds.soft.floor
        assert result.soft_multiplier == pytest.approx(floor)

    def test_soft_penalties_never_produce_a_veto(self):
        """Anything genuinely fatal must be a veto rule, not accumulated soft penalties."""
        awful = safe_security(top10_holder_pct=44.9, bundled_supply_pct=34.9, buy_tax_bps=490)
        result = assess_risk(awful, healthy_market(volume_1h_usd=312_000.0 * 100))
        assert result.veto is False


def test_findings_carry_readable_detail():
    result = assess_risk(safe_security(mint_authority_revoked=False), healthy_market())
    finding = next(f for f in result.findings if f.rule_id == "MINT_AUTHORITY_ACTIVE")
    assert finding.severity is Severity.VETO
    assert "mint authority" in finding.detail.lower()
