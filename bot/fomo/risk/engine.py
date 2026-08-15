"""Risk engine (docs/04 §7).

Two things make this module different from a normal scorer:

1. **Vetoes are absorbing.** A veto sets MASTER to 0. No amount of social momentum,
   smart money or volume compensates. That is the whole point of the gated design.
2. **It fails closed.** Unknown authority state, stale data, disagreeing providers —
   all veto. The failure mode of every safety check must be AVOID, never "proceed
   without the check". This is the single most common way these systems lose money.
"""

from __future__ import annotations

from ..config import SoftThresholds, Thresholds, VetoThresholds, get_settings
from ..domain import (
    MarketSnapshot,
    RiskAssessment,
    RiskFinding,
    SecuritySnapshot,
    Severity,
)
from ..scoring.math_utils import clip


def _ramp(value: float, lo: float, hi: float, max_penalty: float) -> float:
    """Linear penalty ramp: 0 below ``lo``, ``max_penalty`` at/above ``hi``."""
    if hi <= lo or value <= lo:
        return 0.0
    return max_penalty * min((value - lo) / (hi - lo), 1.0)


def _lp_secured(sec: SecuritySnapshot, v: VetoThresholds) -> bool:
    if sec.lp_burned_pct >= v.lp_secured_min_burn_pct:
        return True
    return sec.lp_locked_seconds_remaining is not None and sec.lp_locked_seconds_remaining > 0


def _check_vetoes(
    sec: SecuritySnapshot, mkt: MarketSnapshot, v: VetoThresholds
) -> list[RiskFinding]:
    """Every rule here is non-negotiable and none of them is compensable."""
    out: list[RiskFinding] = []

    def veto(rule_id: str, detail: str, value: float | None = None, threshold: float | None = None):
        out.append(
            RiskFinding(
                rule_id=rule_id, severity=Severity.VETO, detail=detail, value=value, threshold=threshold
            )
        )

    # `is not True` rather than `is False`: unknown must veto too.
    if v.require_mint_authority_revoked and sec.mint_authority_revoked is not True:
        veto("MINT_AUTHORITY_ACTIVE", "mint authority not provably revoked — supply can be inflated")
    if v.require_freeze_authority_revoked and sec.freeze_authority_revoked is not True:
        veto("FREEZE_AUTHORITY_ACTIVE", "freeze authority not provably revoked — you may be unable to sell")

    if not _lp_secured(sec, v):
        veto(
            "LP_NOT_SECURED",
            f"LP only {sec.lp_burned_pct:.1f}% burned and not locked — liquidity can be pulled",
            sec.lp_burned_pct,
            v.lp_secured_min_burn_pct,
        )

    if sec.honeypot_sim_passed is not True:
        veto("HONEYPOT", "sell simulation did not pass — you may be able to buy but not sell")

    if sec.sell_tax_bps > v.max_sell_tax_bps:
        veto(
            "SELL_TAX_EXTREME",
            f"sell tax {sec.sell_tax_bps / 100:.1f}% — economically a honeypot",
            float(sec.sell_tax_bps),
            float(v.max_sell_tax_bps),
        )

    if sec.has_transfer_hook and not sec.transfer_hook_whitelisted and not v.allow_unknown_transfer_hook:
        veto("TRANSFER_HOOK_UNKNOWN", "unknown Token-2022 transfer hook — transfers can be blocked")

    if sec.top10_holder_pct > v.max_top10_holder_pct:
        veto(
            "HOLDER_CONCENTRATION",
            f"top-10 hold {sec.top10_holder_pct:.1f}% (excl. LP) — one wallet can drain the pool",
            sec.top10_holder_pct,
            v.max_top10_holder_pct,
        )

    if sec.creator_holding_pct > v.max_creator_holding_pct:
        veto(
            "CREATOR_HOLDING",
            f"creator holds {sec.creator_holding_pct:.1f}%",
            sec.creator_holding_pct,
            v.max_creator_holding_pct,
        )

    if mkt.liquidity_usd < v.min_liquidity_usd:
        veto(
            "LIQUIDITY_FLOOR",
            f"liquidity ${mkt.liquidity_usd:,.0f} below floor — you cannot exit",
            mkt.liquidity_usd,
            v.min_liquidity_usd,
        )

    if sec.creator_prior_rugs >= v.max_creator_prior_rugs:
        veto(
            "SERIAL_RUGGER",
            f"creator has {sec.creator_prior_rugs} prior LP-pull(s) — the best predictor of a rug is a previous rug",
            float(sec.creator_prior_rugs),
            float(v.max_creator_prior_rugs),
        )

    # Architectural vetoes: not about the market, about whether we can trust our data.
    if mkt.liquidity_usd_secondary is not None and mkt.liquidity_usd > 0:
        disagreement = 100.0 * abs(mkt.liquidity_usd_secondary - mkt.liquidity_usd) / mkt.liquidity_usd
        if disagreement > v.max_provider_disagreement_pct:
            veto(
                "DATA_DISAGREEMENT",
                f"liquidity sources disagree by {disagreement:.0f}% — we do not know which is true",
                disagreement,
                v.max_provider_disagreement_pct,
            )

    if sec.observed_age_seconds > v.max_security_staleness_seconds:
        veto(
            "STALE_DATA",
            f"security snapshot is {sec.observed_age_seconds:.0f}s old — trading on stale safety data is trading without it",
            sec.observed_age_seconds,
            float(v.max_security_staleness_seconds),
        )

    return out


def _check_soft(
    sec: SecuritySnapshot, mkt: MarketSnapshot, s: SoftThresholds
) -> tuple[list[RiskFinding], float]:
    """Graduated penalties. Everything genuinely fatal belongs in the veto list instead —
    stacking soft penalties into a de-facto veto hides the reason inside a number."""
    findings: list[RiskFinding] = []
    total = 0.0

    def add(rule_id: str, penalty: float, detail: str, value: float, threshold: float):
        nonlocal total
        if penalty <= 0:
            return
        total += penalty
        severity = Severity.HIGH if penalty >= 0.15 else Severity.MEDIUM
        findings.append(
            RiskFinding(rule_id=rule_id, severity=severity, detail=detail, value=value, threshold=threshold)
        )

    add(
        "TOP10_CONCENTRATION",
        _ramp(sec.top10_holder_pct, s.top10_pct_from, s.top10_pct_to, s.top10_penalty_max),
        f"top-10 concentration {sec.top10_holder_pct:.1f}%",
        sec.top10_holder_pct,
        s.top10_pct_from,
    )
    add(
        "BUNDLED_SUPPLY",
        _ramp(sec.bundled_supply_pct, s.bundled_pct_from, s.bundled_pct_to, s.bundled_penalty_max),
        f"{sec.bundled_supply_pct:.1f}% of supply bought in the launch block (snipers/bundlers)",
        sec.bundled_supply_pct,
        s.bundled_pct_from,
    )

    if mkt.mcap_usd > 0:
        ratio = mkt.liquidity_usd / mkt.mcap_usd
        if ratio < s.liq_mcap_ratio_min:
            penalty = s.liq_mcap_penalty_max * (1.0 - ratio / s.liq_mcap_ratio_min)
            add(
                "THIN_LIQUIDITY_RATIO",
                penalty,
                f"liquidity is only {100 * ratio:.1f}% of market cap",
                ratio,
                s.liq_mcap_ratio_min,
            )

        if mkt.fdv_usd > 0:
            fdv_ratio = mkt.fdv_usd / mkt.mcap_usd
            add(
                "FDV_OVERHANG",
                _ramp(fdv_ratio, s.fdv_mcap_ratio_max, s.fdv_mcap_ratio_max * 3, s.fdv_penalty_max),
                f"FDV is {fdv_ratio:.1f}x market cap — unlock overhang",
                fdv_ratio,
                s.fdv_mcap_ratio_max,
            )

    if mkt.liquidity_usd > 0:
        turnover = mkt.volume_1h_usd / mkt.liquidity_usd
        add(
            "WASH_SUSPICION",
            _ramp(turnover, s.vol_liq_ratio_max, s.vol_liq_ratio_max * 3, s.wash_penalty_max),
            f"volume is {turnover:.0f}x liquidity in 1h — implausible without wash trading",
            turnover,
            s.vol_liq_ratio_max,
        )

    if 0 < mkt.token_age_seconds < s.min_token_age_seconds:
        add(
            "VERY_YOUNG_TOKEN",
            s.young_penalty,
            f"token is {mkt.token_age_seconds:.0f}s old — metadata not yet reliable",
            mkt.token_age_seconds,
            float(s.min_token_age_seconds),
        )

    add(
        "BUY_TAX",
        _ramp(float(sec.buy_tax_bps), float(s.buy_tax_bps_from), float(s.buy_tax_bps_to), s.buy_tax_penalty_max),
        f"buy tax {sec.buy_tax_bps / 100:.1f}%",
        float(sec.buy_tax_bps),
        float(s.buy_tax_bps_from),
    )

    if (
        sec.lp_locked_seconds_remaining is not None
        and 0 < sec.lp_locked_seconds_remaining < s.lp_unlock_soon_seconds
        and sec.lp_burned_pct < 90.0
    ):
        add(
            "LP_UNLOCK_SOON",
            s.lp_unlock_soon_penalty,
            f"LP unlocks in {sec.lp_locked_seconds_remaining / 3600:.0f}h",
            sec.lp_locked_seconds_remaining,
            float(s.lp_unlock_soon_seconds),
        )

    return findings, clip(1.0 - total, s.floor, 1.0)


def assess_risk(
    security: SecuritySnapshot,
    market: MarketSnapshot,
    thresholds: Thresholds | None = None,
) -> RiskAssessment:
    """Full risk pass: hard vetoes first, then graduated penalties."""
    t = thresholds or get_settings().thresholds

    vetoes = _check_vetoes(security, market, t.veto)
    soft_findings, soft_multiplier = _check_soft(security, market, t.soft)

    # Human-facing 0-100 (higher = safer). Not used in the master formula: the formula
    # uses the veto flag and the multiplier, which cannot be averaged away.
    risk_score = 0.0 if vetoes else 100.0 * soft_multiplier

    return RiskAssessment(
        veto=bool(vetoes),
        findings=tuple(vetoes + soft_findings),
        soft_multiplier=soft_multiplier,
        risk_score=risk_score,
    )
