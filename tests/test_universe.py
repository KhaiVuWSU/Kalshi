import httpx
import pytest

from src.config import Config
from src.ingest import universe


class StubKalshi:
    """Minimal stand-in for KalshiClient used by sync_universe."""

    def __init__(self, markets, events, series_404=()):
        self.markets = markets
        self.events = events
        self.series_404 = set(series_404)
        self.series_calls: list[str] = []

    async def iter_events(self, status="open"):
        for e in self.events:
            yield e

    async def iter_markets(self, status="open"):
        for m in self.markets:
            yield m

    async def get_series(self, st):
        self.series_calls.append(st)
        if st in self.series_404:
            req = httpx.Request("GET", f"https://x/series/{st}")
            raise httpx.HTTPStatusError(
                "404", request=req, response=httpx.Response(404, request=req))
        return {"ticker": st, "title": st}

    async def get_market(self, ticker):
        for m in self.markets:
            if m["ticker"] == ticker:
                return m
        return {}


MARKETS = [
    {"ticker": "GOOD-1", "event_ticker": "GOOD-EV", "series_ticker": "GOOD",
     "status": "open", "volume": 10},
    {"ticker": "MISSING-1", "event_ticker": "MISSING-EV",
     "series_ticker": "MISSING", "status": "open", "volume": 10},
]
EVENTS = [{"event_ticker": "GOOD-EV", "series_ticker": "GOOD"},
          {"event_ticker": "MISSING-EV", "series_ticker": "MISSING"}]


async def test_series_404_cached_not_refetched(conn):
    client = StubKalshi(MARKETS, EVENTS, series_404={"MISSING"})
    cfg = Config()
    await universe.sync_universe(conn, client, cfg)
    await universe.sync_universe(conn, client, cfg)
    # Each series asked for exactly once across both syncs: the real one is
    # stored, the 404 is cached as a stub row.
    assert client.series_calls.count("MISSING") == 1
    assert client.series_calls.count("GOOD") == 1
    row = conn.execute("SELECT * FROM series WHERE ticker='MISSING'").fetchone()
    assert row is not None and row["title"] is None


async def test_sync_idempotent_market_counts(conn):
    client = StubKalshi(MARKETS, EVENTS)
    cfg = Config()
    s1 = await universe.sync_universe(conn, client, cfg)
    s2 = await universe.sync_universe(conn, client, cfg)
    assert s1["markets"] == s2["markets"] == 2
    n = conn.execute("SELECT COUNT(*) c FROM markets").fetchone()["c"]
    assert n == 2
