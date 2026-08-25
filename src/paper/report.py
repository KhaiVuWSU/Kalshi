"""Daily digest and weekly report over signals and paper P&L."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import db


def _fmt_usd(cents: float | None) -> str:
    return f"${(cents or 0) / 100:,.2f}"


def market_link(ticker: str) -> str:
    return f"https://kalshi.com/markets/{ticker}"


def daily_digest(conn: sqlite3.Connection) -> str:
    since = db.now() - 86400
    counts = {r["strategy"]: r["n"] for r in conn.execute(
        "SELECT strategy, COUNT(*) n FROM signals WHERE last_seen_ts >= ? "
        "GROUP BY strategy", (since,))}
    qualified = {r["strategy"]: r["n"] for r in conn.execute(
        "SELECT strategy, COUNT(*) n FROM signals WHERE last_seen_ts >= ? "
        "AND status='qualified' GROUP BY strategy", (since,))}
    realized = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_cents),0) v FROM paper_positions "
        "WHERE status='settled'").fetchone()["v"]
    realized_24h = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_cents),0) v FROM paper_positions "
        "WHERE status='settled' AND settled_ts >= ?", (since,)).fetchone()["v"]
    unrealized = conn.execute(
        """SELECT COALESCE(SUM((mark_price_cents - avg_entry_price_cents)
             * contracts - fees_paid_cents), 0) v
           FROM paper_positions WHERE status='open' AND mark_price_cents IS NOT NULL"""
    ).fetchone()["v"]
    open_rows = conn.execute(
        "SELECT market_ticker, side, contracts, avg_entry_price_cents, "
        "mark_price_cents FROM paper_positions WHERE status='open' "
        "ORDER BY opened_ts DESC LIMIT 15").fetchall()

    total_open = conn.execute(
        "SELECT COUNT(*) c FROM paper_positions WHERE status='open'").fetchone()["c"]
    lines = [
        "**Daily digest — Kalshi Edge Scanner**",
        f"Signals seen (24h): A={counts.get('A', 0)}  B={counts.get('B', 0)}"
        f"  |  qualified: A={qualified.get('A', 0)}  B={qualified.get('B', 0)}",
        f"Paper P&L: realized {_fmt_usd(realized)} (24h {_fmt_usd(realized_24h)}), "
        f"unrealized {_fmt_usd(unrealized)}",
        f"Open positions: {len(open_rows)} shown / {total_open} total",
    ]
    for p in open_rows:
        mark = f" mark {p['mark_price_cents']:.0f}c" if p["mark_price_cents"] else ""
        lines.append(f"- `{p['market_ticker']}` {p['side'].upper()} x{p['contracts']}"
                     f" @ {p['avg_entry_price_cents']:.1f}c{mark}")
    return "\n".join(lines)


def weekly_report(conn: sqlite3.Connection, reports_dir: str | Path) -> tuple[Path, str]:
    now = datetime.now(timezone.utc)
    since = db.now() - 7 * 86400
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"weekly-{now:%Y-%m-%d}.md"

    per_strategy = []
    for strat in ("A", "B"):
        n_signals = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE strategy=? AND last_seen_ts>=?",
            (strat, since)).fetchone()["c"]
        n_qualified = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE strategy=? AND last_seen_ts>=? "
            "AND (status='qualified' OR paper_traded=1)", (strat, since)).fetchone()["c"]
        settled = conn.execute(
            """SELECT p.* FROM paper_positions p JOIN signals s ON s.id=p.signal_id
               WHERE s.strategy=? AND p.status='settled'""", (strat,)).fetchall()
        wins = sum(1 for p in settled if (p["realized_pnl_cents"] or 0) > 0)
        hit = (wins / len(settled) * 100) if settled else None
        avg_modeled = (sum(p["modeled_edge_cents"] or 0 for p in settled) / len(settled)
                       if settled else None)
        avg_realized = (sum(p["realized_edge_cents"] or 0 for p in settled) / len(settled)
                        if settled else None)
        per_strategy.append((strat, n_signals, n_qualified, len(settled), hit,
                             avg_modeled, avg_realized))

    realized = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_cents),0) v FROM paper_positions "
        "WHERE status='settled'").fetchone()["v"]
    unrealized = conn.execute(
        """SELECT COALESCE(SUM((mark_price_cents - avg_entry_price_cents)
             * contracts - fees_paid_cents), 0) v
           FROM paper_positions WHERE status='open' AND mark_price_cents IS NOT NULL"""
    ).fetchone()["v"]

    # Max drawdown over the daily cumulative equity curve
    curve = conn.execute(
        "SELECT date, COALESCE(realized_cents,0)+COALESCE(unrealized_cents,0) v "
        "FROM pnl_daily ORDER BY date").fetchall()
    peak, max_dd = float("-inf"), 0.0
    for r in curve:
        peak = max(peak, r["v"])
        max_dd = max(max_dd, peak - r["v"])

    biggest = conn.execute(
        "SELECT * FROM paper_positions WHERE status='settled' "
        "ORDER BY realized_pnl_cents DESC LIMIT 3").fetchall()
    worst = conn.execute(
        "SELECT * FROM paper_positions WHERE status='settled' "
        "ORDER BY realized_pnl_cents ASC LIMIT 3").fetchall()

    md = [f"# Weekly report — {now:%Y-%m-%d}", ""]
    md.append("| Strategy | Signals (7d) | Qualified | Settled | Hit rate | Avg modeled edge | Avg realized edge |")
    md.append("|---|---|---|---|---|---|---|")
    for strat, n_sig, n_q, n_settled, hit, am, ar in per_strategy:
        md.append(f"| {strat} | {n_sig} | {n_q} | {n_settled} | "
                  f"{f'{hit:.0f}%' if hit is not None else '—'} | "
                  f"{f'{am:.2f}c' if am is not None else '—'} | "
                  f"{f'{ar:.2f}c' if ar is not None else '—'} |")
    md += ["",
           f"**Cumulative P&L:** realized {_fmt_usd(realized)}, "
           f"unrealized {_fmt_usd(unrealized)}",
           f"**Max drawdown (daily closes):** {_fmt_usd(max_dd)}", "",
           "The modeled-vs-realized edge gap is the key output: it measures "
           "how much edge evaporates between signal and settlement.", ""]
    if biggest:
        md.append("## Biggest winners")
        for p in biggest:
            md.append(f"- [{p['market_ticker']}]({market_link(p['market_ticker'])}) "
                      f"{p['side'].upper()} x{p['contracts']}: "
                      f"{_fmt_usd(p['realized_pnl_cents'])}")
    if worst:
        md.append("## Biggest losers")
        for p in worst:
            md.append(f"- [{p['market_ticker']}]({market_link(p['market_ticker'])}) "
                      f"{p['side'].upper()} x{p['contracts']}: "
                      f"{_fmt_usd(p['realized_pnl_cents'])}")
    text = "\n".join(md) + "\n"
    path.write_text(text)

    summary = (f"Weekly report written to {path.name}. "
               f"Cumulative realized {_fmt_usd(realized)}, "
               f"unrealized {_fmt_usd(unrealized)}, max DD {_fmt_usd(max_dd)}.")
    return path, summary


KIND_LABELS = {
    "complement": "YES+NO priced under $1 in one market",
    "bucket_sum_buy": "event buckets sum under $1 (buy them all)",
    "bucket_sum_sell": "event buckets sum over $1 (fade them all)",
    "monotonicity": "narrower outcome priced above the broader one",
    "cross_platform": "Kalshi vs Polymarket pricing gap",
}


def format_signal_alert(sig_row: sqlite3.Row) -> str:
    """Discord 'callout' for a qualifying signal: the exact plays, prices,
    and size at which the inconsistency was executable when scanned."""
    legs = json.loads(sig_row["legs"])
    provisional = (" — PROVISIONAL PAIR, verification pending, alert only"
                   if sig_row["provisional"] else "")
    label = KIND_LABELS.get(sig_row["kind"], sig_row["kind"])
    lines = [
        f":dart: **CALLOUT — {label}**{provisional}",
        f"Net edge after fees: **{sig_row['edge_cents']:.2f}c/contract "
        f"({sig_row['edge_pct']:.1f}%)** — ${sig_row['edge_usd']:.2f} total "
        f"at up to {sig_row['max_size_contracts']} contracts",
        "**The plays (all legs together, prices as scanned):**",
    ]
    for leg in legs:
        if leg.get("platform") == "polymarket":
            lines.append(f"• Polymarket reference: YES trades at "
                         f"{leg['price_cents']:.1f}c there (no action — "
                         f"comparison leg)")
        else:
            lines.append(f"• **{leg['action'].upper()} {leg['side'].upper()}** "
                         f"`{leg['market_ticker']}` @ ~{leg['price_cents']:.1f}c "
                         f"x{leg['contracts']} — {market_link(leg['market_ticker'])}")
    lines.append(
        "_Not financial advice. Automated structural signal — prices move; "
        "verify the live book fills at these levels (all legs, full size) "
        "before acting. Unproven until paper results are in._")
    return "\n".join(lines)
