"""Strategy A — intra-Kalshi structural violations.

Three violation classes, all evaluated on executable prices from orderbook
depth and net of taker fees:

1. Nested-window monotonicity: A implies B, but YES(A) is bid above YES(B)'s
   ask. Trade: buy YES on B + buy NO on A -> minimum payout $1.00 per pair.
   Nesting is auto-inferred only where it is provable from metadata
   (same-event strike ladders with the same strike_type); everything else
   goes to relationship_candidates for manual confirmation and is never
   scanned until confirmed.
2. Bucket-sum violations within one event. Sell-all (buy NO on every bucket)
   needs only mutual exclusivity (Kalshi's event flag). Buy-all (buy YES on
   every bucket) additionally requires exhaustiveness, which metadata does
   not assert — so buy-all runs only for events confirmed exhaustive via the
   CLI (relationship_candidates kind='exhaustive_event').
3. Complement: buy YES + buy NO in the same market for < $1.00 - fees.
"""
from __future__ import annotations

import itertools
import logging
import sqlite3

from .. import db
from ..book import OrderBook
from ..config import Config
from ..fees import EdgeResult, FeeSchedule, edge_result, fee_cents_for_fill, qualifies
from .common import Leg, Signal, best_size

log = logging.getLogger(__name__)

MAX_NESTED_CANDIDATES_PER_SERIES = 40


# --------------------------------------------------------------------------
# Relationship inference / candidate generation
# --------------------------------------------------------------------------

_GREATER = ("greater", "greater_or_equal")
_LESS = ("less", "less_or_equal")


def provable_nested_pairs(markets: list[sqlite3.Row]) -> list[tuple[str, str]]:
    """(narrower, broader) pairs provable from same-event strike structure.

    Only compares markets within one event sharing the exact same
    strike_type, where boundary semantics cancel:
      greater(f_a) implies greater(f_b)  iff f_a >= f_b
      less(c_a) implies less(c_b)        iff c_a <= c_b
      between [f_a,c_a] subset [f_b,c_b] implies containment
    """
    pairs: list[tuple[str, str]] = []
    by_event: dict[str, list[sqlite3.Row]] = {}
    for m in markets:
        if m["event_ticker"]:
            by_event.setdefault(m["event_ticker"], []).append(m)
    for ms in by_event.values():
        for a, b in itertools.permutations(ms, 2):
            st = a["strike_type"]
            if not st or st != b["strike_type"]:
                continue
            if st in _GREATER:
                fa, fb = a["floor_strike"], b["floor_strike"]
                if fa is not None and fb is not None and fa > fb:
                    pairs.append((a["ticker"], b["ticker"]))
            elif st in _LESS:
                ca, cb = a["cap_strike"], b["cap_strike"]
                if ca is not None and cb is not None and ca < cb:
                    pairs.append((a["ticker"], b["ticker"]))
            elif st == "between":
                fa, ca = a["floor_strike"], a["cap_strike"]
                fb, cb = b["floor_strike"], b["cap_strike"]
                if None in (fa, ca, fb, cb):
                    continue
                if (fa >= fb and ca <= cb) and (fa, ca) != (fb, cb):
                    pairs.append((a["ticker"], b["ticker"]))
    return pairs


def generate_relationship_candidates(conn: sqlite3.Connection) -> int:
    """Write *uncertain* relationships to relationship_candidates (pending).

    Cross-event nesting inside one series (e.g. "by March" vs "by June")
    cannot be proven from metadata, so it is only ever suggested here and
    scanned after manual `confirm-relationship`.
    """
    added = 0
    rows = conn.execute(
        "SELECT ticker, event_ticker, series_ticker, close_time, strike_type, "
        "floor_strike, cap_strike, title, yes_sub_title FROM markets "
        "WHERE status IN ('open','active')").fetchall()

    by_series: dict[str, list[sqlite3.Row]] = {}
    for m in rows:
        if m["series_ticker"]:
            by_series.setdefault(m["series_ticker"], []).append(m)

    for series, ms in by_series.items():
        candidates = 0
        # Same strike shape, different events, different close time: possible
        # cumulative windows ("by <date>").
        key = lambda m: (m["strike_type"] or "", m["floor_strike"], m["cap_strike"])
        ms_sorted = sorted(ms, key=lambda m: (key(m), m["close_time"] or ""))
        for _, group in itertools.groupby(ms_sorted, key=key):
            g = list(group)
            for a, b in zip(g, g[1:]):  # adjacent close times only
                if (a["event_ticker"] == b["event_ticker"]
                        or not a["close_time"] or not b["close_time"]
                        or a["close_time"] == b["close_time"]):
                    continue
                if candidates >= MAX_NESTED_CANDIDATES_PER_SERIES:
                    break
                narrower, broader = (a, b) if a["close_time"] < b["close_time"] else (b, a)
                rationale = (f"same series {series}, same strike shape, "
                             f"close {narrower['close_time']} vs {broader['close_time']}; "
                             f"if cumulative ('by date'), earlier implies later")
                cur = conn.execute(
                    """INSERT OR IGNORE INTO relationship_candidates
                       (kind, narrower_ticker, broader_ticker, event_ticker,
                        rationale, status, created_at)
                       VALUES('nested', ?, ?, '', ?, 'pending', ?)""",
                    (narrower["ticker"], broader["ticker"], rationale, db.now()))
                added += cur.rowcount
                candidates += cur.rowcount

    # Exhaustiveness candidates: mutually exclusive events with >=2 buckets.
    for e in conn.execute(
            "SELECT e.event_ticker, e.title, COUNT(m.ticker) AS n FROM events e "
            "JOIN markets m ON m.event_ticker = e.event_ticker "
            "WHERE e.mutually_exclusive=1 AND m.status IN ('open','active') "
            "GROUP BY e.event_ticker HAVING n >= 2"):
        cur = conn.execute(
            """INSERT OR IGNORE INTO relationship_candidates
               (kind, narrower_ticker, broader_ticker, event_ticker, rationale,
                status, created_at)
               VALUES('exhaustive_event', '', '', ?, ?, 'pending', ?)""",
            (e["event_ticker"],
             f"event '{e['title']}' is mutually exclusive with {e['n']} open "
             f"buckets; confirm buckets are EXHAUSTIVE to enable buy-all scans",
             db.now()))
        added += cur.rowcount
    conn.commit()
    return added


def confirmed_nested_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r["narrower_ticker"], r["broader_ticker"]) for r in conn.execute(
        "SELECT narrower_ticker, broader_ticker FROM relationship_candidates "
        "WHERE kind='nested' AND status='confirmed'")]


def confirmed_exhaustive_events(conn: sqlite3.Connection) -> set[str]:
    return {r["event_ticker"] for r in conn.execute(
        "SELECT event_ticker FROM relationship_candidates "
        "WHERE kind='exhaustive_event' AND status='confirmed'")}


# --------------------------------------------------------------------------
# Violation detectors (pure functions over books; unit-tested on fixtures)
# --------------------------------------------------------------------------

def _leg_fee(fees: FeeSchedule, levels, series: str | None) -> int:
    return fee_cents_for_fill(levels, fees.rate_for(series))


def check_complement(ticker: str, book: OrderBook, fees: FeeSchedule,
                     series: str | None, max_contracts: int) -> Signal | None:
    """Buy YES + buy NO < $1.00 - fees => guaranteed profit at settlement."""
    if book.best_ask("yes") is None or book.best_ask("no") is None:
        return None

    def edge_at(n: int) -> EdgeResult:
        fy, fn = book.walk_buy("yes", n), book.walk_buy("no", n)
        k = min(fy.contracts, fn.contracts)
        if k <= 0:
            return edge_result(0, 0, 0, 0)
        fy, fn = book.walk_buy("yes", k), book.walk_buy("no", k)
        fee = (_leg_fee(fees, fy.levels, series) + _leg_fee(fees, fn.levels, series))
        return edge_result(k, 100 * k, fy.cost_cents + fn.cost_cents, fee)

    breakpoints = _cum_sizes(book.asks("yes")) + _cum_sizes(book.asks("no"))
    n = best_size(edge_at, max_contracts, breakpoints)
    if n <= 0:
        return None
    e = edge_at(n)
    if e.net_edge_cents <= 0:
        return None
    fy, fn = book.walk_buy("yes", n), book.walk_buy("no", n)
    return Signal(
        strategy="A", kind="complement",
        legs=[Leg(ticker, "yes", "buy", fy.avg_price_cents, n),
              Leg(ticker, "no", "buy", fn.avg_price_cents, n)],
        edge=e,
        details={"yes_ask": book.best_ask("yes"), "no_ask": book.best_ask("no")})


def check_bucket_sum(event_ticker: str, members: list[tuple[str, OrderBook, str | None]],
                     fees: FeeSchedule, buffer_cents: float, max_contracts: int,
                     direction: str) -> Signal | None:
    """direction 'buy': buy YES on all buckets (requires exhaustive+exclusive).
    direction 'sell': buy NO on all buckets (requires exclusive only)."""
    k = len(members)
    if k < 2:
        return None
    side = "yes" if direction == "buy" else "no"
    payout_per_contract = 100 if direction == "buy" else (k - 1) * 100
    for _, book, _ in members:
        if book.best_ask(side) is None:
            return None

    def edge_at(n: int) -> EdgeResult:
        fills = [book.walk_buy(side, n) for _, book, _ in members]
        m = min(f.contracts for f in fills)
        if m <= 0:
            return edge_result(0, 0, 0, 0)
        fills = [book.walk_buy(side, m) for _, book, _ in members]
        cost = sum(f.cost_cents for f in fills)
        fee = sum(_leg_fee(fees, f.levels, s)
                  for f, (_, _, s) in zip(fills, members))
        return edge_result(m, payout_per_contract * m, cost, fee)

    breakpoints: list[int] = []
    for _, book, _ in members:
        breakpoints += _cum_sizes(book.asks(side))
    n = best_size(edge_at, max_contracts, breakpoints)
    if n <= 0:
        return None
    e = edge_at(n)
    # Buffer guards against stale books: require edge beyond it, per contract.
    if e.net_edge_cents <= buffer_cents * e.contracts:
        return None
    fills = [book.walk_buy(side, e.contracts) for _, book, _ in members]
    return Signal(
        strategy="A",
        kind="bucket_sum_buy" if direction == "buy" else "bucket_sum_sell",
        legs=[Leg(t, side, "buy", f.avg_price_cents, e.contracts)
              for (t, _, _), f in zip(members, fills)],
        edge=e,
        details={"event_ticker": event_ticker, "buckets": k,
                 "sum_avg_cents": sum(f.avg_price_cents for f in fills)})


def check_monotonicity(narrower: str, broader: str, book_a: OrderBook,
                       book_b: OrderBook, fees: FeeSchedule,
                       series_a: str | None, series_b: str | None,
                       max_contracts: int) -> Signal | None:
    """A implies B. Violation trade: buy YES(B) + buy NO(A); min payout $1.00.
    Profitable iff yes_bid(A) > yes_ask(B) + fees."""
    if book_b.best_ask("yes") is None or book_a.best_ask("no") is None:
        return None

    def edge_at(n: int) -> EdgeResult:
        fb = book_b.walk_buy("yes", n)
        fa = book_a.walk_buy("no", n)
        m = min(fb.contracts, fa.contracts)
        if m <= 0:
            return edge_result(0, 0, 0, 0)
        fb, fa = book_b.walk_buy("yes", m), book_a.walk_buy("no", m)
        fee = (_leg_fee(fees, fb.levels, series_b) + _leg_fee(fees, fa.levels, series_a))
        return edge_result(m, 100 * m, fb.cost_cents + fa.cost_cents, fee)

    breakpoints = _cum_sizes(book_b.asks("yes")) + _cum_sizes(book_a.asks("no"))
    n = best_size(edge_at, max_contracts, breakpoints)
    if n <= 0:
        return None
    e = edge_at(n)
    if e.net_edge_cents <= 0:
        return None
    fb, fa = book_b.walk_buy("yes", n), book_a.walk_buy("no", n)
    return Signal(
        strategy="A", kind="monotonicity",
        legs=[Leg(broader, "yes", "buy", fb.avg_price_cents, n),
              Leg(narrower, "no", "buy", fa.avg_price_cents, n)],
        edge=e,
        details={"narrower": narrower, "broader": broader,
                 "narrower_yes_bid": book_a.best_bid("yes"),
                 "broader_yes_ask": book_b.best_ask("yes")})


def _cum_sizes(ladder: list[tuple[int, int]]) -> list[int]:
    out, total = [], 0
    for _, q in ladder:
        total += q
        out.append(total)
    return out


# --------------------------------------------------------------------------
# Scan orchestration
# --------------------------------------------------------------------------

def scan(conn: sqlite3.Connection, cfg: Config, fees: FeeSchedule,
         books: dict[str, OrderBook]) -> list[Signal]:
    """Run all Strategy A detectors over the given books. Pure read; the
    caller records signals (persistence gate) and triggers paper trades."""
    signals: list[Signal] = []
    max_contracts = 10_000  # sizing is re-capped by the paper engine
    tickers = list(books)
    if not tickers:
        return signals
    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        f"SELECT ticker, event_ticker, series_ticker, strike_type, "
        f"floor_strike, cap_strike, status FROM markets "
        f"WHERE ticker IN ({placeholders})", tickers).fetchall()
    meta = {r["ticker"]: r for r in rows}

    # 3) complement, every tracked market
    for t, book in books.items():
        m = meta.get(t)
        s = check_complement(t, book, fees, m["series_ticker"] if m else None,
                             max_contracts)
        if s:
            signals.append(s)

    # 2) bucket sums per event
    exhaustive = confirmed_exhaustive_events(conn)
    by_event: dict[str, list] = {}
    for t, book in books.items():
        m = meta.get(t)
        if m and m["event_ticker"]:
            by_event.setdefault(m["event_ticker"], []).append(
                (t, book, m["series_ticker"]))
    if by_event:
        ev_rows = conn.execute(
            "SELECT event_ticker, mutually_exclusive FROM events "
            f"WHERE event_ticker IN ({','.join('?' * len(by_event))})",
            list(by_event)).fetchall()
        me = {r["event_ticker"]: bool(r["mutually_exclusive"]) for r in ev_rows}
        for ev, members in by_event.items():
            if len(members) < 2 or not me.get(ev):
                continue
            # Only evaluate when every open bucket of the event has a book;
            # otherwise sums are meaningless.
            n_open = conn.execute(
                "SELECT COUNT(*) AS n FROM markets WHERE event_ticker=? "
                "AND status IN ('open','active')", (ev,)).fetchone()["n"]
            if len(members) != n_open:
                continue
            s = check_bucket_sum(ev, members, fees, cfg.bucket_sum_buffer_cents,
                                 max_contracts, "sell")
            if s:
                signals.append(s)
            if ev in exhaustive:
                s = check_bucket_sum(ev, members, fees,
                                     cfg.bucket_sum_buffer_cents,
                                     max_contracts, "buy")
                if s:
                    signals.append(s)

    # 1) monotonicity: provable same-event ladders + manually confirmed pairs
    market_rows = [meta[t] for t in books if t in meta]
    pairs = provable_nested_pairs(market_rows) + confirmed_nested_pairs(conn)
    seen: set[tuple[str, str]] = set()
    for narrower, broader in pairs:
        if (narrower, broader) in seen:
            continue
        seen.add((narrower, broader))
        if narrower not in books or broader not in books:
            continue
        ma, mb = meta.get(narrower), meta.get(broader)
        s = check_monotonicity(
            narrower, broader, books[narrower], books[broader], fees,
            ma["series_ticker"] if ma else None,
            mb["series_ticker"] if mb else None, max_contracts)
        if s:
            signals.append(s)

    # Threshold gate (persistence gate is applied by the caller on record)
    return [s for s in signals
            if qualifies(s.edge, cfg.min_edge_pct, cfg.min_edge_usd)]
