"""Entrypoint: scheduler for the always-on scanner, plus operator CLI.

Usage:
  python -m src.main run                     # the long-running scanner
  python -m src.main sync                    # one-shot universe sync
  python -m src.main scan-once               # one snapshot + scan pass
  python -m src.main verify-auth             # signed request against demo
  python -m src.main candidates [--status pending]
  python -m src.main confirm-relationship <id>
  python -m src.main reject-relationship <id>
  python -m src.main pairs [--status confirmed]
  python -m src.main report daily|weekly
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .alerts.discord import DiscordAlerter
from .book import OrderBook
from .clients.kalshi import KalshiClient
from .clients.polymarket import PolymarketClient, book_to_kalshi_shape
from .config import Config, load_config
from .fees import FeeSchedule
from .ingest import snapshots, universe
from .matching import matcher as matching
from .paper import engine, report
from .strategies import correlated, cross_platform
from .strategies.common import expire_stale_signals, record_signal

log = logging.getLogger("scanner")


def setup_logging(cfg: Config) -> None:
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(
        Path(cfg.log_dir) / "scanner.log", maxBytes=20_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    # One line per HTTP request would swamp the logs (and any redirect file)
    # at a request every ~200ms; scanner INFO lines already summarize cycles.
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = db.connect(cfg.db_path)
        self.fees = FeeSchedule.from_config(cfg)
        # Public market data: production, read-only, UNAUTHENTICATED — the
        # configured key belongs to the demo environment and must never be
        # sent to prod (prod may 401 a signature from a key it doesn't know).
        # Authenticated calls (verify-auth, future order flow) build their own
        # demo client with the credentials.
        self.kalshi = KalshiClient(
            cfg.kalshi_prod_base_url,
            rate_limit_rps=cfg.rate_limit_rps)
        self.poly = PolymarketClient(
            cfg.polymarket_gamma_base_url, cfg.polymarket_clob_base_url,
            rate_limit_rps=cfg.polymarket_rate_limit_rps)
        self.alerter = DiscordAlerter(cfg.discord_webhook_url)
        self.signals_today = {"A": 0, "B": 0}

    async def aclose(self) -> None:
        await self.kalshi.aclose()
        await self.poly.aclose()
        await self.alerter.aclose()
        self.conn.close()

    # ------------------------------------------------------------------
    async def scan_pass(self) -> dict:
        """One full pass: snapshots -> Strategy A -> Strategy B -> record ->
        alert -> paper trade -> expire."""
        cfg, conn = self.cfg, self.conn
        tickers = universe.tracked_market_tickers(conn, cfg)
        n_snaps = await snapshots.snapshot_cycle(conn, self.kalshi, cfg, tickers)

        books: dict[str, OrderBook] = {}
        for t in tickers:
            snap = db.latest_snapshot(conn, t, max_age_s=cfg.snapshot_seconds * 4)
            if snap:
                books[t] = OrderBook(yes_bids=json.loads(snap["yes_bids"]),
                                     no_bids=json.loads(snap["no_bids"]))

        found = correlated.scan(conn, cfg, self.fees, books)

        poly_books = await self._fetch_poly_books()
        found += cross_platform.scan(conn, cfg, self.fees, books, poly_books)

        scan_seq = db.next_scan_seq(conn)
        n_qualified = 0
        for sig in found:
            self.signals_today[sig.strategy] = self.signals_today.get(sig.strategy, 0) + 1
            sig_id, newly_qualified, _ = record_signal(
                conn, sig, scan_seq, cfg.persistence_scans)
            if not newly_qualified:
                continue
            n_qualified += 1
            row = conn.execute("SELECT * FROM signals WHERE id=?", (sig_id,)).fetchone()
            await self.alerter.send(report.format_signal_alert(row))
            conn.execute("UPDATE signals SET alerted=1 WHERE id=?", (sig_id,))
            if not sig.provisional and not row["paper_traded"]:
                engine.execute_signal(conn, cfg, self.fees, sig_id, sig, books)
        expired = expire_stale_signals(conn, scan_seq)
        conn.commit()
        db.heartbeat(conn, "scan", detail=f"seq={scan_seq}")
        stats = {"snapshots": n_snaps, "signals": len(found),
                 "newly_qualified": n_qualified, "expired": expired,
                 "scan_seq": scan_seq}
        log.info("scan pass: %s", stats)
        return stats

    async def _fetch_poly_books(self) -> dict[str, OrderBook]:
        """Books for the YES token of every active pair, keyed by token id."""
        pairs = self.conn.execute(
            "SELECT DISTINCT poly_token_id_yes FROM market_pairs "
            "WHERE status IN ('confirmed','provisional') "
            "AND poly_token_id_yes IS NOT NULL").fetchall()
        out: dict[str, OrderBook] = {}
        for r in pairs:
            token = r["poly_token_id_yes"]
            try:
                raw = await self.poly.get_book(token)
            except Exception as exc:
                log.warning("poly book fetch failed for %s: %s", token, exc)
                continue
            yes_bids, no_bids = book_to_kalshi_shape(raw)
            snapshots.store_book(self.conn, token, yes_bids, no_bids,
                                 self.cfg.orderbook_depth_levels,
                                 source="polymarket")
            out[token] = OrderBook(yes_bids=yes_bids, no_bids=no_bids)
        self.conn.commit()
        return out

    # ------------------------------------------------------------------
    async def universe_loop(self) -> None:
        while True:
            try:
                await universe.sync_universe(self.conn, self.kalshi, self.cfg)
                n = correlated.generate_relationship_candidates(self.conn)
                if n:
                    log.info("relationship candidates added: %d", n)
                db.heartbeat(self.conn, "universe")
            except Exception as exc:
                log.exception("universe sync failed")
                await self.alerter.error("universe sync", exc)
            await asyncio.sleep(self.cfg.universe_sync_minutes * 60)

    async def matcher_loop(self) -> None:
        while True:
            try:
                poly_markets = [m async for m in self.poly.iter_active_markets()]
                matching.run_matcher(self.conn, poly_markets, self.cfg)
                db.heartbeat(self.conn, "matcher")
            except Exception as exc:
                log.exception("matcher failed")
                await self.alerter.error("matcher", exc)
            await asyncio.sleep(self.cfg.matcher_sync_minutes * 60)

    async def scan_loop(self) -> None:
        while True:
            started = db.now()
            try:
                await self.scan_pass()
            except Exception as exc:
                log.exception("scan pass failed")
                await self.alerter.error("scan pass", exc)
            elapsed = db.now() - started
            await asyncio.sleep(max(1.0, self.cfg.snapshot_seconds - elapsed))

    async def settle_loop(self) -> None:
        while True:
            try:
                settled = await engine.settle_positions(self.conn, self.kalshi)
                marked = engine.mark_positions(self.conn)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                engine.update_daily_pnl(self.conn, today,
                                        self.signals_today.get("A", 0),
                                        self.signals_today.get("B", 0))
                if settled:
                    await self.alerter.send(
                        f"Settled {settled} paper position(s); {marked} marked.")
                db.heartbeat(self.conn, "settle")
            except Exception as exc:
                log.exception("settle loop failed")
                await self.alerter.error("settle loop", exc)
            await asyncio.sleep(1800)

    async def heartbeat_loop(self) -> None:
        while True:
            try:
                open_n = self.conn.execute(
                    "SELECT COUNT(*) c FROM paper_positions WHERE status='open'"
                ).fetchone()["c"]
                sig_n = self.conn.execute(
                    "SELECT COUNT(*) c FROM signals WHERE status='qualified'"
                ).fetchone()["c"]
                await self.alerter.heartbeat(
                    f"alive; {sig_n} qualified signals, {open_n} open paper positions")
            except Exception:
                log.exception("heartbeat failed")
            await asyncio.sleep(self.cfg.heartbeat_hours * 3600)

    async def report_loop(self) -> None:
        last_daily = last_weekly = None
        while True:
            now = datetime.now(timezone.utc)
            try:
                if now.hour == self.cfg.daily_digest_utc_hour and last_daily != now.date():
                    await self.alerter.send(report.daily_digest(self.conn))
                    self.signals_today = {"A": 0, "B": 0}
                    last_daily = now.date()
                if (now.weekday() == self.cfg.weekly_report_weekday
                        and now.hour == self.cfg.daily_digest_utc_hour
                        and last_weekly != now.date()):
                    _, summary = report.weekly_report(self.conn, self.cfg.reports_dir)
                    await self.alerter.send(summary)
                    last_weekly = now.date()
            except Exception as exc:
                log.exception("report loop failed")
                await self.alerter.error("report loop", exc)
            await asyncio.sleep(300)

    async def run(self) -> None:
        if self.cfg.live_trading:
            # Future phase: this is a stub by design (spec: MVP never trades live).
            engine.place_live_order()
        await self.alerter.send(":rocket: scanner starting")
        # Burn one scan seq so persistence streaks never span a restart: a
        # signal's consecutive-scan count requires an unbroken seq chain, and
        # we can't prove the violation persisted while we were down.
        db.next_scan_seq(self.conn)
        # Universe sync must complete once before scanning has anything to do.
        await universe.sync_universe(self.conn, self.kalshi, self.cfg)
        correlated.generate_relationship_candidates(self.conn)
        loops = [self.universe_loop(), self.matcher_loop(), self.scan_loop(),
                 self.settle_loop(), self.heartbeat_loop(), self.report_loop()]
        # First universe_loop iteration re-syncs immediately; harmless (idempotent).
        await asyncio.gather(*loops)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

async def cmd_verify_auth(cfg: Config) -> int:
    """M1 acceptance: a signed authenticated request against demo succeeds."""
    if not cfg.kalshi_api_key_id or not cfg.kalshi_private_key_path:
        print("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env "
              "(create the key in the demo environment).")
        return 2
    client = KalshiClient(cfg.kalshi_demo_base_url,
                          api_key_id=cfg.kalshi_api_key_id,
                          private_key_path=cfg.kalshi_private_key_path,
                          rate_limit_rps=cfg.rate_limit_rps)
    try:
        # /portfolio/balance requires a valid signature — public endpoints
        # would succeed even with broken auth, so they prove nothing.
        balance = await client.request("GET", "/portfolio/balance")
        print(f"Demo auth OK. Balance response: {balance}")
        return 0
    except Exception as exc:
        print(f"Demo auth FAILED: {exc}")
        return 1
    finally:
        await client.aclose()


def cmd_candidates(conn, status: str) -> None:
    rows = conn.execute(
        "SELECT * FROM relationship_candidates WHERE status=? ORDER BY id",
        (status,)).fetchall()
    if not rows:
        print(f"No {status} relationship candidates.")
        return
    for r in rows:
        target = (f"{r['narrower_ticker']} => {r['broader_ticker']}"
                  if r["kind"] == "nested" else f"event {r['event_ticker']}")
        print(f"[{r['id']}] {r['kind']}: {target}\n    {r['rationale']}")


def cmd_decide_relationship(conn, cand_id: int, decision: str) -> None:
    cur = conn.execute(
        "UPDATE relationship_candidates SET status=?, decided_at=? WHERE id=?",
        (decision, db.now(), cand_id))
    conn.commit()
    print(f"Candidate {cand_id}: {decision}" if cur.rowcount
          else f"No candidate with id {cand_id}")


def cmd_pairs(conn, status: str | None) -> None:
    q = "SELECT * FROM market_pairs"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    rows = conn.execute(q + " ORDER BY score DESC", args).fetchall()
    if not rows:
        print("No pairs.")
        return
    for r in rows:
        print(f"[{r['id']}] {r['status']:<11} {r['source']:<6} "
              f"score={r['score'] if r['score'] is not None else '—'} "
              f"{r['kalshi_ticker']}  <->  {(r['poly_question'] or '')[:60]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kalshi-scanner")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("sync")
    sub.add_parser("scan-once")
    sub.add_parser("verify-auth")
    p = sub.add_parser("candidates")
    p.add_argument("--status", default="pending")
    p = sub.add_parser("confirm-relationship")
    p.add_argument("id", type=int)
    p = sub.add_parser("reject-relationship")
    p.add_argument("id", type=int)
    p = sub.add_parser("pairs")
    p.add_argument("--status", default=None)
    p = sub.add_parser("report")
    p.add_argument("which", choices=["daily", "weekly"])
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg)

    if args.command == "verify-auth":
        return asyncio.run(cmd_verify_auth(cfg))

    if args.command in ("candidates", "confirm-relationship",
                        "reject-relationship", "pairs", "report"):
        conn = db.connect(cfg.db_path)
        try:
            if args.command == "candidates":
                cmd_candidates(conn, args.status)
            elif args.command == "confirm-relationship":
                cmd_decide_relationship(conn, args.id, "confirmed")
            elif args.command == "reject-relationship":
                cmd_decide_relationship(conn, args.id, "rejected")
            elif args.command == "pairs":
                cmd_pairs(conn, args.status)
            elif args.command == "report":
                if args.which == "daily":
                    print(report.daily_digest(conn))
                else:
                    path, summary = report.weekly_report(conn, cfg.reports_dir)
                    print(summary)
                    print(path.read_text())
        finally:
            conn.close()
        return 0

    app = App(cfg)

    async def _run() -> int:
        try:
            if args.command == "run":
                await app.run()
            elif args.command == "sync":
                stats = await universe.sync_universe(app.conn, app.kalshi, cfg)
                n = correlated.generate_relationship_candidates(app.conn)
                print(f"synced: {stats}; new relationship candidates: {n}")
            elif args.command == "scan-once":
                print(await app.scan_pass())
            return 0
        finally:
            await app.aclose()

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
