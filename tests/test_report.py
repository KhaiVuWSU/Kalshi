from src import db
from src.fees import edge_result
from src.paper import report
from src.strategies.common import Leg, Signal, record_signal


def _seed(conn):
    sig = Signal(strategy="B", kind="cross_platform",
                 legs=[Leg("KMKT", "yes", "buy", 40.0, 50),
                       Leg("0xc0ffee" * 6, "yes", "sell", 50.0, 50,
                           platform="polymarket")],
                 edge=edge_result(50, 2500, 2000, 134), provisional=True)
    sid, _, _ = record_signal(conn, sig, db.next_scan_seq(conn), 1)
    conn.execute(
        """INSERT INTO paper_positions(signal_id, market_ticker, side, contracts,
             avg_entry_price_cents, fees_paid_cents, modeled_edge_cents, status,
             opened_ts, settled_ts, settle_price_cents, realized_pnl_cents,
             realized_edge_cents)
           VALUES(?, 'KMKT','yes',50,40,84,7.3,'settled',?,?,100,2916,58.32)""",
        (sid, db.now(), db.now()))
    conn.execute(
        """INSERT INTO paper_positions(signal_id, market_ticker, side, contracts,
             avg_entry_price_cents, fees_paid_cents, status, opened_ts,
             mark_price_cents, marked_ts)
           VALUES(?, 'KMKT2','no',10,60,10,'open',?,65,?)""",
        (sid, db.now(), db.now()))
    conn.commit()
    return sid


def test_daily_digest_renders(conn):
    _seed(conn)
    text = report.daily_digest(conn)
    assert "Daily digest" in text
    assert "B=1" in text
    assert "$29.16" in text


def test_weekly_report_renders(conn, tmp_path):
    _seed(conn)
    path, summary = report.weekly_report(conn, tmp_path)
    text = path.read_text()
    assert "| B | 1 |" in text
    assert "100%" in text                 # hit rate: 1 settled winner
    assert "kalshi.com/markets/KMKT" in text
    assert "modeled" in text.lower()
    assert "realized" in summary


def test_signal_alert_labels_provisional_and_links(conn):
    sid = _seed(conn)
    row = conn.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
    text = report.format_signal_alert(row)
    assert "CALLOUT" in text
    assert "PROVISIONAL" in text
    assert "kalshi.com/markets/KMKT" in text
    assert "Polymarket reference" in text
    assert "Not financial advice" in text
    assert "BUY YES" in text
