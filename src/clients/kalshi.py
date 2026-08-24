"""Kalshi trading API client: REST + WebSocket, RSA-PSS request signing.

Auth (per docs.kalshi.com, "API Keys"): sign the string
    f"{timestamp_ms}{METHOD}{path}"
where `path` includes the /trade-api/v2 prefix and excludes the query
string, with RSA-PSS (SHA-256 digest, MGF1-SHA256, salt length = digest
length), base64-encode, and send headers KALSHI-ACCESS-KEY,
KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP.

Public market data endpoints work unauthenticated on production; we sign
every request when credentials are configured (higher rate limits, and the
demo environment exercises the auth path end-to-end).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger(__name__)


def load_private_key(path: str | Path) -> rsa.RSAPrivateKey:
    data = Path(path).read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Kalshi API key must be an RSA private key")
    return key


def sign_request(private_key: rsa.RSAPrivateKey, timestamp_ms: int,
                 method: str, path: str) -> str:
    message = f"{timestamp_ms}{method.upper()}{path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


class TokenBucket:
    def __init__(self, rate_per_s: float, capacity: float | None = None):
        self.rate = max(0.1, float(rate_per_s))
        self.capacity = capacity if capacity is not None else max(1.0, self.rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class KalshiClient:
    """REST client. `base_url` must end with /trade-api/v2."""

    def __init__(self, base_url: str, *, api_key_id: str = "",
                 private_key_path: str = "", rate_limit_rps: float = 5,
                 timeout: float = 20.0, max_retries: int = 5):
        self.base_url = base_url.rstrip("/")
        self.path_prefix = urlsplit(self.base_url).path  # "/trade-api/v2"
        self.api_key_id = api_key_id
        self.private_key = (load_private_key(private_key_path)
                            if api_key_id and private_key_path else None)
        self.bucket = TokenBucket(rate_limit_rps)
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def auth_headers(self, method: str, full_path: str) -> dict[str, str]:
        """Signed headers for `full_path` (incl. /trade-api prefix, no query)."""
        if not self.private_key:
            return {}
        ts = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sign_request(self.private_key, ts, method, full_path),
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
        }

    async def request(self, method: str, path: str,
                      params: dict | None = None) -> dict:
        """`path` is relative to the API root, e.g. "/markets"."""
        url = self.base_url + path
        sign_path = self.path_prefix + path
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self.bucket.acquire()
            try:
                resp = await self._client.request(
                    method, url, params=params,
                    headers=self.auth_headers(method, sign_path))
            except httpx.HTTPError as exc:
                last_exc = exc
                await self._backoff(attempt, None)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} from {path}", request=resp.request,
                    response=resp)
                await self._backoff(attempt, resp)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Kalshi request failed after retries: {method} {path}") from last_exc

    async def _backoff(self, attempt: int, resp: httpx.Response | None) -> None:
        delay = min(60.0, (2 ** attempt) + random.uniform(0, 1))
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
        log.warning("Kalshi backoff %.1fs (attempt %d, status %s)", delay,
                    attempt, resp.status_code if resp is not None else "net")
        await asyncio.sleep(delay)

    # --- endpoints ---
    async def get_exchange_status(self) -> dict:
        return await self.request("GET", "/exchange/status")

    async def get_series(self, series_ticker: str) -> dict:
        return (await self.request("GET", f"/series/{series_ticker}")).get("series", {})

    async def get_market(self, ticker: str) -> dict:
        return (await self.request("GET", f"/markets/{ticker}")).get("market", {})

    async def get_orderbook(self, ticker: str, depth: int = 5) -> dict:
        return await self.request("GET", f"/markets/{ticker}/orderbook",
                                  params={"depth": depth})

    async def get_trades(self, ticker: str, limit: int = 100,
                         cursor: str | None = None) -> dict:
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self.request("GET", "/markets/trades", params=params)

    async def iter_markets(self, status: str = "open",
                           limit: int = 1000) -> AsyncIterator[dict]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": limit, "status": status}
            if cursor:
                params["cursor"] = cursor
            page = await self.request("GET", "/markets", params=params)
            for m in page.get("markets", []):
                yield m
            cursor = page.get("cursor")
            if not cursor:
                return

    async def iter_events(self, status: str = "open",
                          limit: int = 200) -> AsyncIterator[dict]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": limit, "status": status}
            if cursor:
                params["cursor"] = cursor
            page = await self.request("GET", "/events", params=params)
            for e in page.get("events", []):
                yield e
            cursor = page.get("cursor")
            if not cursor:
                return


class KalshiWebSocket:
    """Minimal orderbook_delta/ticker subscriber maintaining local books.

    Swappable transport: ingest.snapshots uses REST polling by default
    (snapshot_transport: "rest"); set "ws" to source books from this feed.
    Auth: same signature scheme over GET + the WS path.
    """

    def __init__(self, ws_url: str, *, api_key_id: str = "",
                 private_key_path: str = "",
                 on_book: Callable[[str, list, list], None] | None = None):
        self.ws_url = ws_url
        self.api_key_id = api_key_id
        self.private_key = (load_private_key(private_key_path)
                            if api_key_id and private_key_path else None)
        self.on_book = on_book
        self.books: dict[str, dict[str, dict[int, int]]] = {}
        self._stop = asyncio.Event()

    def _headers(self) -> dict[str, str]:
        if not self.private_key:
            return {}
        ts = int(time.time() * 1000)
        path = urlsplit(self.ws_url).path
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sign_request(self.private_key, ts, "GET", path),
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
        }

    def stop(self) -> None:
        self._stop.set()

    async def run(self, market_tickers: list[str]) -> None:
        import websockets
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                        self.ws_url, additional_headers=self._headers(),
                        ping_interval=10) as ws:
                    await ws.send(json.dumps({
                        "id": 1, "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta", "ticker"],
                                   "market_tickers": market_tickers},
                    }))
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        self._handle(json.loads(raw))
            except Exception as exc:  # reconnect on any transport error
                log.warning("Kalshi WS error: %s; reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2 + random.uniform(0, 1))

    def _handle(self, msg: dict) -> None:
        mtype, data = msg.get("type"), msg.get("msg") or {}
        ticker = data.get("market_ticker")
        if not ticker:
            return
        if mtype == "orderbook_snapshot":
            self.books[ticker] = {
                "yes": {int(p): int(q) for p, q in (data.get("yes") or [])},
                "no": {int(p): int(q) for p, q in (data.get("no") or [])},
            }
        elif mtype == "orderbook_delta":
            book = self.books.setdefault(ticker, {"yes": {}, "no": {}})
            side = data.get("side")
            price, delta = int(data.get("price", 0)), int(data.get("delta", 0))
            if side in ("yes", "no"):
                lvl = book[side]
                lvl[price] = lvl.get(price, 0) + delta
                if lvl[price] <= 0:
                    lvl.pop(price, None)
        else:
            return
        if self.on_book and ticker in self.books:
            b = self.books[ticker]
            yes = sorted(b["yes"].items(), key=lambda kv: -kv[0])
            no = sorted(b["no"].items(), key=lambda kv: -kv[0])
            self.on_book(ticker, [list(l) for l in yes], [list(l) for l in no])
