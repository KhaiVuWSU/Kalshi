import json

import pytest

from src import db
from src.book import OrderBook
from src.config import Config
from src.fees import FeeSchedule, edge_result
from src.paper import engine
from src.strategies.common import Leg, Signal
from tests.conftest import make_market


def one_leg_signal(contracts=100):
    return Signal(strategy="A", kind="monotonicity",
                  legs=[Leg("MKT", "yes", "buy", 48.0, contracts)],
                  edge=edge_result(contracts, contracts * 100,
                                   contracts * 48, 100))


def test_live_trading_is_a_stub():
    with pytest.raises(NotImplementedError):
        engine.place_live_order()


def test_fill_walks_depth_not_top_of_book(conn):
    cfg = Config(max_paper_size_usd=1000, depth_participation_cap=1.0)
    fees = FeeSchedule.from_config(cfg)
    make_market(conn, "MKT")
    books = {"MKT": OrderBook(no_bids=[[52, 60], [50, 40]])}  # asks 48x60, 50x40
    ids = engine.execute_signal(conn, cfg, fees, 1, one_leg_signal(80), books)
    assert len(ids) == 1
    order = conn.execute("SELECT * FROM paper_orders").fetchone()
    assert order["contracts"] == 80
    assert abs(order["avg_price_cents"] - 48.5) < 1e-9   # crossed two levels
    assert json.loads(order["fill_levels"]) == [[48, 60], [50, 20]]


def test_depth_participation_cap(conn):
    cfg = Config(max_paper_size_usd=1000, depth_participation_cap=0.20)
    fees = FeeSchedule.from_config(cfg)
    make_market(conn, "MKT")
    books = {"MKT": OrderBook(no_bids=[[52, 100]])}
    engine.execute_signal(conn, cfg, fees, 1, one_leg_signal(100), books)
    pos = conn.execute("SELECT * FROM paper_positions").fetchone()
    assert pos["contracts"] == 20      # 20% of 100 visible


def test_budget_cap(conn):
    cfg = Config(max_paper_size_usd=10, depth_participation_cap=1.0)
    fees = FeeSchedule.from_config(cfg)
    make_market(conn, "MKT")
    books = {"MKT": OrderBook(no_bids=[[52, 100]])}   # ask 48c
    engine.execute_signal(conn, cfg, fees, 1, one_leg_signal(100), books)
    pos = conn.execute("SELECT * FROM paper_positions").fetchone()
    assert pos["contracts"] * 48 <= 1000
    assert pos["contracts"] > 0


def test_missing_book_skips_trade(conn):
    cfg = Config()
    fees = FeeSchedule.from_config(cfg)
    assert engine.execute_signal(conn, cfg, fees, 1, one_leg_signal(), {}) == []
    assert conn.execute("SELECT COUNT(*) c FROM paper_positions").fetchone()["c"] == 0


class StubKalshi:
    def __init__(self, markets):
        self.markets = markets

    async def get_market(self, ticker):
        return self.markets[ticker]


async def test_settlement_realizes_pnl(conn):
    conn.execute(
        """INSERT INTO paper_positions(signal_id, market_ticker, side, contracts,
             avg_entry_price_cents, fees_paid_cents, modeled_edge_cents,
             status, opened_ts) VALUES(1,'WIN','yes',50,40,84,5,'open',?)""",
        (db.now(),))
    conn.execute(
        """INSERT INTO paper_positions(signal_id, market_ticker, side, contracts,
             avg_entry_price_cents, fees_paid_cents, modeled_edge_cents,
             status, opened_ts) VALUES(1,'LOSE','yes',50,40,84,5,'open',?)""",
        (db.now(),))
    conn.execute(
        """INSERT INTO paper_positions(signal_id, market_ticker, side, contracts,
             avg_entry_price_cents, fees_paid_cents, modeled_edge_cents,
             status, opened_ts) VALUES(1,'STILLOPEN','yes',10,40,10,5,'open',?)""",
        (db.now(),))
    conn.commit()
    stub = StubKalshi({
        "WIN": {"status": "settled", "result": "yes"},
        "LOSE": {"status": "settled", "result": "no"},
        "STILLOPEN": {"status": "open", "result": ""},
    })
    n = await engine.settle_positions(conn, stub)
    assert n == 2
    win = conn.execute("SELECT * FROM paper_positions WHERE market_ticker='WIN'").fetchone()
    assert win["status"] == "settled"
    assert win["settle_price_cents"] == 100
    assert win["realized_pnl_cents"] == (100 - 40) * 50 - 84
    lose = conn.execute("SELECT * FROM paper_positions WHERE market_ticker='LOSE'").fetchone()
    assert lose["realized_pnl_cents"] == (0 - 40) * 50 - 84
    # modeled vs realized edge is the key output
    assert win["realized_edge_cents"] == win["realized_pnl_cents"] / 50
    still = conn.execute("SELECT * FROM paper_positions WHERE market_ticker='STILLOPEN'").fetchone()
    assert still["status"] == "open"


def test_mark_to_market_uses_exit_bid(conn):
    conn.execute(
        """INSERT INTO paper_positions(signal_id, market_ticker, side, contracts,
             avg_entry_price_cents, fees_paid_cents, status, opened_ts)
           VALUES(1,'MKT','no',50,40,84,'open',?)""", (db.now(),))
    db.insert_snapshot(conn, "MKT", yes_bids=[[55, 10]], no_bids=[[42, 30]])
    conn.commit()
    assert engine.mark_positions(conn) == 1
    pos = conn.execute("SELECT * FROM paper_positions").fetchone()
    assert pos["mark_price_cents"] == 42     # NO side marks at NO bid


def test_update_daily_pnl_idempotent(conn):
    conn.execute(
        """INSERT INTO paper_positions(signal_id, market_ticker, side, contracts,
             avg_entry_price_cents, fees_paid_cents, status, opened_ts,
             settled_ts, settle_price_cents, realized_pnl_cents)
           VALUES(1,'MKT','yes',50,40,84,'settled',?,?,100,2916)""",
        (db.now(), db.now()))
    conn.commit()
    engine.update_daily_pnl(conn, "2026-08-24", signals_a=3, signals_b=1)
    engine.update_daily_pnl(conn, "2026-08-24", signals_a=5, signals_b=1)
    rows = conn.execute("SELECT * FROM pnl_daily").fetchall()
    assert len(rows) == 1
    assert rows[0]["realized_cents"] == 2916
    assert rows[0]["signals_a"] == 5
