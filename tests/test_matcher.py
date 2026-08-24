from pathlib import Path

from src.config import Config
from src.matching import matcher
from tests.conftest import make_market

POLY = {
    "question": "Will the Fed cut rates in September 2026?",
    "description": "Resolves YES if the FOMC lowers the target rate at its September 2026 meeting.",
    "conditionId": "0xabc123",
    "clobTokenIds": '["111", "222"]',
    "endDate": "2026-09-17T20:00:00Z",
}


def test_normalize_and_tokens():
    assert matcher.normalize_text("Will the FED cut rates?!") == "will the fed cut rates"
    assert "fed" in matcher.tokens("Will the Fed cut rates?")
    assert "will" not in matcher.tokens("Will the Fed cut rates?")


def test_entities_extract_numbers_and_names():
    ents = matcher.entities("Will Trump win Michigan by 5%?")
    assert "trump" in ents and "michigan" in ents and "5%" in ents


def test_similarity_high_for_same_event():
    score = matcher.similarity(
        "Fed cuts rates in September?", "", "2026-09-17T20:00:00Z",
        POLY["question"], POLY["description"], POLY["endDate"], 3)
    assert score > 0.55


def test_similarity_zero_when_dates_far_apart():
    score = matcher.similarity(
        "Fed cuts rates in September?", "", "2026-12-17T20:00:00Z",
        POLY["question"], POLY["description"], POLY["endDate"], 3)
    assert score == 0.0


def test_verify_exact_requires_same_date_and_text():
    assert matcher.verify_exact("Resolves YES if X.", "2026-09-17T23:00:00Z",
                                "Resolves YES if X.", "2026-09-17T01:00:00Z")
    assert not matcher.verify_exact("Resolves YES if X.", "2026-09-17T23:00:00Z",
                                    "Resolves YES if X.", "2026-09-18T01:00:00Z")
    assert not matcher.verify_exact("Resolves YES if X.", "2026-09-17T23:00:00Z",
                                    "Resolves YES if Y.", "2026-09-17T01:00:00Z")
    assert not matcher.verify_exact("", "2026-09-17T23:00:00Z",
                                    "", "2026-09-17T01:00:00Z")


def test_run_matcher_creates_provisional_pair(conn):
    cfg = Config()
    make_market(conn, "KXFED-26SEP", title="Fed cuts rates in September 2026?",
                rules_primary="Different rules text.",
                close_time="2026-09-17T20:00:00Z")
    matcher.run_matcher(conn, [POLY], cfg)
    row = conn.execute("SELECT * FROM market_pairs").fetchone()
    assert row is not None
    assert row["status"] == "provisional"       # criteria text differs
    assert row["poly_token_id_yes"] == "111"
    assert row["poly_token_id_no"] == "222"


def test_run_matcher_auto_confirms_exact_match(conn):
    cfg = Config()
    make_market(conn, "KXFED-26SEP", title="Fed cut rates September 2026?",
                rules_primary=POLY["description"],
                close_time="2026-09-17T23:59:00Z")
    matcher.run_matcher(conn, [POLY], cfg)
    row = conn.execute("SELECT * FROM market_pairs").fetchone()
    assert row["status"] == "confirmed"


def test_override_file_always_wins(conn, tmp_path):
    cfg = Config(pairs_override_path=str(tmp_path / "pairs_override.yaml"))
    make_market(conn, "KXFED-26SEP", title="Fed cuts rates in September 2026?",
                rules_primary="Different rules.",
                close_time="2026-09-17T20:00:00Z")
    Path(cfg.pairs_override_path).write_text(
        "pairs:\n"
        "  - kalshi_ticker: KXFED-26SEP\n"
        "    poly_condition_id: '0xabc123'\n"
        "    status: confirmed\n"
        "    notes: hand-verified\n")
    matcher.run_matcher(conn, [POLY], cfg)
    row = conn.execute("SELECT * FROM market_pairs").fetchone()
    assert row["status"] == "confirmed" and row["source"] == "manual"
    # Re-running the automatic matcher must NOT demote a manual decision.
    matcher.run_matcher(conn, [POLY], cfg)
    row = conn.execute("SELECT * FROM market_pairs").fetchone()
    assert row["status"] == "confirmed" and row["source"] == "manual"


def test_no_candidate_without_shared_tokens(conn):
    cfg = Config()
    make_market(conn, "KXWX-26", title="High temperature in Miami tomorrow?",
                close_time="2026-09-17T20:00:00Z")
    matcher.run_matcher(conn, [POLY], cfg)
    assert conn.execute("SELECT COUNT(*) c FROM market_pairs").fetchone()["c"] == 0
