"""SQLite schema and access. Plain sqlite3, WAL mode, idempotent DDL."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    ticker TEXT PRIMARY KEY,
    title TEXT,
    category TEXT,
    fee_type TEXT,
    raw TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS events (
    event_ticker TEXT PRIMARY KEY,
    series_ticker TEXT,
    title TEXT,
    mutually_exclusive INTEGER,
    strike_date TEXT,
    raw TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    series_ticker TEXT,
    title TEXT,
    yes_sub_title TEXT,
    status TEXT,
    close_time TEXT,
    expiration_time TEXT,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    volume INTEGER,
    open_interest INTEGER,
    liquidity INTEGER,
    yes_bid INTEGER,
    yes_ask INTEGER,
    no_bid INTEGER,
    no_ask INTEGER,
    last_price INTEGER,
    result TEXT,
    rules_primary TEXT,
    rules_secondary TEXT,
    raw TEXT,
    first_seen_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_status_volume ON markets(status, volume);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_ticker TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'kalshi',   -- 'kalshi' | 'polymarket'
    ts REAL NOT NULL,
    yes_bids TEXT NOT NULL,   -- JSON [[price_cents, contracts], ...] best first
    no_bids TEXT NOT NULL     -- JSON, same shape (Polymarket: asks mapped to NO bids)
);
CREATE INDEX IF NOT EXISTS idx_snap_market_ts ON orderbook_snapshots(market_ticker, ts);

CREATE TABLE IF NOT EXISTS relationship_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- 'nested' | 'exhaustive_event'
    narrower_ticker TEXT,           -- nested: A where A implies B
    broader_ticker TEXT,            -- nested: B
    event_ticker TEXT,              -- exhaustive_event: the event
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | rejected
    created_at REAL,
    decided_at REAL,
    UNIQUE(kind, narrower_ticker, broader_ticker, event_ticker)
);

CREATE TABLE IF NOT EXISTS market_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kalshi_ticker TEXT NOT NULL,
    poly_condition_id TEXT NOT NULL,
    poly_token_id_yes TEXT,
    poly_token_id_no TEXT,
    poly_question TEXT,
    poly_end_date TEXT,
    score REAL,
    status TEXT NOT NULL DEFAULT 'provisional',  -- confirmed | provisional | rejected
    source TEXT NOT NULL DEFAULT 'auto',         -- auto | manual
    notes TEXT,
    created_at REAL,
    updated_at REAL,
    UNIQUE(kalshi_ticker, poly_condition_id)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    strategy TEXT NOT NULL,         -- 'A' | 'B'
    kind TEXT NOT NULL,             -- monotonicity | bucket_sum_buy | bucket_sum_sell
                                    -- | complement | cross_platform
    legs TEXT NOT NULL,             -- JSON list of legs (see strategies.common.Leg)
    edge_cents REAL,                -- net edge per contract, cents
    edge_pct REAL,                  -- net edge as % of capital at risk
    edge_usd REAL,                  -- net edge at max executable size, dollars
    max_size_contracts INTEGER,
    max_size_usd REAL,
    details TEXT,                   -- JSON free-form context
    provisional INTEGER NOT NULL DEFAULT 0,  -- 1 = provisional pair, alert-only
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | qualified | expired
    consecutive_scans INTEGER NOT NULL DEFAULT 1,
    last_scan_seq INTEGER,
    first_seen_ts REAL,
    last_seen_ts REAL,
    alerted INTEGER NOT NULL DEFAULT 0,
    paper_traded INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,             -- 'yes' | 'no'
    action TEXT NOT NULL,           -- 'buy' | 'sell'
    contracts INTEGER NOT NULL,
    avg_price_cents REAL NOT NULL,
    fees_cents REAL NOT NULL,
    fill_levels TEXT,               -- JSON [[price_cents, contracts], ...]
    ts REAL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,             -- 'yes' | 'no'
    contracts INTEGER NOT NULL,     -- signed: >0 long the side
    avg_entry_price_cents REAL NOT NULL,
    fees_paid_cents REAL NOT NULL DEFAULT 0,
    modeled_edge_pct REAL,
    modeled_edge_cents REAL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | settled
    opened_ts REAL,
    mark_price_cents REAL,
    marked_ts REAL,
    settled_ts REAL,
    settle_price_cents REAL,
    realized_pnl_cents REAL,
    realized_edge_cents REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON paper_positions(status);

CREATE TABLE IF NOT EXISTS pnl_daily (
    date TEXT PRIMARY KEY,          -- YYYY-MM-DD (UTC)
    realized_cents REAL,
    unrealized_cents REAL,
    fees_cents REAL,
    open_positions INTEGER,
    signals_a INTEGER,
    signals_b INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    started_at REAL,
    last_heartbeat REAL,
    status TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def now() -> float:
    return time.time()


def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def next_scan_seq(conn: sqlite3.Connection) -> int:
    """Monotonic scan counter, persisted so restarts break persistence streaks
    (a restart means we can't prove the signal was continuously present)."""
    seq = int(meta_get(conn, "scan_seq", "0") or 0) + 1
    meta_set(conn, "scan_seq", str(seq))
    return seq


def upsert_series(conn: sqlite3.Connection, s: dict) -> None:
    conn.execute(
        """INSERT INTO series(ticker, title, category, fee_type, raw, updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET title=excluded.title,
             category=excluded.category, fee_type=excluded.fee_type,
             raw=excluded.raw, updated_at=excluded.updated_at""",
        (s.get("ticker"), s.get("title"), s.get("category"),
         s.get("fee_type"), json.dumps(s), now()),
    )


def upsert_event(conn: sqlite3.Connection, e: dict) -> None:
    conn.execute(
        """INSERT INTO events(event_ticker, series_ticker, title,
             mutually_exclusive, strike_date, raw, updated_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(event_ticker) DO UPDATE SET
             series_ticker=excluded.series_ticker, title=excluded.title,
             mutually_exclusive=excluded.mutually_exclusive,
             strike_date=excluded.strike_date, raw=excluded.raw,
             updated_at=excluded.updated_at""",
        (e.get("event_ticker"), e.get("series_ticker"), e.get("title"),
         1 if e.get("mutually_exclusive") else 0, e.get("strike_date"),
         json.dumps(e), now()),
    )


def upsert_market(conn: sqlite3.Connection, m: dict) -> None:
    ts = now()
    conn.execute(
        """INSERT INTO markets(ticker, event_ticker, series_ticker, title,
             yes_sub_title, status, close_time, expiration_time, strike_type,
             floor_strike, cap_strike, volume, open_interest, liquidity,
             yes_bid, yes_ask, no_bid, no_ask, last_price, result,
             rules_primary, rules_secondary, raw, first_seen_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
             event_ticker=excluded.event_ticker,
             series_ticker=excluded.series_ticker,
             title=excluded.title, yes_sub_title=excluded.yes_sub_title,
             status=excluded.status, close_time=excluded.close_time,
             expiration_time=excluded.expiration_time,
             strike_type=excluded.strike_type,
             floor_strike=excluded.floor_strike, cap_strike=excluded.cap_strike,
             volume=excluded.volume, open_interest=excluded.open_interest,
             liquidity=excluded.liquidity, yes_bid=excluded.yes_bid,
             yes_ask=excluded.yes_ask, no_bid=excluded.no_bid,
             no_ask=excluded.no_ask, last_price=excluded.last_price,
             result=excluded.result, rules_primary=excluded.rules_primary,
             rules_secondary=excluded.rules_secondary, raw=excluded.raw,
             updated_at=excluded.updated_at""",
        (m.get("ticker"), m.get("event_ticker"),
         m.get("series_ticker") or _series_from_event(m.get("event_ticker")),
         m.get("title"), m.get("yes_sub_title"), m.get("status"),
         m.get("close_time"), m.get("expiration_time"), m.get("strike_type"),
         m.get("floor_strike"), m.get("cap_strike"), m.get("volume"),
         m.get("open_interest"), m.get("liquidity"), m.get("yes_bid"),
         m.get("yes_ask"), m.get("no_bid"), m.get("no_ask"),
         m.get("last_price"), m.get("result"), m.get("rules_primary"),
         m.get("rules_secondary"), json.dumps(m), ts, ts),
    )


def _series_from_event(event_ticker: str | None) -> str | None:
    # Kalshi event tickers are "<SERIES>-<suffix>"; best-effort fallback when
    # the market payload lacks an explicit series ticker.
    if not event_ticker:
        return None
    return event_ticker.split("-")[0]


def insert_snapshot(conn: sqlite3.Connection, market_ticker: str,
                    yes_bids: list, no_bids: list, source: str = "kalshi",
                    ts: float | None = None) -> None:
    conn.execute(
        "INSERT INTO orderbook_snapshots(market_ticker, source, ts, yes_bids, no_bids) "
        "VALUES(?,?,?,?,?)",
        (market_ticker, source, ts if ts is not None else now(),
         json.dumps(yes_bids), json.dumps(no_bids)),
    )


def latest_snapshot(conn: sqlite3.Connection, market_ticker: str,
                    source: str = "kalshi", max_age_s: float | None = None):
    row = conn.execute(
        "SELECT * FROM orderbook_snapshots WHERE market_ticker=? AND source=? "
        "ORDER BY ts DESC LIMIT 1",
        (market_ticker, source),
    ).fetchone()
    if row is None:
        return None
    if max_age_s is not None and now() - row["ts"] > max_age_s:
        return None
    return row


def heartbeat(conn: sqlite3.Connection, component: str, status: str = "ok",
              detail: str = "") -> None:
    row = conn.execute(
        "SELECT id FROM runs WHERE component=? ORDER BY id DESC LIMIT 1",
        (component,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE runs SET last_heartbeat=?, status=?, detail=? WHERE id=?",
            (now(), status, detail, row["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO runs(component, started_at, last_heartbeat, status, detail) "
            "VALUES(?,?,?,?,?)",
            (component, now(), now(), status, detail),
        )
    conn.commit()
