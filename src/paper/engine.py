"""Paper trading: simulated fills against real orderbook depth, positions,
mark-to-market, and settlement against actual market resolutions.

The MVP NEVER places a live order; see place_live_order below.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from .. import db
from ..book import OrderBook
from ..clients.kalshi import KalshiClient
from ..config import Config
from ..fees import FeeSchedule, fee_cents_for_fill
from ..strategies.common import Signal

log = logging.getLogger(__name__)


def place_live_order(*args, **kwargs):
    """Live execution is a future phase, gated behind config.live_trading.
    The MVP observes, signals, and paper-trades only."""
    raise NotImplementedError(
        "Live trading is not implemented in the MVP. Set live_trading: false.")


def execute_signal(conn: sqlite3.Connection, cfg: Config, fees: FeeSchedule,
                   signal_id: int, sig: Signal,
                   books: dict[str, OrderBook]) -> list[int]:
    """Open paper positions for a qualified, confirmed signal.

    Fills walk the stored book through levels (never top-of-book only).
    Size cap: min(signal's executable size, max_paper_size_usd across all
    legs, depth_participation_cap x visible depth per leg).
    Returns created position ids ([] if nothing could be filled).
    """
    if sig.provisional:
        return []
    kalshi_legs = [l for l in sig.legs if l.platform == "kalshi"]
    if not kalshi_legs:
        return []

    # Per-leg depth caps
    n_cap = min(l.contracts for l in kalshi_legs)
    for leg in kalshi_legs:
        book = books.get(leg.market_ticker)
        if book is None:
            log.warning("no book for %s; skipping paper trade", leg.market_ticker)
            return []
        depth = book.depth_contracts(leg.side, leg.action)
        n_cap = min(n_cap, int(depth * cfg.depth_participation_cap))
    if n_cap <= 0:
        return []

    # Budget cap: total cost across legs (at walked prices) <= max_paper_size_usd
    budget_cents = int(cfg.max_paper_size_usd * 100)

    def total_cost(n: int) -> int | None:
        cost = 0
        for leg in kalshi_legs:
            fill = books[leg.market_ticker].walk_buy(leg.side, n)
            if fill.contracts < n:
                return None
            cost += fill.cost_cents
        return cost

    n = n_cap
    while n > 0:
        c = total_cost(n)
        if c is not None and c <= budget_cents:
            break
        n -= max(1, n // 10)
    if n <= 0:
        return []

    position_ids: list[int] = []
    series_by_ticker = _series_map(conn, [l.market_ticker for l in kalshi_legs])
    per_contract_edge = sig.edge.per_contract_net_cents
    for leg in kalshi_legs:
        book = books[leg.market_ticker]
        fill = book.walk_buy(leg.side, n)
        fee = fee_cents_for_fill(
            fill.levels, fees.rate_for(series_by_ticker.get(leg.market_ticker)))
        conn.execute(
            """INSERT INTO paper_orders(signal_id, market_ticker, side, action,
                 contracts, avg_price_cents, fees_cents, fill_levels, ts)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (signal_id, leg.market_ticker, leg.side, "buy", fill.contracts,
             fill.avg_price_cents, fee, json.dumps(fill.levels), db.now()))
        cur = conn.execute(
            """INSERT INTO paper_positions(signal_id, market_ticker, side,
                 contracts, avg_entry_price_cents, fees_paid_cents,
                 modeled_edge_pct, modeled_edge_cents, status, opened_ts)
               VALUES(?,?,?,?,?,?,?,?, 'open', ?)""",
            (signal_id, leg.market_ticker, leg.side, fill.contracts,
             fill.avg_price_cents, fee, sig.edge.net_edge_pct,
             per_contract_edge, db.now()))
        position_ids.append(cur.lastrowid)
    conn.execute("UPDATE signals SET paper_traded=1 WHERE id=?", (signal_id,))
    conn.commit()
    log.info("paper trade opened: signal=%s legs=%d contracts=%d",
             signal_id, len(kalshi_legs), n)
    return position_ids


def _series_map(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, str | None]:
    if not tickers:
        return {}
    rows = conn.execute(
        f"SELECT ticker, series_ticker FROM markets "
        f"WHERE ticker IN ({','.join('?' * len(tickers))})", tickers).fetchall()
    return {r["ticker"]: r["series_ticker"] for r in rows}


def mark_positions(conn: sqlite3.Connection, max_snapshot_age_s: float = 3600) -> int:
    """Mark open positions at the executable exit (best bid for the held
    side) from the latest stored snapshot."""
    n = 0
    for pos in conn.execute(
            "SELECT * FROM paper_positions WHERE status='open'").fetchall():
        snap = db.latest_snapshot(conn, pos["market_ticker"],
                                  max_age_s=max_snapshot_age_s)
        if snap is None:
            continue
        book = OrderBook(yes_bids=json.loads(snap["yes_bids"]),
                         no_bids=json.loads(snap["no_bids"]))
        bid = book.best_bid(pos["side"])
        if bid is None:
            continue
        conn.execute(
            "UPDATE paper_positions SET mark_price_cents=?, marked_ts=? WHERE id=?",
            (bid, db.now(), pos["id"]))
        n += 1
    conn.commit()
    return n


async def settle_positions(conn: sqlite3.Connection,
                           client: KalshiClient) -> int:
    """Realize P&L for positions whose market has resolved."""
    n = 0
    tickers = [r["market_ticker"] for r in conn.execute(
        "SELECT DISTINCT market_ticker FROM paper_positions WHERE status='open'")]
    for ticker in tickers:
        try:
            m = await client.get_market(ticker)
        except Exception as exc:
            log.warning("settle: fetch %s failed: %s", ticker, exc)
            continue
        status = (m.get("status") or "").lower()
        result = (m.get("result") or "").lower()
        if status not in ("settled", "finalized") or result not in ("yes", "no"):
            continue
        for pos in conn.execute(
                "SELECT * FROM paper_positions WHERE status='open' AND market_ticker=?",
                (ticker,)).fetchall():
            settle_price = 100 if pos["side"] == result else 0
            pnl = ((settle_price - pos["avg_entry_price_cents"]) * pos["contracts"]
                   - pos["fees_paid_cents"])
            realized_edge = pnl / pos["contracts"] if pos["contracts"] else 0.0
            conn.execute(
                """UPDATE paper_positions SET status='settled', settled_ts=?,
                     settle_price_cents=?, realized_pnl_cents=?,
                     realized_edge_cents=? WHERE id=?""",
                (db.now(), settle_price, pnl, realized_edge, pos["id"]))
            n += 1
    conn.commit()
    return n


def update_daily_pnl(conn: sqlite3.Connection, date_utc: str,
                     signals_a: int = 0, signals_b: int = 0) -> None:
    realized = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_cents),0) v FROM paper_positions "
        "WHERE status='settled'").fetchone()["v"]
    unrealized = conn.execute(
        """SELECT COALESCE(SUM((mark_price_cents - avg_entry_price_cents)
             * contracts - fees_paid_cents), 0) v
           FROM paper_positions WHERE status='open' AND mark_price_cents IS NOT NULL"""
    ).fetchone()["v"]
    fees_total = conn.execute(
        "SELECT COALESCE(SUM(fees_paid_cents),0) v FROM paper_positions").fetchone()["v"]
    open_n = conn.execute(
        "SELECT COUNT(*) c FROM paper_positions WHERE status='open'").fetchone()["c"]
    conn.execute(
        """INSERT INTO pnl_daily(date, realized_cents, unrealized_cents,
             fees_cents, open_positions, signals_a, signals_b)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET realized_cents=excluded.realized_cents,
             unrealized_cents=excluded.unrealized_cents,
             fees_cents=excluded.fees_cents,
             open_positions=excluded.open_positions,
             signals_a=MAX(pnl_daily.signals_a, excluded.signals_a),
             signals_b=MAX(pnl_daily.signals_b, excluded.signals_b)""",
        (date_utc, realized, unrealized, fees_total, open_n, signals_a, signals_b))
    conn.commit()
