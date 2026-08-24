import os

from src.config import load_config


def test_defaults_when_no_files(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml", tmp_path / "nope.env")
    assert cfg.universe_sync_minutes == 15
    assert cfg.snapshot_seconds == 30
    assert cfg.tracked_top_n == 200
    assert cfg.persistence_scans == 3
    assert cfg.live_trading is False


def test_yaml_and_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    (tmp_path / "config.yaml").write_text(
        "tracked_top_n: 50\nmin_edge_pct: 3.5\n"
        "kalshi_fee_rate_overrides:\n  KXINX: 0.035\n")
    (tmp_path / ".env").write_text(
        "KALSHI_API_KEY_ID=abc\n# comment\nDISCORD_WEBHOOK_URL='https://x'\n")
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.tracked_top_n == 50
    assert cfg.min_edge_pct == 3.5
    assert cfg.kalshi_fee_rate_overrides == {"KXINX": 0.035}
    assert cfg.kalshi_api_key_id == "abc"
    assert cfg.discord_webhook_url == "https://x"
    assert os.environ["KALSHI_API_KEY_ID"] == "abc"


def test_real_repo_config_parses():
    from pathlib import Path
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert cfg.tracked_top_n == 200
    assert cfg.live_trading is False
