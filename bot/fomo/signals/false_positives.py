"""False-positive detection (docs/04 §9).

These run alongside the scoring rather than inside it, because they describe
*patterns between* signals rather than the strength of any one signal. The two that
save the most money in practice are ``CLUSTER_ILLUSION`` and ``SOCIAL_WITHOUT_BUYERS``
— and they are exactly the two a naive implementation misses, because in raw counts
they look like the strongest signal of the day.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import FalsePositiveThresholds, get_settings
from ..domain import ConfirmationAssessment, Severity, TokenSnapshot
from ..scoring.components import social_velocity_z


@dataclass(frozen=True)
class Flag:
    flag_id: str
    severity: Severity
    detail: str


CRITICAL_FLAGS = frozenset(
    {
        "SOCIAL_WITHOUT_BUYERS",
        "CLUSTER_ILLUSION",
        "VOLUME_WITHOUT_LIQUIDITY",
        "INSIDER_DISTRIBUTION",
        "WASH_TRADING",
    }
)


def detect(
    snapshot: TokenSnapshot,
    confirmation: ConfirmationAssessment,
    thresholds: FalsePositiveThresholds | None = None,
) -> list[Flag]:
    fp = thresholds or get_settings().thresholds.false_positive
    mkt, soc = snapshot.market, snapshot.social
    flags: list[Flag] = []

    velocity_z = social_velocity_z(soc) if soc.mentions else 0.0

    # Everyone is talking, nobody is buying. Classic paid campaign or exit distribution.
    if velocity_z > fp.social_velocity_high and mkt.unique_buyers_15m < fp.unique_buyers_low_15m:
        flags.append(
            Flag(
                "SOCIAL_WITHOUT_BUYERS",
                Severity.CRITICAL,
                f"social velocity z={velocity_z:.1f} but only {mkt.unique_buyers_15m} unique buyers in 15m",
            )
        )

    # Several wallets, one person. Looks like the strongest confirmation you will see.
    if (
        confirmation.n_traders >= fp.cluster_illusion_min_wallets
        and confirmation.n_effective < fp.cluster_illusion_neff_max
    ):
        flags.append(
            Flag(
                "CLUSTER_ILLUSION",
                Severity.CRITICAL,
                f"{confirmation.n_traders} wallets collapse to {confirmation.n_effective:.2f} independent traders",
            )
        )

    if (
        confirmation.n_effective > 0
        and confirmation.n_effective < fp.weak_trader_neff_max
        and confirmation.mean_quality * 100.0 < fp.weak_trader_quality_max
    ):
        flags.append(
            Flag(
                "SINGLE_WEAK_TRADER",
                Severity.HIGH,
                f"one trader with mean quality {confirmation.mean_quality * 100:.0f}/100",
            )
        )

    if mkt.liquidity_usd > 0:
        turnover = mkt.volume_1h_usd / mkt.liquidity_usd
        if turnover > fp.volume_liquidity_ratio_max:
            flags.append(
                Flag(
                    "VOLUME_WITHOUT_LIQUIDITY",
                    Severity.CRITICAL,
                    f"1h volume is {turnover:.0f}x liquidity",
                )
            )

    # The tell that beats every other bearish signal: the people who know are selling.
    if mkt.insider_net_flow_usd < 0 and velocity_z > 0:
        flags.append(
            Flag(
                "INSIDER_DISTRIBUTION",
                Severity.CRITICAL,
                f"insiders net -${abs(mkt.insider_net_flow_usd):,.0f} while social is rising",
            )
        )

    if mkt.same_wallet_volume_share > fp.wash_trade_share_max:
        flags.append(
            Flag(
                "WASH_TRADING",
                Severity.CRITICAL,
                f"{100 * mkt.same_wallet_volume_share:.0f}% of volume is between the same wallets",
            )
        )

    if soc.duplicate_text_share > fp.duplicate_text_share_max:
        flags.append(
            Flag(
                "COORDINATED_SHILL",
                Severity.HIGH,
                f"{100 * soc.duplicate_text_share:.0f}% of mentions are near-identical text",
            )
        )

    if soc.mentions > 0 and soc.new_account_share > fp.new_account_share_max:
        flags.append(
            Flag(
                "NEW_ACCOUNT_SWARM",
                Severity.HIGH,
                f"{100 * soc.new_account_share:.0f}% of mentions from accounts < 30d old",
            )
        )

    # Social peaking while price does not follow is usually distribution into the noise.
    if velocity_z > fp.social_velocity_high and mkt.price_15m_ago and mkt.price_15m_ago > 0:
        if mkt.price_usd <= mkt.price_15m_ago:
            flags.append(
                Flag(
                    "PRICE_SOCIAL_DIVERGENCE",
                    Severity.MEDIUM,
                    "social is spiking while price is flat or falling",
                )
            )

    if soc.scam_warning_count > 0:
        flags.append(
            Flag(
                "SCAM_WARNINGS",
                Severity.HIGH,
                f"{soc.scam_warning_count} scam/liquidity warning post(s) detected",
            )
        )

    return flags


def has_critical(flags: list[Flag]) -> bool:
    return any(f.flag_id in CRITICAL_FLAGS for f in flags)
