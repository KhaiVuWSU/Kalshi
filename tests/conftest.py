import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db  # noqa: E402
from src.config import Config  # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def cfg():
    return Config(min_edge_pct=2.0, min_edge_usd=1.00, persistence_scans=3)


def make_market(conn, ticker, event_ticker="EV-1", series_ticker="SER",
                status="open", **kw):
    m = {"ticker": ticker, "event_ticker": event_ticker,
         "series_ticker": series_ticker, "status": status,
         "title": kw.pop("title", ticker), "volume": kw.pop("volume", 1000)}
    m.update(kw)
    db.upsert_market(conn, m)
    conn.commit()
    return m


def make_event(conn, event_ticker="EV-1", series_ticker="SER",
               mutually_exclusive=False, **kw):
    e = {"event_ticker": event_ticker, "series_ticker": series_ticker,
         "title": kw.pop("title", event_ticker),
         "mutually_exclusive": mutually_exclusive}
    e.update(kw)
    db.upsert_event(conn, e)
    conn.commit()
    return e
