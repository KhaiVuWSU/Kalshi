"""Strategy B — Kalshi vs Polymarket pricing gaps on equivalent markets.

Signal when |Kalshi executable - Polymarket executable| exceeds Kalshi fees
+ slippage buffer + min edge, per direction, at executable depth:

  kalshi_cheap: buy YES on Kalshi at its ask; reference exit is Polymarket's
                YES bid (what the same claim sells for there).
  kalshi_rich:  buy NO on Kalshi (economically: sell Kalshi's YES bid);
                reference is Polymarket's YES ask.

The Polymarket side is signal-only in the MVP: the paper trade takes the
Kalshi leg and records the Polymarket reference price for evaluation.
Provisional pairs may alert (clearly labeled) but never paper-trade.
"""
from __future__ import annotations

import logging
import sqlite3

from ..book import OrderBook
from ..config import Config
from ..fees import EdgeResult, FeeSchedule, edge_result, fee_cents_for_fill, qualifies
from .common import Leg, Signal, best_size

log = logging.getLogger(__name__)


def check_pair(pair: sqlite3.Row, kalshi_book: OrderBook, poly_book: OrderBook,
               fees: FeeSchedule, series: str | None, buffer_cents: float,
               kalshi_close: str | None, max_contracts: int = 10_000
               ) -> list[Signal]:
    signals: list[Signal] = []
    provisional = pair["status"] != "confirmed"
    timing_mismatch = _timing_mismatch(kalshi_close, pair["poly_end_date"])

    for direction, k_side, p_action in (("kalshi_cheap", "yes", "sell"),
                                        ("kalshi_rich", "no", "buy")):
        def edge_at(n: int) -> EdgeResult:
            kf = kalshi_book.walk_buy(k_side, n)
            # Reference exit on Polymarket, walked at depth too:
            # kalshi_cheap sells YES there (walk poly YES bids);
            # kalshi_rich's NO position is worth 100 - poly YES ask.
            if p_action == "sell":
                pf = poly_book.walk_sell("yes", n)
                m = min(kf.contracts, pf.contracts)
                if m <= 0:
                    return edge_result(0, 0, 0, 0)
                kf, pf = kalshi_book.walk_buy(k_side, m), poly_book.walk_sell("yes", m)
                proceeds = pf.cost_cents
            else:
                pf = poly_book.walk_buy("yes", n)
                m = min(kf.contracts, pf.contracts)
                if m <= 0:
                    return edge_result(0, 0, 0, 0)
                kf, pf = kalshi_book.walk_buy(k_side, m), poly_book.walk_buy("yes", m)
                proceeds = 100 * m - pf.cost_cents
            fee = fee_cents_for_fill(kf.levels, fees.rate_for(series))
            fee += int(buffer_cents * m)          # slippage buffer as a cost
            return edge_result(m, proceeds, kf.cost_cents, fee)

        breakpoints = ([sum(q for _, q in kalshi_book.asks(k_side)[:i + 1])
                        for i in range(len(kalshi_book.asks(k_side)))]
                       + [sum(q for _, q in poly_book.bids("yes")[:i + 1])
                          for i in range(len(poly_book.bids("yes")))]
                       + [sum(q for _, q in poly_book.asks("yes")[:i + 1])
                          for i in range(len(poly_book.asks("yes")))])
        n = best_size(edge_at, max_contracts, breakpoints)
        if n <= 0:
            continue
        e = edge_at(n)
        if e.net_edge_cents <= 0:
            continue
        kf = kalshi_book.walk_buy(k_side, n)
        p_ref = (poly_book.best_bid("yes") if p_action == "sell"
                 else poly_book.best_ask("yes"))
        signals.append(Signal(
            strategy="B", kind="cross_platform",
            legs=[
                Leg(pair["kalshi_ticker"], k_side, "buy", kf.avg_price_cents, n),
                Leg(pair["poly_condition_id"], "yes", p_action,
                    float(p_ref or 0), n, platform="polymarket"),
            ],
            edge=e,
            provisional=provisional,
            details={
                "direction": direction,
                "pair_id": pair["id"],
                "pair_status": pair["status"],
                "poly_question": pair["poly_question"],
                "poly_reference_cents": p_ref,
                "kalshi_yes_bid": kalshi_book.best_bid("yes"),
                "kalshi_yes_ask": kalshi_book.best_ask("yes"),
                "poly_yes_bid": poly_book.best_bid("yes"),
                "poly_yes_ask": poly_book.best_ask("yes"),
                "resolution_timing_mismatch": timing_mismatch,
            }))
    return signals


def _timing_mismatch(kalshi_close: str | None, poly_end: str | None) -> bool:
    from ..matching.matcher import parse_date
    dk, dp = parse_date(kalshi_close), parse_date(poly_end)
    if not dk or not dp:
        return True
    return dk.date() != dp.date()


def scan(conn: sqlite3.Connection, cfg: Config, fees: FeeSchedule,
         kalshi_books: dict[str, OrderBook],
         poly_books: dict[str, OrderBook]) -> list[Signal]:
    """poly_books is keyed by poly_token_id_yes."""
    signals: list[Signal] = []
    pairs = conn.execute(
        "SELECT * FROM market_pairs WHERE status IN ('confirmed','provisional') "
        "AND poly_token_id_yes IS NOT NULL").fetchall()
    for pair in pairs:
        kb = kalshi_books.get(pair["kalshi_ticker"])
        pb = poly_books.get(pair["poly_token_id_yes"])
        if kb is None or pb is None:
            continue
        row = conn.execute(
            "SELECT series_ticker, close_time, status FROM markets WHERE ticker=?",
            (pair["kalshi_ticker"],)).fetchone()
        if row is None or row["status"] not in ("open", "active"):
            continue
        signals.extend(check_pair(
            pair, kb, pb, fees, row["series_ticker"],
            cfg.cross_platform_slippage_buffer_cents, row["close_time"]))
    return [s for s in signals
            if qualifies(s.edge, cfg.min_edge_pct, cfg.min_edge_usd)]
