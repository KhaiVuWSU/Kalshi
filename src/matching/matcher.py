"""Kalshi <-> Polymarket market equivalence matching.

False matches are the #1 risk for Strategy B, so the pipeline is
deliberately conservative:

1. Candidate generation: normalized-title token similarity + shared
   entities (numbers, dates, capitalized names) + resolution-date proximity.
2. Verification: a pair is auto-CONFIRMED only when the resolution date
   matches exactly (same UTC date) AND normalized resolution-criteria text
   matches exactly. Anything else stays PROVISIONAL (alert-only, never
   paper-traded).
3. pairs_override.yaml always wins — entries there force confirmed /
   rejected regardless of what the automatic pipeline thinks.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .. import db
from ..clients.polymarket import parse_clob_token_ids
from ..config import Config

log = logging.getLogger(__name__)

_STOPWORDS = {
    "will", "the", "a", "an", "of", "in", "on", "at", "by", "to", "be",
    "is", "are", "for", "and", "or", "than", "more", "before", "after",
    "does", "do", "what", "who", "how", "many", "much", "market", "yes",
    "no", "this", "that", "it", "its", "with", "as", "from",
}


def normalize_text(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s.%$-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens(s: str | None) -> set[str]:
    return {t for t in normalize_text(s).split() if t not in _STOPWORDS and len(t) > 1}


def entities(s: str | None) -> set[str]:
    """Numbers, years, percents, $ amounts, and capitalized words — the
    things two titles must share to plausibly be the same event."""
    raw = s or ""
    ents = set(re.findall(r"\$?\d[\d,.]*%?", raw))
    ents |= {w.lower() for w in re.findall(r"\b[A-Z][a-zA-Z]+\b", raw)}
    return {e for e in ents if e.lower() not in _STOPWORDS}


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def similarity(kalshi_title: str, kalshi_rules: str, kalshi_close: str | None,
               poly_question: str, poly_description: str,
               poly_end: str | None, max_days_apart: int) -> float:
    tk, tp = tokens(kalshi_title), tokens(poly_question)
    if not tk or not tp:
        return 0.0
    jaccard = len(tk & tp) / len(tk | tp)
    ek, ep = entities(kalshi_title + " " + (kalshi_rules or "")), \
        entities(poly_question + " " + (poly_description or ""))
    ent_overlap = (len(ek & ep) / len(ek | ep)) if (ek or ep) else 0.0
    dk, dp = parse_date(kalshi_close), parse_date(poly_end)
    if dk and dp:
        days = abs((dk - dp).total_seconds()) / 86400
        date_score = max(0.0, 1.0 - days / max(1, max_days_apart))
        if days > max_days_apart:
            return 0.0
    else:
        date_score = 0.0
    return 0.5 * jaccard + 0.3 * ent_overlap + 0.2 * date_score


def verify_exact(kalshi_rules: str | None, kalshi_close: str | None,
                 poly_description: str | None, poly_end: str | None) -> bool:
    """Auto-confirm bar: same UTC resolution date AND identical normalized
    resolution text. Deliberately strict — most confirmations should come
    from the manual override file."""
    dk, dp = parse_date(kalshi_close), parse_date(poly_end)
    if not dk or not dp or dk.date() != dp.date():
        return False
    rk, rp = normalize_text(kalshi_rules), normalize_text(poly_description)
    return bool(rk) and rk == rp


def run_matcher(conn: sqlite3.Connection, poly_markets: list[dict],
                cfg: Config) -> dict:
    """Match all open Kalshi markets against active Polymarket markets."""
    kalshi_rows = conn.execute(
        "SELECT ticker, title, yes_sub_title, rules_primary, close_time "
        "FROM markets WHERE status IN ('open','active')").fetchall()

    # Inverted index over informative poly tokens to avoid O(N*M) scoring.
    index: dict[str, list[int]] = {}
    for i, pm in enumerate(poly_markets):
        for t in tokens(pm.get("question") or pm.get("title") or ""):
            index.setdefault(t, []).append(i)

    n_new = n_confirmed = 0
    for km in kalshi_rows:
        k_title = f"{km['title'] or ''} {km['yes_sub_title'] or ''}".strip()
        cand_ids: dict[int, int] = {}
        for t in tokens(k_title):
            for i in index.get(t, []):
                cand_ids[i] = cand_ids.get(i, 0) + 1
        # Require >= 2 shared informative tokens before scoring.
        for i, shared in cand_ids.items():
            if shared < 2:
                continue
            pm = poly_markets[i]
            q = pm.get("question") or pm.get("title") or ""
            desc = pm.get("description") or ""
            end = pm.get("endDate") or pm.get("end_date_iso") or pm.get("endDateIso")
            score = similarity(k_title, km["rules_primary"] or "",
                               km["close_time"], q, desc, end,
                               cfg.matcher_max_close_days_apart)
            if score < cfg.matcher_min_score:
                continue
            cond = pm.get("conditionId") or pm.get("condition_id") or ""
            if not cond:
                continue
            tok_yes, tok_no = parse_clob_token_ids(pm)
            status = "confirmed" if verify_exact(
                km["rules_primary"], km["close_time"], desc, end) else "provisional"
            cur = conn.execute(
                """INSERT INTO market_pairs(kalshi_ticker, poly_condition_id,
                     poly_token_id_yes, poly_token_id_no, poly_question,
                     poly_end_date, score, status, source, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?, 'auto', ?, ?)
                   ON CONFLICT(kalshi_ticker, poly_condition_id) DO UPDATE SET
                     poly_token_id_yes=excluded.poly_token_id_yes,
                     poly_token_id_no=excluded.poly_token_id_no,
                     poly_question=excluded.poly_question,
                     poly_end_date=excluded.poly_end_date,
                     score=excluded.score, updated_at=excluded.updated_at,
                     status=CASE WHEN market_pairs.source='manual'
                                 THEN market_pairs.status ELSE excluded.status END""",
                (km["ticker"], cond, tok_yes, tok_no, q, end, score, status,
                 db.now(), db.now()))
            n_new += cur.rowcount
            if status == "confirmed":
                n_confirmed += 1
    conn.commit()
    apply_overrides(conn, cfg.pairs_override_path)
    stats = {
        "pairs_total": conn.execute("SELECT COUNT(*) c FROM market_pairs").fetchone()["c"],
        "confirmed": conn.execute(
            "SELECT COUNT(*) c FROM market_pairs WHERE status='confirmed'").fetchone()["c"],
        "provisional": conn.execute(
            "SELECT COUNT(*) c FROM market_pairs WHERE status='provisional'").fetchone()["c"],
    }
    log.info("matcher: %s", stats)
    return stats


def apply_overrides(conn: sqlite3.Connection, path: str | Path) -> int:
    """pairs_override.yaml format:
    pairs:
      - kalshi_ticker: KXEXAMPLE-26DEC31
        poly_condition_id: "0xabc..."
        status: confirmed          # confirmed | rejected | provisional
        poly_token_id_yes: "123"   # optional, needed for confirmed pairs
        poly_token_id_no: "456"    # optional
        notes: "hand-verified 2026-08-24"
    """
    p = Path(path)
    if not p.exists():
        return 0
    data = yaml.safe_load(p.read_text()) or {}
    n = 0
    for entry in data.get("pairs", []):
        kt = entry.get("kalshi_ticker")
        cond = entry.get("poly_condition_id")
        status = entry.get("status", "confirmed")
        if not kt or not cond or status not in ("confirmed", "rejected", "provisional"):
            continue
        conn.execute(
            """INSERT INTO market_pairs(kalshi_ticker, poly_condition_id,
                 poly_token_id_yes, poly_token_id_no, status, source, notes,
                 created_at, updated_at)
               VALUES(?,?,?,?,?, 'manual', ?, ?, ?)
               ON CONFLICT(kalshi_ticker, poly_condition_id) DO UPDATE SET
                 status=excluded.status, source='manual', notes=excluded.notes,
                 poly_token_id_yes=COALESCE(excluded.poly_token_id_yes,
                                            market_pairs.poly_token_id_yes),
                 poly_token_id_no=COALESCE(excluded.poly_token_id_no,
                                           market_pairs.poly_token_id_no),
                 updated_at=excluded.updated_at""",
            (kt, cond, entry.get("poly_token_id_yes"),
             entry.get("poly_token_id_no"), status, entry.get("notes"),
             db.now(), db.now()))
        n += 1
    conn.commit()
    return n
