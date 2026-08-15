"""Domain models — the vocabulary the whole system shares.

These are the *only* types that cross module boundaries. Provider payloads are
translated into these in ``adapters/``; nothing downstream ever sees a provider shape
(docs/01 §1.A).

Everything here is frozen. The decision core is a pure function of its inputs, and
immutable inputs are how that stays true under refactoring.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── Enums ────────────────────────────────────────────────────────────────────


class TxAction(StrEnum):
    """Transaction classification (docs/01 §2). Only OPEN/ADD/TRIM/CLOSE carry signal."""

    OPEN = "OPEN"
    ADD = "ADD"
    TRIM = "TRIM"
    CLOSE = "CLOSE"
    SELF_TRANSFER = "SELF_TRANSFER"
    MM_BOT = "MM_BOT"
    AIRDROP_DUST = "AIRDROP_DUST"


class Reliability(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNPROVEN = "UNPROVEN"


class SignalState(StrEnum):
    AVOID = "AVOID"
    WATCH = "WATCH"
    EARLY_WATCH = "EARLY_WATCH"
    POTENTIAL_ENTRY = "POTENTIAL_ENTRY"
    STRONG_ENTRY = "STRONG_ENTRY"
    EXIT = "EXIT"


class Severity(StrEnum):
    VETO = "veto"
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExitReason(StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING = "TRAILING"
    SIGNAL_INVALIDATED = "SIGNAL_INVALIDATED"
    SMART_MONEY_EXIT = "SMART_MONEY_EXIT"
    EMERGENCY = "EMERGENCY"
    TIMEOUT = "TIMEOUT"


class TweetIntent(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class TweetCategory(StrEnum):
    NEWS = "news"
    LAUNCH = "launch"
    PARTNERSHIP = "partnership"
    LISTING = "listing"
    MEME_HYPE = "meme_hype"
    TECHNICAL_ANALYSIS = "technical_analysis"
    TRADE_SIGNAL = "trade_signal"
    SHILL = "shill"
    SCAM_WARNING = "scam_warning"
    LIQUIDITY_WARNING = "liquidity_warning"


# ── Trader side ──────────────────────────────────────────────────────────────


class TraderStats(_Frozen):
    """Backward-looking statistics for one trader over one window (docs/02 `trader_stats`)."""

    trader_id: str
    nickname: str = ""
    n_trades: int = Field(ge=0)
    n_wins: int = Field(ge=0)
    log_returns: tuple[float, ...] = ()
    """Per-trade ln(exit/entry). Empty is allowed: the score falls back to the prior."""
    largest_win_usd: float = 0.0
    total_win_usd: float = 0.0
    median_entry_mcap_usd: float = 0.0
    median_entry_age_seconds: float = 0.0
    exit_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    trades_per_day: float = 0.0
    bot_likeness: float = Field(default=0.0, ge=0.0, le=1.0)
    suspicious: float = Field(default=0.0, ge=0.0, le=1.0)
    active_days: int = 0


class TraderScore(_Frozen):
    trader_id: str
    nickname: str
    score: float = Field(ge=0.0, le=100.0)
    reliability: Reliability
    components: dict[str, float]
    penalty: float
    weights_version: str


class TraderActivity(_Frozen):
    """What one trader did on one token, in the current evaluation window."""

    trader_id: str
    wallet_address: str
    quality: float = Field(ge=0.0, le=1.0)
    """Trader score / 100 — the weight this trader carries as evidence."""
    action: TxAction
    usd_bought: float = 0.0
    usd_sold: float = 0.0
    pct_of_wallet: float = Field(default=0.0, ge=0.0, le=1.0)
    token_age_at_entry_seconds: float = 0.0
    price_at_entry: float = 0.0
    still_holding: bool = True
    event_at: datetime | None = None


# ── Token side ───────────────────────────────────────────────────────────────


class SecuritySnapshot(_Frozen):
    """On-chain safety facts (docs/02 `token_security`).

    Note the ``None`` defaults on the authority fields: unknown is *not* the same as
    safe, and the risk engine treats it as a veto (docs/04 §7.1, fail-closed).
    """

    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    lp_burned_pct: float = 0.0
    lp_locked_seconds_remaining: float | None = None
    top10_holder_pct: float = 100.0
    creator_holding_pct: float = 100.0
    bundled_supply_pct: float = 0.0
    sniper_wallet_count: int = 0
    buy_tax_bps: int = 0
    sell_tax_bps: int = 0
    has_transfer_hook: bool = False
    transfer_hook_whitelisted: bool = False
    honeypot_sim_passed: bool | None = None
    creator_prior_rugs: int = 0
    observed_age_seconds: float = 0.0
    """How stale this snapshot is. Old safety data is unusable safety data."""


class MarketSnapshot(_Frozen):
    price_usd: float = Field(gt=0)
    mcap_usd: float = 0.0
    fdv_usd: float = 0.0
    liquidity_usd: float = 0.0
    liquidity_usd_secondary: float | None = None
    """Independent second source. Disagreement is itself a risk signal (docs/04 §7.1)."""
    volume_1h_usd: float = 0.0
    unique_buyers_15m: int = 0
    unique_sellers_15m: int = 0
    buy_count_15m: int = 0
    sell_count_15m: int = 0
    unique_buyers_growth_15m: float = 0.0
    holder_growth_15m: float = 0.0
    holders: int = 0
    price_15m_ago: float | None = None
    ema20_1m: float | None = None
    liquidity_drop_5m_pct: float = 0.0
    insider_net_flow_usd: float = 0.0
    """Negative = creator/top holders are net sellers."""
    same_wallet_volume_share: float = Field(default=0.0, ge=0.0, le=1.0)
    token_age_seconds: float = 0.0


class SocialWindow(_Frozen):
    """Aggregated social state for one token (docs/02 `social_metrics`)."""

    mentions: int = 0
    mentions_per_account: tuple[int, ...] = ()
    """Mention counts per distinct account — the input to the diversity calculation."""
    quality_weighted_mentions: float = 0.0
    baseline_ewma: float = 0.0
    baseline_mad: float = 0.0
    high_quality_accounts: int = 0
    engagement_ratio: float = 0.0
    avg_sentiment: float = Field(default=0.0, ge=-1.0, le=1.0)
    bot_share: float = Field(default=0.0, ge=0.0, le=1.0)
    duplicate_text_share: float = Field(default=0.0, ge=0.0, le=1.0)
    new_account_share: float = Field(default=0.0, ge=0.0, le=1.0)
    peak_quality_weighted: float = 0.0
    """Highest value seen for this token — used to detect social collapse on exit."""
    scam_warning_count: int = 0


class TweetAnalysis(_Frozen):
    """The only thing the AI layer is allowed to produce (docs/04 §10)."""

    tweet_id: str
    token_address: str | None = None
    match_confidence: float = Field(ge=0.0, le=1.0)
    sentiment: float = Field(ge=-1.0, le=1.0)
    relevance: float = Field(ge=0.0, le=1.0)
    hype: float = Field(ge=0.0, le=1.0)
    credibility: float = Field(ge=0.0, le=1.0)
    intent: TweetIntent
    category: TweetCategory
    confidence: float = Field(ge=0.0, le=1.0)
    model: str = "heuristic"


class TokenSnapshot(_Frozen):
    """Everything the decision core knows about one token at one instant.

    This is the sole input to ``signals.engine.evaluate``. Point-in-time by
    construction: if a fact is not in here, the engine cannot see it — which is what
    makes look-ahead bias structurally impossible in the backtest (docs/05 §2).
    """

    address: str
    symbol: str
    chain: str = "solana"
    observed_at: datetime
    market: MarketSnapshot
    security: SecuritySnapshot
    social: SocialWindow = SocialWindow()
    activities: tuple[TraderActivity, ...] = ()
    correlations: dict[str, float] = Field(default_factory=dict)
    """rho between trader pairs, keyed "id_a|id_b" with id_a < id_b (docs/04 §4)."""
    first_smart_entry_price: float | None = None
    intended_position_usd: float = 1000.0
    """Liquidity is scored against the size you actually intend to trade."""


# ── Decision output ──────────────────────────────────────────────────────────


class RiskFinding(_Frozen):
    rule_id: str
    severity: Severity
    detail: str
    value: float | None = None
    threshold: float | None = None


class RiskAssessment(_Frozen):
    veto: bool
    findings: tuple[RiskFinding, ...]
    soft_multiplier: float = Field(ge=0.0, le=1.0)
    risk_score: float = Field(ge=0.0, le=100.0)
    """Human-facing 0-100 where higher = safer. Not used in the master formula."""

    @property
    def veto_reasons(self) -> tuple[str, ...]:
        return tuple(f.rule_id for f in self.findings if f.severity is Severity.VETO)


class TimingAssessment(_Frozen):
    multiplier: float = Field(ge=0.0, le=1.0)
    smart_multiple: float
    runup_15m: float
    parabolic_extension: float
    binding_factor: str
    """Which of the three measures was the constraint — goes straight into the alert."""


class ConfirmationAssessment(_Frozen):
    n_traders: int
    n_effective: float
    mean_quality: float
    detail: str


class SignalDecision(_Frozen):
    token_address: str
    symbol: str
    observed_at: datetime
    state: SignalState
    master_score: float = Field(ge=0.0, le=100.0)
    smart_money_score: float
    social_score: float
    market_score: float
    liquidity_score: float
    risk: RiskAssessment
    timing: TimingAssessment
    confirmation: ConfirmationAssessment
    flags: tuple[str, ...]
    contributors: tuple[tuple[str, float], ...] = ()
    """(trader_id, usd_bought) per confirming trader, largest first.

    Carried on the decision so alerts can show real amounts. An alert that invents a
    plausible-looking split is worse than one that shows nothing.
    """
    features: dict[str, float]
    """The full feature vector — this is the fase-10 training set (docs/04 §11)."""
    explanation: str
    config_version: str

    @property
    def is_entry(self) -> bool:
        return self.state in (SignalState.POTENTIAL_ENTRY, SignalState.STRONG_ENTRY)
