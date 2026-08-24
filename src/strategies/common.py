"""Shared signal types and the persistence gate."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field

from .. import db
from ..fees import EdgeResult


@dataclass
class Leg:
    market_ticker: str
    side: str                   # 'yes' | 'no'
    action: str                 # 'buy' | 'sell'
    price_cents: float          # executable avg price at sized depth
    contracts: int
    platform: str = "kalshi"    # 'kalshi' | 'polymarket' (polymarket = reference only)


@dataclass
class Signal:
    strategy: str               # 'A' | 'B'
    kind: str                   # monotonicity | bucket_sum_buy | bucket_sum_sell
                                # | complement | cross_platform
    legs: list[Leg]
    edge: EdgeResult
    details: dict = field(default_factory=dict)
    provisional: bool = False   # provisional pair: alert-only, no paper trade

    @property
    def fingerprint(self) -> str:
        ident = "|".join(sorted(
            f"{l.platform}:{l.market_ticker}:{l.side}:{l.action}" for l in self.legs))
        raw = f"{self.strategy}|{self.kind}|{ident}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def legs_json(self) -> str:
        return json.dumps([asdict(l) for l in self.legs])


def record_signal(conn: sqlite3.Connection, sig: Signal, scan_seq: int,
                  persistence_scans: int) -> tuple[int, bool, int]:
    """Upsert a signal sighting for this scan pass.

    A signal must be seen on `persistence_scans` *consecutive* scan passes
    before it qualifies (alerts / paper-trades). A gap in scan sequence, or a
    process restart (scan_seq is persisted and monotonic), resets the streak.

    Returns (signal_id, newly_qualified, consecutive_scans).
    """
    ts = db.now()
    row = conn.execute("SELECT * FROM signals WHERE fingerprint=?",
                       (sig.fingerprint,)).fetchone()
    if row is None:
        consecutive = 1
        status = "qualified" if consecutive >= persistence_scans else "pending"
        cur = conn.execute(
            """INSERT INTO signals(fingerprint, strategy, kind, legs, edge_cents,
                 edge_pct, edge_usd, max_size_contracts, max_size_usd, details,
                 provisional, status, consecutive_scans, last_scan_seq,
                 first_seen_ts, last_seen_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sig.fingerprint, sig.strategy, sig.kind, sig.legs_json(),
             sig.edge.per_contract_net_cents, sig.edge.net_edge_pct,
             sig.edge.net_edge_usd, sig.edge.contracts,
             sig.edge.capital_at_risk_cents / 100.0, json.dumps(sig.details),
             1 if sig.provisional else 0, status, consecutive, scan_seq, ts, ts))
        return cur.lastrowid, status == "qualified", consecutive

    if row["last_scan_seq"] == scan_seq:          # duplicate within one pass
        return row["id"], False, row["consecutive_scans"]
    if row["last_scan_seq"] == scan_seq - 1 and row["status"] != "expired":
        consecutive = row["consecutive_scans"] + 1
    else:
        consecutive = 1                            # streak broken; start over
    was_qualified = row["status"] == "qualified" and row["last_scan_seq"] == scan_seq - 1
    status = "qualified" if consecutive >= persistence_scans else "pending"
    conn.execute(
        """UPDATE signals SET legs=?, edge_cents=?, edge_pct=?, edge_usd=?,
             max_size_contracts=?, max_size_usd=?, details=?, provisional=?,
             status=?, consecutive_scans=?, last_scan_seq=?, last_seen_ts=?
           WHERE id=?""",
        (sig.legs_json(), sig.edge.per_contract_net_cents,
         sig.edge.net_edge_pct, sig.edge.net_edge_usd, sig.edge.contracts,
         sig.edge.capital_at_risk_cents / 100.0, json.dumps(sig.details),
         1 if sig.provisional else 0, status, consecutive, scan_seq, ts,
         row["id"]))
    newly_qualified = status == "qualified" and not was_qualified
    return row["id"], newly_qualified, consecutive


def expire_stale_signals(conn: sqlite3.Connection, scan_seq: int) -> int:
    """After a full scan pass, anything not re-seen this pass is expired."""
    cur = conn.execute(
        "UPDATE signals SET status='expired' "
        "WHERE last_scan_seq < ? AND status IN ('pending','qualified')",
        (scan_seq,))
    return cur.rowcount


def best_size(edge_at, max_contracts: int, breakpoints: list[int]) -> int:
    """Largest size (from candidate breakpoints) whose net edge stays positive.

    `edge_at(n) -> EdgeResult`; breakpoints are cumulative-depth sizes from the
    books involved. Books are shallow (top 5 levels) so a linear scan is fine.
    """
    candidates = sorted({b for b in breakpoints if 0 < b <= max_contracts})
    best = 0
    for n in candidates:
        if edge_at(n).net_edge_cents > 0:
            best = n
    return best
