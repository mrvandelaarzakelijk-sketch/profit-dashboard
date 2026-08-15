"""Master score and signal decision (docs/04 §5.5 and §8).

    MASTER = 100 * Veto * R_soft * Timing * sigma(Evidence)

Pure function: no I/O, no clock, no database. Same code path in live paper trading and
in the backtest, which is the only way the two can be compared honestly (docs/05 §2).
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..domain import (
    SignalDecision,
    SignalState,
    TokenSnapshot,
)
from ..risk.engine import assess_risk
from ..scoring.components import (
    score_liquidity,
    score_market,
    score_smart_money,
    score_social,
)
from ..scoring.confirmation import BUY_ACTIONS, assess_confirmation
from ..scoring.math_utils import center, clip, logistic, sat
from . import false_positives
from .timing import assess_timing


def evaluate(snapshot: TokenSnapshot, settings: Settings | None = None) -> SignalDecision:
    cfg = settings or get_settings()
    w = cfg.weights.master
    t = cfg.thresholds.signal

    # ── evidence ────────────────────────────────────────────────────────────
    confirmation = assess_confirmation(snapshot.activities, snapshot.correlations)
    smart, f_smart = score_smart_money(snapshot, confirmation, cfg.weights.smart_money)
    social, f_social = score_social(snapshot.social, cfg.weights.social)
    market, f_market = score_market(snapshot.market, cfg.weights.market)
    liquidity, f_liq = score_liquidity(
        snapshot.market, snapshot.intended_position_usd, cfg.thresholds.liquidity
    )

    # ── gates ───────────────────────────────────────────────────────────────
    risk = assess_risk(snapshot.security, snapshot.market, cfg.thresholds)
    timing = assess_timing(snapshot.market, snapshot.first_smart_entry_price, cfg.weights.timing)
    flags = false_positives.detect(snapshot, confirmation, cfg.thresholds.false_positive)

    evidence = (
        w.bias
        + w.k_smart * center(smart)
        + w.k_social * center(social)
        + w.k_market * center(market)
        + w.k_liquidity * center(liquidity)
        + w.k_confirmation * sat(confirmation.n_effective - 1.0, w.k_neff)
    )
    conviction = logistic(evidence)

    veto_factor = 0.0 if risk.veto else 1.0
    master = clip(
        100.0 * veto_factor * risk.soft_multiplier * timing.multiplier * conviction, 0.0, 100.0
    )

    critical = false_positives.has_critical(flags)
    state = _decide_state(
        master=master,
        confirmation_neff=confirmation.n_effective,
        timing_multiplier=timing.multiplier,
        liquidity=liquidity,
        market=market,
        social=social,
        soft_risk=risk.soft_multiplier,
        token_age_seconds=snapshot.market.token_age_seconds,
        veto=risk.veto,
        critical_flag=critical,
        t=t,
        avoid_below=cfg.thresholds.signal.avoid.master_below,
    )

    features: dict[str, float] = {
        **f_smart,
        **f_social,
        **f_market,
        **f_liq,
        "score_smart": smart,
        "score_social": social,
        "score_market": market,
        "score_liquidity": liquidity,
        "n_traders": float(confirmation.n_traders),
        "n_effective": confirmation.n_effective,
        "mean_trader_quality": confirmation.mean_quality,
        "risk_soft_multiplier": risk.soft_multiplier,
        "risk_veto": float(risk.veto),
        "timing_multiplier": timing.multiplier,
        "timing_smart_multiple": timing.smart_multiple,
        "timing_runup_15m": timing.runup_15m,
        "timing_parabolic": timing.parabolic_extension,
        "evidence": evidence,
        "conviction": conviction,
        "master": master,
        "token_age_seconds": snapshot.market.token_age_seconds,
    }

    return SignalDecision(
        token_address=snapshot.address,
        symbol=snapshot.symbol,
        observed_at=snapshot.observed_at,
        state=state,
        master_score=master,
        smart_money_score=smart,
        social_score=social,
        market_score=market,
        liquidity_score=liquidity,
        risk=risk,
        timing=timing,
        confirmation=confirmation,
        flags=tuple(f.flag_id for f in flags),
        contributors=_contributors(snapshot),
        features=features,
        explanation=build_explanation(
            snapshot, state, master, smart, social, market, liquidity, risk, timing, confirmation, flags
        ),
        config_version=cfg.version,
    )


def _contributors(snapshot: TokenSnapshot) -> tuple[tuple[str, float], ...]:
    """USD bought per confirming trader, largest first. Repeat buys are summed."""
    totals: dict[str, float] = {}
    for activity in snapshot.activities:
        if activity.action in BUY_ACTIONS and activity.usd_bought > 0:
            totals[activity.trader_id] = totals.get(activity.trader_id, 0.0) + activity.usd_bought
    return tuple(sorted(totals.items(), key=lambda item: item[1], reverse=True))


def _decide_state(
    *,
    master: float,
    confirmation_neff: float,
    timing_multiplier: float,
    liquidity: float,
    market: float,
    social: float,
    soft_risk: float,
    token_age_seconds: float,
    veto: bool,
    critical_flag: bool,
    t,
    avoid_below: float,
) -> SignalState:
    """Each state needs the score **and** every structural condition.

    A high score on its own is never enough — that is how you end up with a 95 on a
    token with $8k of liquidity.
    """
    if veto or critical_flag or master < avoid_below:
        return SignalState.AVOID

    s = t.strong_entry
    if (
        master >= s.master_min
        and confirmation_neff >= s.neff_min
        and timing_multiplier >= s.timing_min
        and liquidity >= s.liquidity_min
        and market >= s.market_min
        and soft_risk >= s.soft_risk_min
        and social >= s.social_min
    ):
        return SignalState.STRONG_ENTRY

    p = t.potential_entry
    if (
        master >= p.master_min
        and confirmation_neff >= p.neff_min
        and timing_multiplier >= p.timing_min
        and liquidity >= p.liquidity_min
        and market >= p.market_min
        and soft_risk >= p.soft_risk_min
    ):
        return SignalState.POTENTIAL_ENTRY

    # EARLY_WATCH intentionally has no social requirement: it is the phase-1 verdict of
    # the two-phase pipeline (docs/01 §4), emitted before social data has arrived.
    e = t.early_watch
    if (
        master >= e.master_min
        and confirmation_neff >= e.neff_min
        and timing_multiplier >= e.timing_min
        and liquidity >= e.liquidity_min
        and token_age_seconds <= e.max_token_age_seconds
    ):
        return SignalState.EARLY_WATCH

    if master >= t.watch.master_min:
        return SignalState.WATCH

    return SignalState.AVOID


def build_explanation(
    snapshot, state, master, smart, social, market, liquidity, risk, timing, confirmation, flags
) -> str:
    """Human-readable reason. Deliberately states the *binding constraint*, not a summary."""
    if risk.veto:
        reasons = ", ".join(f.detail for f in risk.findings if f.severity.value == "veto")
        return f"AVOID — hard veto: {reasons}"

    critical = [f.detail for f in flags if f.flag_id in false_positives.CRITICAL_FLAGS]
    if critical:
        return f"AVOID — false positive detected: {'; '.join(critical)}"

    parts = [
        f"{confirmation.detail}; smart money {smart:.0f}, social {social:.0f}, "
        f"market {market:.0f}, liquidity {liquidity:.0f}."
    ]

    if timing.multiplier < 0.7:
        parts.append(
            f"Entry is late: price is {timing.smart_multiple:.1f}x the first smart-money entry "
            f"(binding factor: {timing.binding_factor}), score cut by "
            f"{100 * (1 - timing.multiplier):.0f}%."
        )
    if risk.soft_multiplier < 0.9:
        worst = max(
            (f for f in risk.findings if f.severity.value != "veto"),
            key=lambda f: f.value or 0.0,
            default=None,
        )
        if worst:
            parts.append(f"Risk discount {100 * (1 - risk.soft_multiplier):.0f}% ({worst.detail}).")

    parts.append(f"MASTER {master:.1f} -> {state.value}.")
    return " ".join(parts)
