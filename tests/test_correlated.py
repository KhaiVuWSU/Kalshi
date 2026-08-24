"""Strategy A detectors against hand-built fixture books, including
near-miss cases that must NOT signal."""
from src import db
from src.book import OrderBook
from src.fees import FeeSchedule
from src.strategies import correlated
from src.config import Config
from tests.conftest import make_event, make_market

FEES = FeeSchedule.from_config(Config())


def book(yes_bids=(), no_bids=()):
    return OrderBook(yes_bids=[list(l) for l in yes_bids],
                     no_bids=[list(l) for l in no_bids])


# --------------------------- complement ---------------------------------

def test_complement_violation_detected():
    # yes ask 48, no ask 48: pair costs 96c, pays 100c; fees 2x175=350 on 100
    b = book(yes_bids=[(52, 100)], no_bids=[(52, 100)])
    sig = correlated.check_complement("MKT", b, FEES, None, 10_000)
    assert sig is not None
    assert sig.kind == "complement"
    assert sig.edge.net_edge_cents == 100 * 100 - 9600 - 350  # 50c net
    assert {(l.side, l.action) for l in sig.legs} == {("yes", "buy"), ("no", "buy")}


def test_complement_near_miss_fees_eat_it():
    # asks 49 + 50 = 99c: 1c gross/contract but fees are ~3.5c/contract
    b = book(yes_bids=[(50, 100)], no_bids=[(51, 100)])
    assert correlated.check_complement("MKT", b, FEES, None, 10_000) is None


def test_complement_exact_dollar_not_a_signal():
    b = book(yes_bids=[(50, 100)], no_bids=[(50, 100)])
    assert correlated.check_complement("MKT", b, FEES, None, 10_000) is None


# --------------------------- bucket sums --------------------------------

def _members(*books):
    return [(f"B{i}", b, None) for i, b in enumerate(books)]


def test_bucket_sell_all_violation():
    # yes bids 40/40/38 sum=118 > 100: buy NO everywhere
    members = _members(book(yes_bids=[(40, 100)]),
                       book(yes_bids=[(40, 100)]),
                       book(yes_bids=[(38, 100)]))
    sig = correlated.check_bucket_sum("EV", members, FEES, 1.0, 10_000, "sell")
    assert sig is not None
    assert sig.kind == "bucket_sum_sell"
    assert sig.edge.net_edge_cents == 20000 - 18200 - 501  # 1299c
    assert all(l.side == "no" and l.action == "buy" for l in sig.legs)


def test_bucket_sell_near_miss_not_signaled():
    # bids sum 102 > 100 — looks like a violation, but fees make it negative
    members = _members(book(yes_bids=[(35, 100)]),
                       book(yes_bids=[(35, 100)]),
                       book(yes_bids=[(32, 100)]))
    assert correlated.check_bucket_sum("EV", members, FEES, 1.0, 10_000, "sell") is None


def test_bucket_buy_all_violation():
    # yes asks 30/30/30 sum=90 < 100
    members = _members(book(no_bids=[(70, 100)]),
                       book(no_bids=[(70, 100)]),
                       book(no_bids=[(70, 100)]))
    sig = correlated.check_bucket_sum("EV", members, FEES, 1.0, 10_000, "buy")
    assert sig is not None
    assert sig.edge.net_edge_cents == 10000 - 9000 - 441  # 559c


def test_bucket_buy_respects_buffer():
    # asks 30/30/31 sum 91: net edge 4.56c/contract after fees.
    # Signals with a 1c buffer, suppressed by a 5c buffer.
    members = _members(book(no_bids=[(70, 100)]),
                       book(no_bids=[(70, 100)]),
                       book(no_bids=[(69, 100)]))
    assert correlated.check_bucket_sum("EV", members, FEES, 1.0, 10_000, "buy") is not None
    assert correlated.check_bucket_sum("EV", members, FEES, 5.0, 10_000, "buy") is None


def test_bucket_single_market_ignored():
    assert correlated.check_bucket_sum(
        "EV", _members(book(no_bids=[(70, 100)])), FEES, 1.0, 10_000, "buy") is None


# --------------------------- monotonicity -------------------------------

def test_monotonicity_violation():
    # A (narrower) yes bid 62; B (broader) yes ask 52 => A priced above B
    book_a = book(yes_bids=[(62, 100)])
    book_b = book(no_bids=[(48, 100)])
    sig = correlated.check_monotonicity("A", "B", book_a, book_b, FEES,
                                        None, None, 10_000)
    assert sig is not None
    assert sig.edge.net_edge_cents == 10000 - 9000 - 340  # 660c
    legs = {l.market_ticker: l for l in sig.legs}
    assert legs["B"].side == "yes" and legs["B"].action == "buy"
    assert legs["A"].side == "no" and legs["A"].action == "buy"


def test_monotonicity_near_miss():
    # 1c apparent violation, killed by fees
    book_a = book(yes_bids=[(55, 100)])
    book_b = book(no_bids=[(46, 100)])
    assert correlated.check_monotonicity("A", "B", book_a, book_b, FEES,
                                         None, None, 10_000) is None


def test_monotonicity_consistent_prices_no_signal():
    book_a = book(yes_bids=[(30, 100)])   # narrower cheaper: consistent
    book_b = book(no_bids=[(40, 100)])    # broader ask 60
    assert correlated.check_monotonicity("A", "B", book_a, book_b, FEES,
                                         None, None, 10_000) is None


# ----------------------- nesting inference ------------------------------

def _row(conn, ticker):
    return conn.execute("SELECT * FROM markets WHERE ticker=?", (ticker,)).fetchone()


def test_provable_nested_pairs_greater_ladder(conn):
    make_market(conn, "T5", event_ticker="EV", strike_type="greater", floor_strike=5.0)
    make_market(conn, "T3", event_ticker="EV", strike_type="greater", floor_strike=3.0)
    rows = [_row(conn, "T5"), _row(conn, "T3")]
    assert ("T5", "T3") in correlated.provable_nested_pairs(rows)
    assert ("T3", "T5") not in correlated.provable_nested_pairs(rows)


def test_provable_nested_pairs_between_containment(conn):
    make_market(conn, "IN", event_ticker="EV", strike_type="between",
                floor_strike=3.0, cap_strike=4.0)
    make_market(conn, "OUT", event_ticker="EV", strike_type="between",
                floor_strike=2.0, cap_strike=5.0)
    rows = [_row(conn, "IN"), _row(conn, "OUT")]
    assert ("IN", "OUT") in correlated.provable_nested_pairs(rows)


def test_mixed_strike_types_never_inferred(conn):
    make_market(conn, "G", event_ticker="EV", strike_type="greater", floor_strike=3.0)
    make_market(conn, "B", event_ticker="EV", strike_type="between",
                floor_strike=4.0, cap_strike=9.0)
    rows = [_row(conn, "G"), _row(conn, "B")]
    assert correlated.provable_nested_pairs(rows) == []


def test_cross_event_nesting_goes_to_candidates_not_scan(conn, cfg):
    # Same series, same strike shape, different events/close times:
    # candidate only — never scanned until confirmed.
    make_market(conn, "SER-MAR", event_ticker="SER-26MAR", series_ticker="SER",
                close_time="2026-03-31T00:00:00Z")
    make_market(conn, "SER-JUN", event_ticker="SER-26JUN", series_ticker="SER",
                close_time="2026-06-30T00:00:00Z")
    added = correlated.generate_relationship_candidates(conn)
    assert added >= 1
    cand = conn.execute("SELECT * FROM relationship_candidates "
                        "WHERE kind='nested'").fetchone()
    assert cand["status"] == "pending"
    assert cand["narrower_ticker"] == "SER-MAR"
    assert cand["broader_ticker"] == "SER-JUN"

    # Books with a blatant violation — but the relationship is unconfirmed.
    books = {"SER-MAR": book(yes_bids=[(70, 100)]),
             "SER-JUN": book(no_bids=[(60, 100)])}
    assert correlated.scan(conn, cfg, FEES, books) == []

    # Confirm it -> now it scans.
    conn.execute("UPDATE relationship_candidates SET status='confirmed' WHERE id=?",
                 (cand["id"],))
    conn.commit()
    sigs = correlated.scan(conn, cfg, FEES, books)
    assert [s.kind for s in sigs] == ["monotonicity"]


def test_candidate_generation_idempotent(conn):
    make_market(conn, "SER-MAR", event_ticker="SER-26MAR", series_ticker="SER",
                close_time="2026-03-31T00:00:00Z")
    make_market(conn, "SER-JUN", event_ticker="SER-26JUN", series_ticker="SER",
                close_time="2026-06-30T00:00:00Z")
    correlated.generate_relationship_candidates(conn)
    n1 = conn.execute("SELECT COUNT(*) c FROM relationship_candidates").fetchone()["c"]
    correlated.generate_relationship_candidates(conn)
    n2 = conn.execute("SELECT COUNT(*) c FROM relationship_candidates").fetchone()["c"]
    assert n1 == n2


# ----------------------- scan orchestration -----------------------------

def test_bucket_buy_requires_confirmed_exhaustiveness(conn, cfg):
    make_event(conn, "EV-B", mutually_exclusive=True)
    for i in range(3):
        make_market(conn, f"BK{i}", event_ticker="EV-B")
    books = {f"BK{i}": book(no_bids=[(70, 100)], yes_bids=[(5, 100)])
             for i in range(3)}
    sigs = correlated.scan(conn, cfg, FEES, books)
    assert "bucket_sum_buy" not in {s.kind for s in sigs}

    correlated.generate_relationship_candidates(conn)
    conn.execute("UPDATE relationship_candidates SET status='confirmed' "
                 "WHERE kind='exhaustive_event' AND event_ticker='EV-B'")
    conn.commit()
    sigs = correlated.scan(conn, cfg, FEES, books)
    assert "bucket_sum_buy" in {s.kind for s in sigs}


def test_bucket_sum_skipped_when_bucket_book_missing(conn, cfg):
    make_event(conn, "EV-B", mutually_exclusive=True)
    for i in range(3):
        make_market(conn, f"BK{i}", event_ticker="EV-B")
    # Only 2 of 3 open buckets have books: sums are meaningless -> no signal.
    books = {f"BK{i}": book(yes_bids=[(60, 100)]) for i in range(2)}
    sigs = correlated.scan(conn, cfg, FEES, books)
    assert "bucket_sum_sell" not in {s.kind for s in sigs}


def test_scan_applies_edge_thresholds(conn, cfg):
    # Real violation but tiny (50c net on $99.50 at risk = 0.5% < 2%)
    make_market(conn, "SMALL", event_ticker="EV-S")
    books = {"SMALL": book(yes_bids=[(52, 100)], no_bids=[(52, 100)])}
    assert correlated.scan(conn, cfg, FEES, books) == []
