"""Full market-universe sync into SQLite, and the tracked-market set."""
from __future__ import annotations

import logging
import sqlite3

from ..clients.kalshi import KalshiClient
from ..config import Config
from .. import db

log = logging.getLogger(__name__)


async def sync_universe(conn: sqlite3.Connection, client: KalshiClient,
                        cfg: Config) -> dict:
    """Idempotent full sync of open events + markets (+ new series metadata).

    Markets that were open locally but no longer appear in the open set are
    re-fetched individually so their status/result update (needed for paper
    settlement).
    """
    seen_markets: set[str] = set()
    seen_series: set[str] = set()
    n_markets = n_events = 0

    async for e in client.iter_events(status="open"):
        db.upsert_event(conn, e)
        n_events += 1
    conn.commit()

    prev_open = {r["ticker"] for r in conn.execute(
        "SELECT ticker FROM markets WHERE status IN ('open','active')")}

    async for m in client.iter_markets(status="open"):
        db.upsert_market(conn, m)
        seen_markets.add(m.get("ticker"))
        st = m.get("series_ticker") or db._series_from_event(m.get("event_ticker"))
        if st:
            seen_series.add(st)
        n_markets += 1
        if n_markets % 2000 == 0:
            conn.commit()
    conn.commit()

    # Fetch series metadata we don't have yet (fee schedule class, category).
    known = {r["ticker"] for r in conn.execute("SELECT ticker FROM series")}
    for st in sorted(seen_series - known):
        try:
            s = await client.get_series(st)
            if s:
                db.upsert_series(conn, s)
        except Exception as exc:
            log.warning("series fetch failed for %s: %s", st, exc)
    conn.commit()

    # Markets that disappeared from the open set: refresh their real status.
    vanished = prev_open - seen_markets
    for ticker in sorted(vanished):
        try:
            m = await client.get_market(ticker)
            if m:
                db.upsert_market(conn, m)
        except Exception as exc:
            log.warning("refresh of closed market %s failed: %s", ticker, exc)
    conn.commit()

    stats = {"events": n_events, "markets": n_markets, "closed": len(vanished)}
    log.info("universe sync: %s", stats)
    return stats


def tracked_market_tickers(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    """Top-N open markets by volume, plus anything referenced by an active
    signal, an open paper position, or a confirmed/provisional pair."""
    top = [r["ticker"] for r in conn.execute(
        "SELECT ticker FROM markets WHERE status IN ('open','active') "
        "ORDER BY COALESCE(volume,0) DESC LIMIT ?", (cfg.tracked_top_n,))]
    tracked = dict.fromkeys(top)  # preserves order, dedupes

    for r in conn.execute(
            "SELECT legs FROM signals WHERE status IN ('pending','qualified')"):
        import json
        for leg in json.loads(r["legs"] or "[]"):
            if leg.get("platform", "kalshi") == "kalshi":
                tracked.setdefault(leg["market_ticker"])
    for r in conn.execute(
            "SELECT DISTINCT market_ticker FROM paper_positions WHERE status='open'"):
        tracked.setdefault(r["market_ticker"])
    for r in conn.execute(
            "SELECT DISTINCT kalshi_ticker FROM market_pairs "
            "WHERE status IN ('confirmed','provisional')"):
        tracked.setdefault(r["kalshi_ticker"])
    return list(tracked)
