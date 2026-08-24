"""Polymarket read-only client: Gamma (metadata) + CLOB (orderbooks).

No wallet, no auth — public read endpoints only (verified against current
docs: Gamma `/markets`, CLOB `/book?token_id=`). Token ids come from
Gamma's `clobTokenIds` (JSON-encoded [yes_token, no_token]).

Prices are 0..1 dollars; we normalize to integer cents and map the book
into the same shape as Kalshi's (YES bids / NO bids) so src.book.OrderBook
works unchanged: a YES ask at p is represented as a NO bid at 100 - p.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, AsyncIterator

import httpx

from .kalshi import TokenBucket

log = logging.getLogger(__name__)


class PolymarketClient:
    def __init__(self, gamma_base_url: str, clob_base_url: str,
                 rate_limit_rps: float = 5, timeout: float = 20.0,
                 max_retries: int = 5):
        self.gamma = gamma_base_url.rstrip("/")
        self.clob = clob_base_url.rstrip("/")
        self.bucket = TokenBucket(rate_limit_rps)
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict | None = None) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self.bucket.acquire()
            try:
                resp = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(min(60.0, 2 ** attempt + random.uniform(0, 1)))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} from {url}", request=resp.request,
                    response=resp)
                await asyncio.sleep(min(60.0, 2 ** attempt + random.uniform(0, 1)))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Polymarket request failed after retries: {url}") from last_exc

    async def iter_active_markets(self, page_size: int = 200) -> AsyncIterator[dict]:
        """Active, unresolved binary markets from Gamma (offset pagination)."""
        offset = 0
        while True:
            page = await self._get(f"{self.gamma}/markets", params={
                "active": "true", "closed": "false", "archived": "false",
                "limit": page_size, "offset": offset,
            })
            markets = page if isinstance(page, list) else page.get("data", [])
            if not markets:
                return
            for m in markets:
                yield m
            if len(markets) < page_size:
                return
            offset += page_size

    async def get_book(self, token_id: str) -> dict:
        """CLOB book for one token: {"bids": [{price,size}], "asks": [...]}"""
        return await self._get(f"{self.clob}/book", params={"token_id": token_id})


def parse_clob_token_ids(market: dict) -> tuple[str | None, str | None]:
    """Returns (yes_token, no_token).

    clobTokenIds is ordered like the market's `outcomes` array; do not assume
    ["Yes","No"] — check outcomes and only accept unambiguous Yes/No binaries.
    """
    raw = market.get("clobTokenIds") or market.get("clob_token_ids")
    if raw is None:
        return None, None
    try:
        ids = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(ids, list) or len(ids) < 2:
        return None, None
    outcomes_raw = market.get("outcomes")
    try:
        outcomes = (json.loads(outcomes_raw) if isinstance(outcomes_raw, str)
                    else list(outcomes_raw) if outcomes_raw else ["Yes", "No"])
    except (ValueError, TypeError):
        return None, None
    outcomes = [str(o).strip().lower() for o in outcomes[:2]]
    if outcomes == ["yes", "no"]:
        return str(ids[0]), str(ids[1])
    if outcomes == ["no", "yes"]:
        return str(ids[1]), str(ids[0])
    return None, None  # not an unambiguous Yes/No binary — never guess


def book_to_kalshi_shape(book: dict) -> tuple[list, list]:
    """CLOB YES-token book -> (yes_bids, no_bids) in integer cents.

    bids on the YES token are YES bids; asks on the YES token at price p are
    equivalent to NO bids at 100 - p. Sizes are share counts (floats) —
    floored to whole contracts.
    """
    def levels(side: list, transform) -> list:
        out = []
        for lvl in side or []:
            try:
                price = float(lvl["price"]) if isinstance(lvl, dict) else float(lvl[0])
                size = float(lvl["size"]) if isinstance(lvl, dict) else float(lvl[1])
            except (KeyError, ValueError, TypeError, IndexError):
                continue
            cents = transform(round(price * 100))
            qty = int(size)
            if 0 < cents < 100 and qty > 0:
                out.append([cents, qty])
        return out

    yes_bids = levels(book.get("bids"), lambda c: c)
    no_bids = levels(book.get("asks"), lambda c: 100 - c)
    yes_bids.sort(key=lambda l: -l[0])
    no_bids.sort(key=lambda l: -l[0])
    return yes_bids, no_bids
