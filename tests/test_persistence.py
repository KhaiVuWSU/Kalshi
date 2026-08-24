"""Persistence gate: a signal must survive N consecutive scans; restarts and
gaps reset the streak; expiry kills stale signals; no duplicate rows ever."""
from src import db
from src.book import OrderBook
from src.fees import edge_result
from src.strategies.common import Leg, Signal, expire_stale_signals, record_signal


def sig(edge_cents=500):
    return Signal(strategy="A", kind="complement",
                  legs=[Leg("MKT", "yes", "buy", 48.0, 100),
                        Leg("MKT", "no", "buy", 48.0, 100)],
                  edge=edge_result(100, 10000, 9600, 350))


def test_qualifies_after_n_consecutive_scans(conn):
    for i in range(3):
        seq = db.next_scan_seq(conn)
        sid, newly, consec = record_signal(conn, sig(), seq, persistence_scans=3)
        assert consec == i + 1
        assert newly == (i == 2)
    row = conn.execute("SELECT * FROM signals").fetchone()
    assert row["status"] == "qualified"
    assert conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 1


def test_gap_resets_streak(conn):
    record_signal(conn, sig(), db.next_scan_seq(conn), 3)
    record_signal(conn, sig(), db.next_scan_seq(conn), 3)
    db.next_scan_seq(conn)  # a scan where the signal was NOT seen
    _, newly, consec = record_signal(conn, sig(), db.next_scan_seq(conn), 3)
    assert consec == 1 and not newly


def test_restart_resets_streak(conn):
    # Two sightings before a crash...
    record_signal(conn, sig(), db.next_scan_seq(conn), 3)
    record_signal(conn, sig(), db.next_scan_seq(conn), 3)
    # ...restart burns one seq (App.run), so the streak cannot continue.
    db.next_scan_seq(conn)
    _, newly, consec = record_signal(conn, sig(), db.next_scan_seq(conn), 3)
    assert consec == 1
    assert not newly
    assert conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 1


def test_duplicate_within_one_pass_ignored(conn):
    seq = db.next_scan_seq(conn)
    record_signal(conn, sig(), seq, 3)
    _, newly, consec = record_signal(conn, sig(), seq, 3)
    assert consec == 1 and not newly
    assert conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 1


def test_expiry_and_requalification(conn):
    for _ in range(3):
        record_signal(conn, sig(), db.next_scan_seq(conn), 3)
    seq = db.next_scan_seq(conn)          # pass without the signal
    assert expire_stale_signals(conn, seq) == 1
    assert conn.execute("SELECT status FROM signals").fetchone()["status"] == "expired"
    # Coming back starts a fresh streak and can newly qualify again.
    for i in range(3):
        _, newly, _ = record_signal(conn, sig(), db.next_scan_seq(conn), 3)
    assert newly


def test_fingerprint_ignores_price_and_size(conn):
    a = sig()
    b = Signal(strategy="A", kind="complement",
               legs=[Leg("MKT", "yes", "buy", 47.0, 55),
                     Leg("MKT", "no", "buy", 46.0, 55)],
               edge=edge_result(55, 5500, 5115, 200))
    assert a.fingerprint == b.fingerprint
    c = Signal(strategy="A", kind="complement",
               legs=[Leg("OTHER", "yes", "buy", 48.0, 100),
                     Leg("OTHER", "no", "buy", 48.0, 100)],
               edge=a.edge)
    assert a.fingerprint != c.fingerprint
