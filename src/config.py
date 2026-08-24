"""Configuration loading: config.yaml for tunables, .env for secrets."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Config(BaseModel):
    kalshi_prod_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_demo_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    kalshi_prod_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    kalshi_demo_ws_url: str = "wss://demo-api.kalshi.co/trade-api/ws/v2"
    polymarket_gamma_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_base_url: str = "https://clob.polymarket.com"

    db_path: str = "data/scanner.db"
    reports_dir: str = "reports"
    log_dir: str = "logs"
    pairs_override_path: str = "pairs_override.yaml"

    universe_sync_minutes: int = 15
    snapshot_seconds: int = 30
    tracked_top_n: int = 200
    orderbook_depth_levels: int = 5
    snapshot_concurrency: int = 8

    persistence_scans: int = 3
    min_edge_pct: float = 2.0
    min_edge_usd: float = 1.00
    bucket_sum_buffer_cents: float = 1.0
    cross_platform_slippage_buffer_cents: float = 1.0

    matcher_min_score: float = 0.55
    matcher_max_close_days_apart: int = 3
    matcher_sync_minutes: int = 60

    kalshi_taker_fee_rate: float = 0.07
    kalshi_maker_fee_rate: float = 0.0175
    kalshi_fee_rate_overrides: dict[str, float] = Field(default_factory=dict)

    max_paper_size_usd: float = 100
    depth_participation_cap: float = 0.20
    paper_mark_to_market_utc_hour: int = 0

    rate_limit_rps: float = 5
    polymarket_rate_limit_rps: float = 5

    snapshot_transport: str = "rest"

    heartbeat_hours: int = 6
    daily_digest_utc_hour: int = 13
    weekly_report_weekday: int = 0

    live_trading: bool = False

    # Secrets (from .env / environment, never from config.yaml)
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    discord_webhook_url: str = ""


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader; existing environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(config_path: str | Path = "config.yaml",
                dotenv_path: str | Path = ".env") -> Config:
    load_dotenv(dotenv_path)
    data: dict = {}
    p = Path(config_path)
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
    cfg = Config(**data)
    cfg.kalshi_api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    cfg.kalshi_private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    cfg.discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    return cfg
