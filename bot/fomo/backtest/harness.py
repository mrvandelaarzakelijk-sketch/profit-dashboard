"""Event-replay backtesting (docs/05 §2).

The one structural guarantee: the engine only ever sees a ``TokenSnapshot``, and a
snapshot carries a single ``observed_at``. There is no query interface into "the
database", so there is nothing to accidentally read from the future. Look-ahead bias is
prevented by the shape of the code, not by remembering to avoid it.

Events are replayed in ``observed_at`` order — *observed*, not *event* time. Data you
only had eight seconds later is data you did not have at t=0.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from ..config import Settings, get_settings
from ..domain import SignalDecision, TokenSnapshot
from ..signals import evaluate
from ..trading.analytics import Performance
from ..trading.analytics import evaluate as evaluate_performance
from ..trading.paper import ClosedTrade, PaperBroker


@dataclass(frozen=True)
class ReplayEvent:
    """One point-in-time observation of one token."""

    observed_at: datetime
    snapshot: TokenSnapshot
    smart_money_exit_share: float = 0.0
    """Fraction of the confirming N_eff that has sold by this point."""


@dataclass
class BacktestResult:
    performance: Performance
    trades: list[ClosedTrade]
    decisions: list[SignalDecision]
    starting_equity: float
    final_equity: float
    config_version: str
    n_events: int
    state_counts: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        states = ", ".join(f"{k}={v}" for k, v in sorted(self.state_counts.items()))
        return (
            f"{self.n_events} events -> {states}\n"
            f"{self.performance.summary()}\n"
            f"equity ${self.starting_equity:,.0f} -> ${self.final_equity:,.0f} "
            f"(config {self.config_version})"
        )


def _ordered(events: Iterable[ReplayEvent]) -> Iterator[ReplayEvent]:
    """Chronological by observation time. Ties broken deterministically by address so
    that a rerun on identical data produces byte-identical trades."""
    yield from sorted(events, key=lambda e: (e.observed_at, e.snapshot.address))


def run(
    events: Iterable[ReplayEvent],
    *,
    starting_equity: float = 10_000.0,
    settings: Settings | None = None,
    record_decisions: bool = True,
) -> BacktestResult:
    cfg = settings or get_settings()
    broker = PaperBroker(settings=cfg, starting_equity_usd=starting_equity)

    decisions: list[SignalDecision] = []
    state_counts: dict[str, int] = {}
    last_price: dict[str, float] = {}
    n_events = 0

    for event in _ordered(events):
        n_events += 1
        snapshot = event.snapshot
        decision = evaluate(snapshot, cfg)
        if record_decisions:
            decisions.append(decision)
        state_counts[decision.state.value] = state_counts.get(decision.state.value, 0) + 1
        last_price[snapshot.address] = snapshot.market.price_usd

        # Exits first: an open position must be able to react to this tick before we
        # consider spending more capital on a new one.
        if snapshot.address in broker.positions:
            broker.on_tick(
                snapshot.address,
                snapshot.market,
                event.observed_at,
                decision=decision,
                smart_money_exit_share=event.smart_money_exit_share,
            )
        elif decision.is_entry:
            broker.open_position(decision, snapshot.market, event.observed_at)

    final_equity = broker.equity_usd(last_price)
    return BacktestResult(
        performance=evaluate_performance(broker.trades, starting_equity),
        trades=list(broker.trades),
        decisions=decisions,
        starting_equity=starting_equity,
        final_equity=final_equity,
        config_version=cfg.version,
        n_events=n_events,
        state_counts=state_counts,
    )


# ── Parameter sweeps ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepRow:
    label: str
    performance: Performance

    def as_row(self) -> str:
        p = self.performance
        return (
            f"{self.label:<34} {p.n_trades:>5}  {100 * p.win_rate:>5.1f}  "
            f"{p.expectancy_pct:>+6.2f}  {p.profit_factor:>5.2f}  "
            f"{-p.max_drawdown_pct:>6.1f}  {p.sharpe:>6.2f}"
        )


def sweep(
    events: Sequence[ReplayEvent],
    variants: dict[str, Settings],
    *,
    starting_equity: float = 10_000.0,
) -> list[SweepRow]:
    """Run the same event stream under several configurations.

    Read the output with suspicion. Picking the best row is exactly how you overfit
    (docs/05 §2): a variant with 87 trades that beats one with 412 may simply be noise.
    The question that matters is whether the ranking survives in *every* walk-forward
    window, which is what ``walk_forward`` below is for.
    """
    rows = [SweepRow(label, run(events, starting_equity=starting_equity, settings=cfg).performance)
            for label, cfg in variants.items()]
    return rows


def format_sweep(rows: Sequence[SweepRow]) -> str:
    header = f"{'variant':<34} {'n':>5}  {'win%':>5}  {'exp%':>6}  {'PF':>5}  {'maxDD':>6}  {'Sharpe':>6}"
    return "\n".join([header, "-" * len(header), *(r.as_row() for r in rows)])


def walk_forward(
    events: Sequence[ReplayEvent],
    variants: dict[str, Settings],
    *,
    n_windows: int = 4,
    starting_equity: float = 10_000.0,
) -> dict[str, list[Performance]]:
    """Split the stream into consecutive windows and score every variant in each.

    Consistency across windows is a far more trustworthy signal than the best aggregate
    number — the latter is precisely what overfitting produces.
    """
    ordered = list(_ordered(events))
    if n_windows < 1 or not ordered:
        return {}
    size = max(len(ordered) // n_windows, 1)
    windows = [ordered[i : i + size] for i in range(0, len(ordered), size)][:n_windows]

    return {
        label: [run(w, starting_equity=starting_equity, settings=cfg).performance for w in windows]
        for label, cfg in variants.items()
    }
