"""Alert rendering for Telegram and Discord (docs/01 §1.J).

Pure formatting — no network. Sending lives in the transport modules so that the
message content is unit-testable without mocking an HTTP client.

The design rule for these messages: **lead with the reason, not the score.** A number
you cannot explain is a number you should not act on, and at 3am you will not remember
what a 91 was made of.
"""

from __future__ import annotations

from ..domain import SignalDecision, SignalState, Severity
from .telegram import escape_html as esc

BAR_FILLED = "█"
BAR_EMPTY = "░"

STATE_EMOJI = {
    SignalState.STRONG_ENTRY: "🚨",
    SignalState.POTENTIAL_ENTRY: "⚡",
    SignalState.EARLY_WATCH: "👀",
    SignalState.WATCH: "📋",
    SignalState.AVOID: "⛔",
    SignalState.EXIT: "🔻",
}


def bar(score: float, width: int = 10) -> str:
    filled = int(round(max(0.0, min(score, 100.0)) / 100.0 * width))
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


def _usd(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.1f}k"
    return f"${value:,.0f}"


def dedupe_key(decision: SignalDecision, bucket_seconds: int = 900) -> str:
    """One alert per token per state per time bucket — keeps one token from flooding
    the channel while still letting a genuine upgrade through."""
    bucket = int(decision.observed_at.timestamp()) // bucket_seconds
    return f"{decision.token_address}:{decision.state.value}:{bucket}"


def render_telegram(decision: SignalDecision, *, top_traders: int = 4) -> str:
    """HTML-parse-mode message body."""
    d = decision
    emoji = STATE_EMOJI.get(d.state, "•")
    lines = [
        f"{emoji} <b>{esc(d.state.value.replace('_', ' '))}</b>  —  <b>${esc(d.symbol)}</b>",
        "",
        f"<b>Master Score: {d.master_score:.0f}/100</b>",
        "<pre>",
        f"Smart Money  {bar(d.smart_money_score)} {d.smart_money_score:.0f}",
        f"Social       {bar(d.social_score)} {d.social_score:.0f}",
        f"Momentum     {bar(d.market_score)} {d.market_score:.0f}",
        f"Liquidity    {bar(d.liquidity_score)} {d.liquidity_score:.0f}",
        f"Safety       {bar(d.risk.risk_score)} {d.risk.risk_score:.0f}",
        "</pre>",
    ]

    if d.risk.veto:
        lines += ["", "⛔ <b>HARD VETO</b>"]
        lines += [f"• {esc(f.detail)}" for f in d.risk.findings if f.severity is Severity.VETO]
        return "\n".join(lines)

    if d.contributors:
        lines += ["", "👛 <b>Smart Money</b>"]
        lines += [f"• {esc(name)} bought {_usd(usd)}" for name, usd in d.contributors[:top_traders]]
    lines.append(
        f"   <i>{d.confirmation.n_traders} wallets → "
        f"{d.confirmation.n_effective:.2f} independent</i>"
    )

    f = d.features
    if f.get("soc_velocity_z", 0) > 0:
        lines += [
            "",
            "🐦 <b>Social</b>",
            f"• velocity z = {f['soc_velocity_z']:.1f}",
            f"• {f.get('soc_accounts_eff', 0):.0f} effective accounts "
            f"(diversity {f.get('soc_diversity', 0):.2f})",
            f"• bot share {100 * f.get('soc_bot_share', 0):.0f}%",
        ]

    lines += [
        "",
        "💰 <b>Market</b>",
        f"• liquidity {_usd(f.get('liq_usd', 0.0))}",
        f"• roundtrip cost {f.get('liq_roundtrip_cost_pct', 0.0):.2f}%",
        f"• unique buyers 15m: {f.get('mkt_unique_buyers_15m', 0.0):.0f}",
        f"• 1h volume / liquidity: {f.get('mkt_turnover', 0.0):.1f}x",
    ]

    lines += [
        "",
        "⏱ <b>Timing</b>",
        f"• {d.timing.smart_multiple:.2f}x first smart-money entry "
        f"(multiplier {d.timing.multiplier:.2f}, binding: {esc(d.timing.binding_factor)})",
    ]

    soft = [f for f in d.risk.findings if f.severity is not Severity.VETO]
    if soft:
        lines += ["", "⚠️ <b>Risk</b>"] + [f"• {esc(finding.detail)}" for finding in soft[:4]]

    if d.flags:
        lines += ["", "🚩 <b>Flags</b>", "• " + esc(", ".join(d.flags))]

    lines += ["", f"<i>{esc(d.explanation)}</i>", "", f"<code>{esc(d.config_version)}</code>"]
    return "\n".join(lines)


def render_discord(decision: SignalDecision) -> dict:
    """Discord webhook payload with a rich embed."""
    d = decision
    colour = {
        SignalState.STRONG_ENTRY: 0x00FF88,
        SignalState.POTENTIAL_ENTRY: 0x00E5C7,
        SignalState.EARLY_WATCH: 0xFF9500,
        SignalState.WATCH: 0x7D8AA0,
        SignalState.AVOID: 0xFF5C5C,
        SignalState.EXIT: 0xFF2D78,
    }.get(d.state, 0x7D8AA0)

    fields = [
        {"name": "Smart Money", "value": f"{bar(d.smart_money_score)} {d.smart_money_score:.0f}", "inline": True},
        {"name": "Social", "value": f"{bar(d.social_score)} {d.social_score:.0f}", "inline": True},
        {"name": "Momentum", "value": f"{bar(d.market_score)} {d.market_score:.0f}", "inline": True},
        {"name": "Liquidity", "value": f"{bar(d.liquidity_score)} {d.liquidity_score:.0f}", "inline": True},
        {"name": "Safety", "value": f"{bar(d.risk.risk_score)} {d.risk.risk_score:.0f}", "inline": True},
        {
            "name": "Independent traders",
            "value": f"{d.confirmation.n_effective:.2f} of {d.confirmation.n_traders}",
            "inline": True,
        },
    ]
    if d.flags:
        fields.append({"name": "🚩 Flags", "value": ", ".join(d.flags), "inline": False})

    return {
        "embeds": [
            {
                # Discord embeds are not HTML, but the title is still untrusted text.
                "title": f"{STATE_EMOJI.get(d.state, '•')} {d.state.value} — ${d.symbol}",
                "description": f"**Master {d.master_score:.0f}/100**\n{d.explanation}",
                "color": colour,
                "fields": fields,
                "footer": {"text": f"{d.token_address[:12]}… · {d.config_version}"},
                "timestamp": d.observed_at.isoformat(),
            }
        ]
    }
