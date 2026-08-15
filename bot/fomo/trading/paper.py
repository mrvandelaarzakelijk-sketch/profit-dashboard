"""Paper trading engine (docs/04 §8.3, docs/05 fase 7).

Deliberately pessimistic. Every simplification here rounds against us: price impact is
charged on both legs, fees are charged on gross, and exits fill at the impacted price.
Optimistic fill assumptions are the most common reason a paper strategy turns into a
losing live one.

The same engine runs live-paper and backtest. There is no separate "simulation mode" —
that split is how backtests end up testing code that never runs in production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..config import Settings, get_settings
from ..domain import ExitReason, MarketSnapshot, SignalDecision, SignalState
from ..scoring.components import max_affordable_size_usd, price_impact_pct


@dataclass
class Position:
    token_address: str
    symbol: str
    opened_at: datetime
    signal_state: SignalState
    entry_price: float
    """Quoted mid price at entry — for reporting only."""
    entry_price_effective: float
    """What we actually paid per token after impact and fees. This is the P&L basis."""
    cost_basis_usd: float
    tokens: float
    tokens_remaining: float
    stop_price: float
    high_water_price: float
    take_profit_hit: list[bool]
    realized_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    master_at_entry: float = 0.0
    neff_at_entry: float = 0.0

    @property
    def remaining_fraction(self) -> float:
        return self.tokens_remaining / self.tokens if self.tokens > 0 else 0.0

    @property
    def remaining_cost_usd(self) -> float:
        return self.cost_basis_usd * self.remaining_fraction


@dataclass
class Fill:
    token_address: str
    symbol: str
    at: datetime
    reason: ExitReason
    fraction: float
    price: float
    price_effective: float
    proceeds_usd: float
    pnl_usd: float
    return_pct: float


@dataclass
class ClosedTrade:
    token_address: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    entry_price_effective: float
    exit_price_effective: float
    size_usd: float
    gross_pnl_usd: float
    fees_usd: float
    slippage_usd: float
    net_pnl_usd: float
    return_pct: float
    hold_seconds: float
    exit_reason: ExitReason
    max_favorable_excursion: float
    max_adverse_excursion: float
    master_at_entry: float
    neff_at_entry: float


@dataclass
class PaperBroker:
    """Virtual portfolio. All amounts in USD."""

    settings: Settings = field(default_factory=get_settings)
    starting_equity_usd: float = 10_000.0
    cash_usd: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[ClosedTrade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    _day_start_equity: float = field(init=False, default=0.0)
    _current_day: str = field(init=False, default="")
    halted_reason: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.cash_usd = self.starting_equity_usd
        self._day_start_equity = self.starting_equity_usd

    # ── portfolio state ──────────────────────────────────────────────────────

    def open_value_usd(self, prices: dict[str, float]) -> float:
        return sum(
            p.tokens_remaining * prices.get(addr, p.entry_price_effective)
            for addr, p in self.positions.items()
        )

    def equity_usd(self, prices: dict[str, float] | None = None) -> float:
        return self.cash_usd + self.open_value_usd(prices or {})

    # ── entries ──────────────────────────────────────────────────────────────

    def _roll_day(self, at: datetime, equity: float) -> None:
        day = at.date().isoformat()
        if day != self._current_day:
            self._current_day = day
            self._day_start_equity = equity
            self.halted_reason = None

    def can_open(self, decision: SignalDecision, at: datetime, equity: float) -> str | None:
        """Returns a rejection reason, or None if the trade is allowed."""
        pos_cfg = self.settings.thresholds.position
        self._roll_day(at, equity)

        if self.halted_reason:
            return self.halted_reason
        if not decision.is_entry:
            return f"state {decision.state.value} is not an entry"
        if decision.token_address in self.positions:
            return "already holding this token"
        if len(self.positions) >= pos_cfg.max_concurrent_positions:
            return "max concurrent positions reached"

        drawdown_pct = 100.0 * (self._day_start_equity - equity) / self._day_start_equity
        if drawdown_pct >= pos_cfg.max_daily_loss_pct:
            # Circuit breaker: the point of a bad day is to make it stop, not to trade out of it.
            self.halted_reason = f"daily loss limit hit ({drawdown_pct:.1f}%)"
            return self.halted_reason
        return None

    def position_size_usd(self, decision: SignalDecision, market: MarketSnapshot, equity: float) -> float:
        """Size by risk budget *and* by what the pool can absorb — whichever is smaller."""
        pos_cfg = self.settings.thresholds.position
        by_equity = equity * pos_cfg.max_position_pct_of_equity / 100.0
        by_liquidity = max_affordable_size_usd(market.liquidity_usd, self.settings.thresholds.liquidity)
        return max(min(by_equity, by_liquidity), 0.0)

    def open_position(
        self, decision: SignalDecision, market: MarketSnapshot, at: datetime
    ) -> Position | None:
        equity = self.equity_usd({decision.token_address: market.price_usd})
        if self.can_open(decision, at, equity) is not None:
            return None

        size = self.position_size_usd(decision, market, equity)
        if size <= 0 or size > self.cash_usd:
            return None

        liq_cfg = self.settings.thresholds.liquidity
        impact_pct = price_impact_pct(size, market.liquidity_usd)
        fee = size * liq_cfg.dex_fee_pct / 100.0
        slip = size * impact_pct / 100.0
        net_to_tokens = size - fee - slip - liq_cfg.priority_fee_usd
        if net_to_tokens <= 0:
            return None

        tokens = net_to_tokens / market.price_usd
        effective = size / tokens

        exit_cfg = self.settings.thresholds.exit
        position = Position(
            token_address=decision.token_address,
            symbol=decision.symbol,
            opened_at=at,
            signal_state=decision.state,
            entry_price=market.price_usd,
            entry_price_effective=effective,
            cost_basis_usd=size,
            tokens=tokens,
            tokens_remaining=tokens,
            stop_price=effective * (1.0 - exit_cfg.stop_loss_pct / 100.0),
            high_water_price=market.price_usd,
            take_profit_hit=[False] * len(exit_cfg.take_profit_levels),
            fees_usd=fee + liq_cfg.priority_fee_usd,
            slippage_usd=slip,
            master_at_entry=decision.master_score,
            neff_at_entry=decision.confirmation.n_effective,
        )
        self.cash_usd -= size
        self.positions[decision.token_address] = position
        return position

    # ── exits ────────────────────────────────────────────────────────────────

    def _sell(self, position: Position, fraction: float, market: MarketSnapshot, at: datetime, reason: ExitReason) -> Fill:
        fraction = min(max(fraction, 0.0), position.remaining_fraction)
        tokens = position.tokens * fraction
        liq_cfg = self.settings.thresholds.liquidity

        gross = tokens * market.price_usd
        impact_pct = price_impact_pct(gross, market.liquidity_usd)
        slip = gross * impact_pct / 100.0
        fee = gross * liq_cfg.dex_fee_pct / 100.0
        net = gross - slip - fee - liq_cfg.priority_fee_usd

        cost = position.cost_basis_usd * fraction
        pnl = net - cost

        position.tokens_remaining -= tokens
        position.realized_pnl_usd += pnl
        position.fees_usd += fee + liq_cfg.priority_fee_usd
        position.slippage_usd += slip
        self.cash_usd += net

        fill = Fill(
            token_address=position.token_address,
            symbol=position.symbol,
            at=at,
            reason=reason,
            fraction=fraction,
            price=market.price_usd,
            price_effective=net / tokens if tokens > 0 else market.price_usd,
            proceeds_usd=net,
            pnl_usd=pnl,
            return_pct=100.0 * pnl / cost if cost > 0 else 0.0,
        )
        self.fills.append(fill)
        return fill

    def _finalize(self, position: Position, at: datetime, price: float, reason: ExitReason) -> None:
        gross_pnl = position.realized_pnl_usd + position.fees_usd + position.slippage_usd
        self.trades.append(
            ClosedTrade(
                token_address=position.token_address,
                symbol=position.symbol,
                opened_at=position.opened_at,
                closed_at=at,
                entry_price_effective=position.entry_price_effective,
                exit_price_effective=price,
                size_usd=position.cost_basis_usd,
                gross_pnl_usd=gross_pnl,
                fees_usd=position.fees_usd,
                slippage_usd=position.slippage_usd,
                net_pnl_usd=position.realized_pnl_usd,
                return_pct=100.0 * position.realized_pnl_usd / position.cost_basis_usd,
                hold_seconds=(at - position.opened_at).total_seconds(),
                exit_reason=reason,
                max_favorable_excursion=position.max_favorable_excursion,
                max_adverse_excursion=position.max_adverse_excursion,
                master_at_entry=position.master_at_entry,
                neff_at_entry=position.neff_at_entry,
            )
        )
        self.positions.pop(position.token_address, None)

    def on_tick(
        self,
        token_address: str,
        market: MarketSnapshot,
        at: datetime,
        decision: SignalDecision | None = None,
        smart_money_exit_share: float = 0.0,
    ) -> list[Fill]:
        """Evaluate exits for one position. Ordered by urgency, most severe first."""
        position = self.positions.get(token_address)
        if position is None:
            return []

        exit_cfg = self.settings.thresholds.exit
        fills: list[Fill] = []

        position.high_water_price = max(position.high_water_price, market.price_usd)
        excursion = 100.0 * (market.price_usd / position.entry_price_effective - 1.0)
        position.max_favorable_excursion = max(position.max_favorable_excursion, excursion)
        position.max_adverse_excursion = min(position.max_adverse_excursion, excursion)

        # 1. Emergency — the pool is disappearing or the token just became unsafe.
        emergency = market.liquidity_drop_5m_pct >= exit_cfg.emergency_liquidity_drop_pct or (
            decision is not None and decision.risk.veto
        )
        if emergency:
            fills.append(self._sell(position, position.remaining_fraction, market, at, ExitReason.EMERGENCY))
            self._finalize(position, at, market.price_usd, ExitReason.EMERGENCY)
            return fills

        # 2. Stop loss — hard, no waiting.
        if market.price_usd <= position.stop_price:
            fills.append(self._sell(position, position.remaining_fraction, market, at, ExitReason.STOP_LOSS))
            self._finalize(position, at, market.price_usd, ExitReason.STOP_LOSS)
            return fills

        # 3. Smart money leaving. The strongest exit there is: they see what we do not.
        if smart_money_exit_share >= exit_cfg.smart_exit_share:
            fills.append(
                self._sell(position, position.remaining_fraction, market, at, ExitReason.SMART_MONEY_EXIT)
            )
            self._finalize(position, at, market.price_usd, ExitReason.SMART_MONEY_EXIT)
            return fills

        # 4. Thesis invalidated.
        if decision is not None:
            social_collapsed = (
                decision.social_score > 0
                and decision.features.get("soc_velocity_z", 0.0) < 0
                and decision.social_score
                < (100.0 - exit_cfg.invalidate_social_drop_pct) * position.master_at_entry / 100.0
            )
            if (
                decision.master_score < exit_cfg.invalidate_master_below
                or decision.confirmation.n_effective < exit_cfg.invalidate_neff_below
                or social_collapsed
            ):
                fills.append(
                    self._sell(position, position.remaining_fraction, market, at, ExitReason.SIGNAL_INVALIDATED)
                )
                self._finalize(position, at, market.price_usd, ExitReason.SIGNAL_INVALIDATED)
                return fills

        # 5. Scaled take-profit. With memecoins you realise on the way up or not at all.
        gain_pct = 100.0 * (market.price_usd / position.entry_price_effective - 1.0)
        for i, level in enumerate(exit_cfg.take_profit_levels):
            if not position.take_profit_hit[i] and gain_pct >= level:
                position.take_profit_hit[i] = True
                fraction = exit_cfg.take_profit_fractions[i]
                fills.append(self._sell(position, fraction, market, at, ExitReason.TAKE_PROFIT))

        # 6. Trailing stop, tightening once the position is well in profit.
        trail_pct = (
            exit_cfg.trailing_tight_pct
            if gain_pct >= exit_cfg.trailing_tighten_at_pct
            else exit_cfg.trailing_pct
        )
        trail_price = position.high_water_price * (1.0 - trail_pct / 100.0)
        if any(position.take_profit_hit) and market.price_usd <= trail_price:
            if position.tokens_remaining > 0:
                fills.append(self._sell(position, position.remaining_fraction, market, at, ExitReason.TRAILING))
            self._finalize(position, at, market.price_usd, ExitReason.TRAILING)
            return fills

        # 7. Timeout — capital exists to rotate.
        if (at - position.opened_at).total_seconds() >= exit_cfg.max_hold_seconds:
            if position.tokens_remaining > 0:
                fills.append(self._sell(position, position.remaining_fraction, market, at, ExitReason.TIMEOUT))
            self._finalize(position, at, market.price_usd, ExitReason.TIMEOUT)
            return fills

        if position.tokens_remaining <= 1e-12:
            self._finalize(position, at, market.price_usd, ExitReason.TAKE_PROFIT)

        return fills
