"""Command-line entrypoints.

    python -m fomo.cli scenarios          # run every scenario through the engine
    python -m fomo.cli explain happy_path # full breakdown of one scenario
    python -m fomo.cli alert happy_path   # what the Telegram message looks like
    python -m fomo.cli traders            # trader scoring worked example
    python -m fomo.cli backtest           # synthetic replay + performance report
    python -m fomo.cli sweep              # parameter sweep across thresholds

Everything here runs offline against the deterministic scenarios in
``fomo/adapters/scenarios.py`` — no API keys, no network.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from .adapters import scenarios
from .adapters.scenarios import T0, healthy_market, trader
from .alerts.format import bar, render_telegram
from .backtest.harness import ReplayEvent, format_sweep, run, sweep, walk_forward
from .config import get_settings, load_settings
from .domain import Severity, TraderStats
from .scoring.trader import score_trader
from .signals import evaluate


def cmd_scenarios(_: argparse.Namespace) -> int:
    print(f"{'scenario':<22} {'signal':<16} {'master':>7}  flags")
    print("-" * 78)
    for name, build in scenarios.ALL_SCENARIOS.items():
        d = evaluate(build())
        flags = ", ".join(d.flags) or "-"
        print(f"{name:<22} {d.state.value:<16} {d.master_score:>7.2f}  {flags}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    build = scenarios.ALL_SCENARIOS.get(args.scenario)
    if build is None:
        print(f"unknown scenario '{args.scenario}'; try: {', '.join(scenarios.ALL_SCENARIOS)}")
        return 1

    d = evaluate(build())
    print(f"\nCOIN: ${d.symbol}   —   {d.token_address[:16]}…")
    print(f"observed at {d.observed_at.isoformat()}   config {d.config_version}\n")

    for label, score in (
        ("Smart Money", d.smart_money_score),
        ("Social Momentum", d.social_score),
        ("Market Momentum", d.market_score),
        ("Liquidity", d.liquidity_score),
        ("Safety (risk)", d.risk.risk_score),
    ):
        print(f"  {label:<18} {bar(score)} {score:6.2f}")

    print(f"\n  {'Veto':<18} {'YES' if d.risk.veto else 'no'}")
    print(f"  {'R_soft':<18} {d.risk.soft_multiplier:.3f}")
    print(
        f"  {'Timing':<18} {d.timing.multiplier:.3f}"
        f"   ({d.timing.smart_multiple:.2f}x first smart entry, binding: {d.timing.binding_factor})"
    )
    print(f"  {'Evidence':<18} {d.features['evidence']:+.3f}  ->  sigma = {d.features['conviction']:.4f}")
    print(f"  {'N_eff':<18} {d.confirmation.n_effective:.2f} of {d.confirmation.n_traders} wallets")

    print(
        f"\n  MASTER = 100 x {0 if d.risk.veto else 1} x {d.risk.soft_multiplier:.3f} "
        f"x {d.timing.multiplier:.3f} x {d.features['conviction']:.4f} = {d.master_score:.2f}"
    )
    print(f"  SIGNAL = {d.state.value}\n")

    if d.risk.findings:
        print("  Risk findings:")
        for finding in d.risk.findings:
            marker = "VETO" if finding.severity is Severity.VETO else finding.severity.value
            print(f"    [{marker:>6}] {finding.rule_id}: {finding.detail}")
    if d.flags:
        print(f"\n  Flags: {', '.join(d.flags)}")
    print(f"\n  {d.explanation}\n")
    return 0


def cmd_alert(args: argparse.Namespace) -> int:
    build = scenarios.ALL_SCENARIOS.get(args.scenario)
    if build is None:
        print(f"unknown scenario '{args.scenario}'")
        return 1
    print(render_telegram(evaluate(build())))
    return 0


def cmd_traders(_: argparse.Namespace) -> int:
    """Worked example: why raw ROI ranks traders wrongly."""
    consistent = TraderStats(
        trader_id="A",
        nickname="solkid",
        n_trades=87,
        n_wins=50,
        log_returns=tuple([0.45, -0.30, 0.60, -0.25, 0.35, -0.20] * 15)[:87],
        largest_win_usd=9_000.0,
        total_win_usd=62_000.0,
        median_entry_mcap_usd=84_000.0,
        median_entry_age_seconds=360.0,
        exit_quality=0.63,
        trades_per_day=3.1,
        bot_likeness=0.02,
        active_days=95,
    )
    lucky = TraderStats(
        trader_id="B",
        nickname="apewhale",
        n_trades=23,
        n_wins=9,
        log_returns=tuple([3.9] + [-0.45] * 14 + [0.30] * 8),
        largest_win_usd=71_000.0,
        total_win_usd=88_000.0,
        median_entry_mcap_usd=410_000.0,
        median_entry_age_seconds=5_520.0,
        exit_quality=0.38,
        trades_per_day=1.4,
        active_days=70,
    )
    botlike = TraderStats(
        trader_id="C",
        nickname="mm-bot-ish",
        n_trades=1_400,
        n_wins=760,
        log_returns=tuple([0.04, -0.03] * 700),
        largest_win_usd=900.0,
        total_win_usd=40_000.0,
        median_entry_mcap_usd=1_800_000.0,
        median_entry_age_seconds=42_000.0,
        exit_quality=0.51,
        trades_per_day=180.0,
        bot_likeness=0.85,
        active_days=8,
    )

    print(f"\n{'trader':<14}{'score':>7}{'reliab.':>11}   components")
    print("-" * 92)
    for stats in (consistent, lucky, botlike):
        s = score_trader(stats)
        comps = "  ".join(
            f"{k}={s.components[k]:.2f}" for k in ("perf", "winrate", "consistency", "early", "exit_quality")
        )
        print(f"{s.nickname:<14}{s.score:>7.1f}{s.reliability.value:>11}   {comps}")
        print(f"{'':<14}{'':>7}{'':>11}   shrink={s.components['shrink']:.2f}  penalty={s.penalty:.2f}")

    import math

    total_b = sum(lucky.log_returns)
    concentration = 100 * lucky.largest_win_usd / lucky.total_win_usd
    print(
        f"\nWhy the ranking is right:\n"
        f"  apewhale's headline ROI looks spectacular — one trade returned "
        f"{math.exp(max(lucky.log_returns)):.0f}x. But that single trade is {concentration:.0f}% of all\n"
        f"  his profit, and his cumulative log-return is {total_b:+.2f}: without that one hit he is\n"
        f"  break-even at best. Consistency collapses to "
        f"{score_trader(lucky).components['consistency']:.2f} and the score ranks him below solkid,\n"
        f"  whose edge is smaller per trade but reproducible.\n"
        f"  mm-bot-ish has the longest record and a positive win rate, but 180 trades/day and a\n"
        f"  0.85 bot-likeness cut his score by {100 * (1 - score_trader(botlike).penalty):.0f}% — "
        f"copying a market maker is not copying an edge.\n"
    )
    return 0


def _synthetic_events() -> list[ReplayEvent]:
    """A deterministic replay: 12 tokens, half of which work out.

    This is a smoke test of the whole pipeline, not a claim about real returns. Real
    backtests need real historical liquidity and social data (docs/05 §2).
    """
    events: list[ReplayEvent] = []
    for i in range(12):
        wins = i % 2 == 0
        base = scenarios.happy_path().model_copy(
            update={"address": f"Token{i:02d}", "symbol": f"TKN{i:02d}"}
        )
        entry_at = T0 + timedelta(hours=i)
        events.append(ReplayEvent(observed_at=entry_at, snapshot=base))

        # Winners: run to +90%, take the profit tranche, then give back enough to
        # trigger the trailing exit. Losers: straight through the stop.
        path = [1.35, 1.90, 1.35] if wins else [0.85, 0.60, 0.45]
        for step, multiple in enumerate(path, start=1):
            market = healthy_market(
                price_usd=0.00041 * multiple,
                price_15m_ago=0.00041 * multiple,
                ema20_1m=0.00041 * multiple,
            )
            events.append(
                ReplayEvent(
                    observed_at=entry_at + timedelta(minutes=15 * step),
                    snapshot=base.model_copy(update={"market": market}),
                )
            )
    return events


def cmd_backtest(_: argparse.Namespace) -> int:
    result = run(_synthetic_events(), starting_equity=10_000.0)
    print("\n" + result.summary())
    print(f"\nexit reasons: {result.performance.exit_reason_counts}")
    print(
        f"avg MFE {result.performance.avg_mfe_pct:+.1f}%   "
        f"avg MAE {result.performance.avg_mae_pct:+.1f}%   "
        f"fees ${result.performance.total_fees_usd:.2f}   "
        f"slippage ${result.performance.total_slippage_usd:.2f}\n"
    )
    return 0


def cmd_sweep(_: argparse.Namespace) -> int:
    events = _synthetic_events()
    base = get_settings()

    def with_master_min(potential: float, strong: float):
        data = base.model_dump()
        data["thresholds"]["signal"]["potential_entry"]["master_min"] = potential
        data["thresholds"]["signal"]["strong_entry"]["master_min"] = strong
        return type(base)(**data)

    variants = {
        "master>=65 / strong>=78": with_master_min(65.0, 78.0),
        "master>=70 / strong>=82 (default)": base,
        "master>=78 / strong>=88": with_master_min(78.0, 88.0),
        "master>=85 / strong>=92": with_master_min(85.0, 92.0),
    }

    print("\n" + format_sweep(sweep(events, variants)))
    print(
        "\nDo not just pick the best row. A variant with few trades can beat one with many\n"
        "by chance; what matters is whether it wins in every walk-forward window.\n"
    )
    windows = walk_forward(events, variants, n_windows=3)
    print(f"{'variant':<34} {'per-window expectancy %':>26}")
    print("-" * 62)
    for label, performances in windows.items():
        cells = "  ".join(f"{p.expectancy_pct:+6.2f}" for p in performances)
        print(f"{label:<34} {cells:>26}")
    print()
    return 0


def cmd_config(_: argparse.Namespace) -> int:
    settings = load_settings()
    print(f"config version: {settings.version}")
    print(f"weights:    {settings.weights.version}")
    print(f"thresholds: {settings.thresholds.version}")
    print("\nvalidated OK — every key is known and every value is in range")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fomo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scenarios", help="run every scenario through the engine").set_defaults(fn=cmd_scenarios)
    sub.add_parser("backtest", help="synthetic replay + performance report").set_defaults(fn=cmd_backtest)
    sub.add_parser("sweep", help="parameter sweep + walk-forward").set_defaults(fn=cmd_sweep)
    sub.add_parser("traders", help="trader scoring worked example").set_defaults(fn=cmd_traders)
    sub.add_parser("config", help="validate configuration").set_defaults(fn=cmd_config)

    explain = sub.add_parser("explain", help="full breakdown of one scenario")
    explain.add_argument("scenario", nargs="?", default="happy_path")
    explain.set_defaults(fn=cmd_explain)

    alert = sub.add_parser("alert", help="render the Telegram alert for a scenario")
    alert.add_argument("scenario", nargs="?", default="happy_path")
    alert.set_defaults(fn=cmd_alert)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
