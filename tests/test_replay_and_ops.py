"""Replay recorded snapshot sessions through the scanners, and ops-recovery
semantics (kill -9 -> restart -> no duplicate signals, no corrupted state)."""
import json
import sqlite3

from src import db
from src.book import OrderBook
from src.config import Config
from src.fees import FeeSchedule
from src.strategies import correlated
from src.strategies.common import expire_stale_signals, record_signal
from tests.conftest import make_market

FEES = FeeSchedule.from_config(Config())


def books_from_latest_snapshots(conn, tickers):
    books = {}
    for t in tickers:
        snap = db.latest_snapshot(conn, t)
        if snap:
            books[t] = OrderBook(yes_bids=json.loads(snap["yes_bids"]),
                                 no_bids=json.loads(snap["no_bids"]))
    return books


def replay_session(conn, cfg, sessions, tickers):
    """Each session entry: {ticker: (yes_bids, no_bids)}. Runs one scan pass
    per session over stored snapshots, exactly like the live scan loop."""
    qualified = []
    for snapshot_set in sessions:
        for t, (yes, no) in snapshot_set.items():
            db.insert_snapshot(conn, t, yes, no)
        conn.commit()
        books = books_from_latest_snapshots(conn, tickers)
        found = correlated.scan(conn, cfg, FEES, books)
        seq = db.next_scan_seq(conn)
        for sig in found:
            sid, newly, _ = record_signal(conn, sig, seq, cfg.persistence_scans)
            if newly:
                qualified.append(sid)
        expire_stale_signals(conn, seq)
        conn.commit()
    return qualified


def test_replay_detects_planted_violation(conn, cfg):
    make_market(conn, "CMP")
    # 3 consecutive sessions with a planted complement violation
    violated = {"CMP": ([[52, 100]], [[52, 100]])}
    qualified = replay_session(conn, Config(min_edge_pct=0.1, min_edge_usd=0.1),
                               [violated] * 3, ["CMP"])
    assert len(qualified) == 1
    row = conn.execute("SELECT * FROM signals").fetchone()
    assert row["status"] == "qualified" and row["kind"] == "complement"


def test_replay_clean_session_zero_signals(conn, cfg):
    make_market(conn, "CMP")
    clean = {"CMP": ([[45, 100]], [[45, 100]])}   # asks sum to 110: no arb
    qualified = replay_session(conn, cfg, [clean] * 5, ["CMP"])
    assert qualified == []
    assert conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0


def test_replay_flicker_never_qualifies(conn):
    cfg = Config(min_edge_pct=0.1, min_edge_usd=0.1, persistence_scans=3)
    make_market(conn, "CMP")
    violated = {"CMP": ([[52, 100]], [[52, 100]])}
    clean = {"CMP": ([[45, 100]], [[45, 100]])}
    qualified = replay_session(conn, cfg,
                               [violated, violated, clean, violated, violated],
                               ["CMP"])
    assert qualified == []       # never 3 consecutive


def test_kill_restart_no_duplicates_no_corruption(tmp_path):
    """Simulates kill -9 between passes: a second process opens the same DB
    file, re-syncs the same universe, re-scans the same books."""
    db_path = tmp_path / "scanner.db"
    cfg = Config(min_edge_pct=0.1, min_edge_usd=0.1, persistence_scans=3)

    conn1 = db.connect(db_path)
    make_market(conn1, "CMP")
    violated = {"CMP": ([[52, 100]], [[52, 100]])}
    replay_session(conn1, cfg, [violated, violated], ["CMP"])
    # kill -9: no clean shutdown, connection just dropped
    conn1.close()

    conn2 = db.connect(db_path)             # restart: idempotent DDL on live DB
    make_market(conn2, "CMP")               # idempotent re-sync of same market
    db.next_scan_seq(conn2)                 # App.run burns a seq on startup
    qualified = replay_session(conn2, cfg, [violated], ["CMP"])
    row = conn2.execute("SELECT * FROM signals").fetchone()
    # Same fingerprint row, no duplicates; streak restarted (2 pre-kill
    # sightings must not combine with 1 post-restart sighting to qualify).
    assert conn2.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 1
    assert qualified == []
    assert row["consecutive_scans"] == 1
    assert conn2.execute("SELECT COUNT(*) c FROM markets").fetchone()["c"] == 1
    conn2.close()


def test_upsert_market_idempotent_and_updates(conn):
    make_market(conn, "M1", volume=10)
    make_market(conn, "M1", volume=99, status="closed")
    rows = conn.execute("SELECT * FROM markets").fetchall()
    assert len(rows) == 1
    assert rows[0]["volume"] == 99 and rows[0]["status"] == "closed"


def test_snapshot_ordering_and_age_filter(conn):
    db.insert_snapshot(conn, "M1", [[40, 1]], [[50, 1]], ts=db.now() - 7200)
    db.insert_snapshot(conn, "M1", [[41, 1]], [[51, 1]], ts=db.now() - 10)
    latest = db.latest_snapshot(conn, "M1")
    assert json.loads(latest["yes_bids"]) == [[41, 1]]
    assert db.latest_snapshot(conn, "M1", max_age_s=5) is None
    old_only = db.latest_snapshot(conn, "M2")
    assert old_only is None
