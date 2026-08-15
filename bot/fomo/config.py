"""Configuration loading and validation.

Every tunable number in the system lives in ``config/*.toml`` and is validated here.
The rule the rest of the codebase relies on: **no magic numbers outside this module's
models**. That is what makes parameter sweeps (docs/05 §2) possible without touching code.

``extra="forbid"`` is deliberate. A typo in a TOML key would otherwise be silently
ignored and you would spend an afternoon wondering why a weight change did nothing.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ── weights.toml ─────────────────────────────────────────────────────────────


class TraderWeights(_Model):
    bias: float
    w_perf: float
    w_winrate: float
    w_consistency: float
    w_early: float
    w_exit_quality: float
    k_ir: float
    prior_alpha: float = Field(gt=0)
    prior_beta: float = Field(gt=0)
    shrink_k: float = Field(gt=0)
    penalty_bot: float
    penalty_churn: float
    penalty_suspicious: float
    churn_k: float = Field(gt=0)
    entry_mcap_lo: float = Field(gt=0)
    entry_mcap_hi: float = Field(gt=0)
    entry_age_lo: float = Field(gt=0)
    entry_age_hi: float = Field(gt=0)
    reliability_high_n: int
    reliability_high_days: int
    reliability_medium_n: int
    reliability_low_n: int


class SmartMoneyWeights(_Model):
    bias: float
    c_neff: float
    c_quality: float
    c_usd: float
    c_conviction: float
    c_early: float
    c_sell: float
    k_neff: float = Field(gt=0)
    k_usd: float = Field(gt=0)
    k_conviction: float = Field(gt=0)
    age_lo: float = Field(gt=0)
    age_hi: float = Field(gt=0)


class SocialWeights(_Model):
    bias: float
    a_velocity: float
    a_diversity: float
    a_accounts: float
    a_engagement: float
    a_sentiment: float
    k_velocity: float = Field(gt=0)
    k_accounts: float = Field(gt=0)


class MarketWeights(_Model):
    bias: float
    m_volume: float
    m_buyer_ratio: float
    m_buyer_growth: float
    m_holder_growth: float
    k_volume: float = Field(gt=0)
    k_buyer_growth: float = Field(gt=0)
    k_holder_growth: float = Field(gt=0)


class MasterWeights(_Model):
    bias: float
    k_smart: float
    k_social: float
    k_market: float
    k_liquidity: float
    k_confirmation: float
    k_neff: float = Field(gt=0)


class TimingWeights(_Model):
    k_smart_multiple: float = Field(gt=0)
    k_runup_15m: float = Field(gt=0)
    k_parabolic: float = Field(gt=0)
    floor: float = Field(ge=0, le=1)


class Weights(_Model):
    version: str
    trader: TraderWeights
    smart_money: SmartMoneyWeights
    social: SocialWeights
    market: MarketWeights
    master: MasterWeights
    timing: TimingWeights


# ── thresholds.toml ──────────────────────────────────────────────────────────


class VetoThresholds(_Model):
    require_mint_authority_revoked: bool
    require_freeze_authority_revoked: bool
    lp_secured_min_burn_pct: float
    max_sell_tax_bps: int
    allow_unknown_transfer_hook: bool
    max_top10_holder_pct: float
    max_creator_holding_pct: float
    min_liquidity_usd: float
    max_creator_prior_rugs: int
    max_provider_disagreement_pct: float
    max_security_staleness_seconds: int


class SoftThresholds(_Model):
    floor: float
    top10_pct_from: float
    top10_pct_to: float
    top10_penalty_max: float
    bundled_pct_from: float
    bundled_pct_to: float
    bundled_penalty_max: float
    liq_mcap_ratio_min: float
    liq_mcap_penalty_max: float
    fdv_mcap_ratio_max: float
    fdv_penalty_max: float
    vol_liq_ratio_max: float
    wash_penalty_max: float
    min_token_age_seconds: int
    young_penalty: float
    buy_tax_bps_from: int
    buy_tax_bps_to: int
    buy_tax_penalty_max: float
    lp_unlock_soon_seconds: int
    lp_unlock_soon_penalty: float


class LiquidityThresholds(_Model):
    max_roundtrip_cost_pct: float = Field(gt=0)
    dex_fee_pct: float
    priority_fee_usd: float
    liq_floor_usd: float = Field(gt=0)
    liq_good_usd: float = Field(gt=0)


class AvoidRule(_Model):
    master_below: float


class WatchRule(_Model):
    master_min: float


class EarlyWatchRule(_Model):
    master_min: float
    neff_min: float
    timing_min: float
    liquidity_min: float
    max_token_age_seconds: int


class PotentialEntryRule(_Model):
    master_min: float
    neff_min: float
    timing_min: float
    liquidity_min: float
    market_min: float
    soft_risk_min: float


class StrongEntryRule(_Model):
    master_min: float
    neff_min: float
    timing_min: float
    liquidity_min: float
    market_min: float
    soft_risk_min: float
    social_min: float


class SignalThresholds(_Model):
    avoid: AvoidRule
    watch: WatchRule
    early_watch: EarlyWatchRule
    potential_entry: PotentialEntryRule
    strong_entry: StrongEntryRule


class FalsePositiveThresholds(_Model):
    social_velocity_high: float
    unique_buyers_low_15m: int
    weak_trader_neff_max: float
    weak_trader_quality_max: float
    cluster_illusion_min_wallets: int
    cluster_illusion_neff_max: float
    volume_liquidity_ratio_max: float
    duplicate_text_share_max: float
    wash_trade_share_max: float
    new_account_share_max: float
    provider_disagreement_pct: float


class PositionThresholds(_Model):
    max_position_pct_of_equity: float = Field(gt=0, le=100)
    max_concurrent_positions: int = Field(gt=0)
    max_daily_loss_pct: float = Field(gt=0)


class ExitThresholds(_Model):
    stop_loss_pct: float = Field(gt=0, lt=100)
    take_profit_levels: list[float]
    take_profit_fractions: list[float]
    trailing_pct: float = Field(gt=0, lt=100)
    trailing_tight_pct: float = Field(gt=0, lt=100)
    trailing_tighten_at_pct: float
    max_hold_seconds: int = Field(gt=0)
    invalidate_master_below: float
    invalidate_neff_below: float
    invalidate_social_drop_pct: float
    smart_exit_share: float
    emergency_liquidity_drop_pct: float


class Thresholds(_Model):
    version: str
    veto: VetoThresholds
    soft: SoftThresholds
    liquidity: LiquidityThresholds
    signal: SignalThresholds
    false_positive: FalsePositiveThresholds
    position: PositionThresholds
    exit: ExitThresholds


class Settings(_Model):
    """The full tunable configuration of the decision core."""

    weights: Weights
    thresholds: Thresholds

    @property
    def version(self) -> str:
        return f"w{self.weights.version}-t{self.thresholds.version}"


def _load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_settings(config_dir: Path | None = None) -> Settings:
    """Load and validate configuration. Raises on any unknown or missing key."""
    directory = config_dir or CONFIG_DIR
    return Settings(
        weights=Weights(**_load_toml(directory / "weights.toml")),
        thresholds=Thresholds(**_load_toml(directory / "thresholds.toml")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached default configuration, for code paths that do not inject their own."""
    return load_settings()
