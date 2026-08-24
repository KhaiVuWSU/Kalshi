"""Orderbook snapshots for the tracked-market set.

Default transport is REST polling every `snapshot_seconds`. The WebSocket
feed (clients.kalshi.KalshiWebSocket) is a drop-in alternative: it calls the
same `store_book` sink, so the rest of the system doesn't care which
transport produced a snapshot.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from ..clients.kalshi import KalshiClient
from ..config import Config
from .. import db

log = logging.getLogger(__name__)


def store_book(conn: sqlite3.Connection, ticker: str, yes_bids: list,
               no_bids: list, depth_levels: int, source: str = "kalshi") -> None:
    yes = sorted(((int(p), int(q)) for p, q in yes_bids), key=lambda l: -l[0])
    no = sorted(((int(p), int(q)) for p, q in no_bids), key=lambda l: -l[0])
    db.insert_snapshot(conn, ticker, yes[:depth_levels], no[:depth_levels],
                       source=source)


async def snapshot_cycle(conn: sqlite3.Connection, client: KalshiClient,
                         cfg: Config, tickers: list[str]) -> int:
    """One REST polling pass over the tracked set. Returns snapshot count."""
    sem = asyncio.Semaphore(cfg.snapshot_concurrency)
    results: list[tuple[str, list, list]] = []

    async def fetch(ticker: str) -> None:
        async with sem:
            try:
                payload = await client.get_orderbook(
                    ticker, depth=cfg.orderbook_depth_levels)
            except Exception as exc:
                log.warning("orderbook fetch failed for %s: %s", ticker, exc)
                return
            ob = payload.get("orderbook") or {}
            results.append((ticker, ob.get("yes") or [], ob.get("no") or []))

    await asyncio.gather(*(fetch(t) for t in tickers))
    for ticker, yes, no in results:
        store_book(conn, ticker, yes, no, cfg.orderbook_depth_levels)
    conn.commit()
    return len(results)
