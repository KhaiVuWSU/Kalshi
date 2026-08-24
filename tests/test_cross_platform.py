from src import db
from src.book import OrderBook
from src.clients.polymarket import book_to_kalshi_shape, parse_clob_token_ids
from src.config import Config
from src.fees import FeeSchedule
from src.strategies import cross_platform
from tests.conftest import make_market

FEES = FeeSchedule.from_config(Config())


def test_book_to_kalshi_shape_maps_asks_to_no_bids():
    raw = {"bids": [{"price": "0.50", "size": "60"}],
           "asks": [{"price": "0.52", "size": "40"}, {"price": "0.55", "size": "10"}]}
    yes_bids, no_bids = book_to_kalshi_shape(raw)
    assert yes_bids == [[50, 60]]
    assert no_bids == [[48, 40], [45, 10]]     # 100-52, 100-55; best first
    book = OrderBook(yes_bids=yes_bids, no_bids=no_bids)
    assert book.best_ask("yes") == 52
    assert book.best_bid("yes") == 50


def test_parse_clob_token_ids():
    assert parse_clob_token_ids({"clobTokenIds": '["1","2"]'}) == ("1", "2")
    assert parse_clob_token_ids({"clobTokenIds": None}) == (None, None)
    assert parse_clob_token_ids({}) == (None, None)
    assert parse_clob_token_ids({"clobTokenIds": "garbage"}) == (None, None)


def test_parse_clob_token_ids_respects_outcome_order():
    assert parse_clob_token_ids(
        {"clobTokenIds": '["1","2"]', "outcomes": '["Yes","No"]'}) == ("1", "2")
    assert parse_clob_token_ids(
        {"clobTokenIds": '["1","2"]', "outcomes": '["No","Yes"]'}) == ("2", "1")
    # Non-Yes/No binaries (e.g. candidate names) must never be treated as
    # a YES/NO market — that would silently invert or misprice everything.
    assert parse_clob_token_ids(
        {"clobTokenIds": '["1","2"]', "outcomes": '["Smith","Jones"]'}) == (None, None)


def _pair(conn, status="confirmed"):
    conn.execute(
        """INSERT INTO market_pairs(kalshi_ticker, poly_condition_id,
             poly_token_id_yes, poly_token_id_no, poly_question, poly_end_date,
             status, source, created_at, updated_at)
           VALUES('KMKT','0xc','TOK','TOK2','Same question?',
                  '2026-09-17T20:00:00Z',?, 'manual', ?, ?)""",
        (status, db.now(), db.now()))
    conn.commit()
    return conn.execute("SELECT * FROM market_pairs").fetchone()


def test_kalshi_cheap_direction_signals(conn):
    cfg = Config()
    make_market(conn, "KMKT", close_time="2026-09-17T20:00:00Z")
    _pair(conn)
    kalshi_books = {"KMKT": OrderBook(no_bids=[[60, 50]], yes_bids=[[38, 50]])}
    poly_books = {"TOK": OrderBook(yes_bids=[[50, 60]], no_bids=[[48, 60]])}
    sigs = cross_platform.scan(conn, cfg, FEES, kalshi_books, poly_books)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.kind == "cross_platform"
    assert s.details["direction"] == "kalshi_cheap"
    # buy 50 YES on Kalshi @40; poly reference bid 50c
    assert s.edge.net_edge_cents == 2500 - 2000 - 84 - 50
    kalshi_leg = next(l for l in s.legs if l.platform == "kalshi")
    assert (kalshi_leg.side, kalshi_leg.action) == ("yes", "buy")
    assert not s.details["resolution_timing_mismatch"]
    assert not s.provisional


def test_kalshi_rich_direction_signals(conn):
    cfg = Config()
    make_market(conn, "KMKT", close_time="2026-09-17T20:00:00Z")
    _pair(conn)
    # Kalshi YES bid 60 (buy NO @40); Polymarket YES ask 50.
    kalshi_books = {"KMKT": OrderBook(yes_bids=[[60, 50]], no_bids=[[38, 50]])}
    poly_books = {"TOK": OrderBook(yes_bids=[[48, 60]], no_bids=[[50, 60]])}
    sigs = cross_platform.scan(conn, cfg, FEES, kalshi_books, poly_books)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.details["direction"] == "kalshi_rich"
    kalshi_leg = next(l for l in s.legs if l.platform == "kalshi")
    assert (kalshi_leg.side, kalshi_leg.action) == ("no", "buy")
    # proceeds 100n - poly ask cost = 5000-2500; cost 40*50; fee on NO@40 + buffer
    assert s.edge.net_edge_cents == 2500 - 2000 - 84 - 50


def test_no_signal_when_prices_agree(conn):
    cfg = Config()
    make_market(conn, "KMKT", close_time="2026-09-17T20:00:00Z")
    _pair(conn)
    kalshi_books = {"KMKT": OrderBook(yes_bids=[[49, 50]], no_bids=[[49, 50]])}
    poly_books = {"TOK": OrderBook(yes_bids=[[49, 60]], no_bids=[[49, 60]])}
    assert cross_platform.scan(conn, cfg, FEES, kalshi_books, poly_books) == []


def test_provisional_pair_signal_is_labeled_and_not_paper_traded(conn):
    cfg = Config()
    make_market(conn, "KMKT", close_time="2026-09-17T20:00:00Z")
    _pair(conn, status="provisional")
    kalshi_books = {"KMKT": OrderBook(no_bids=[[60, 50]], yes_bids=[[38, 50]])}
    poly_books = {"TOK": OrderBook(yes_bids=[[50, 60]], no_bids=[[48, 60]])}
    sigs = cross_platform.scan(conn, cfg, FEES, kalshi_books, poly_books)
    assert len(sigs) == 1 and sigs[0].provisional

    from src.fees import FeeSchedule as FS
    from src.paper import engine
    created = engine.execute_signal(conn, cfg, FS.from_config(cfg), 1,
                                    sigs[0], kalshi_books)
    assert created == []          # provisional never paper-trades


def test_timing_mismatch_flagged(conn):
    cfg = Config()
    make_market(conn, "KMKT", close_time="2026-09-19T20:00:00Z")  # differs
    _pair(conn)
    kalshi_books = {"KMKT": OrderBook(no_bids=[[60, 50]], yes_bids=[[38, 50]])}
    poly_books = {"TOK": OrderBook(yes_bids=[[50, 60]], no_bids=[[48, 60]])}
    sigs = cross_platform.scan(conn, cfg, FEES, kalshi_books, poly_books)
    assert sigs[0].details["resolution_timing_mismatch"] is True
