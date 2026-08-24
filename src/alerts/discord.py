"""Discord webhook alerts. Silent no-op when no webhook URL is configured."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

MAX_LEN = 1900  # Discord hard limit is 2000; leave headroom


class DiscordAlerter:
    def __init__(self, webhook_url: str = "", timeout: float = 15.0):
        self.webhook_url = webhook_url
        self._client = httpx.AsyncClient(timeout=timeout) if webhook_url else None

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()

    async def send(self, content: str) -> bool:
        if not self._client:
            log.debug("discord disabled; message dropped: %.80s", content)
            return False
        ok = True
        for chunk in _chunks(content, MAX_LEN):
            ok = await self._post(chunk) and ok
        return ok

    async def _post(self, chunk: str) -> bool:
        for attempt in range(4):
            try:
                resp = await self._client.post(self.webhook_url,
                                               json={"content": chunk})
            except httpx.HTTPError as exc:
                log.warning("discord post failed: %s", exc)
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                retry = 2.0
                try:
                    retry = float(resp.json().get("retry_after", retry))
                except Exception:
                    pass
                await asyncio.sleep(retry)
                continue
            if resp.status_code >= 400:
                log.warning("discord post rejected: %s %s",
                            resp.status_code, resp.text[:200])
                return False
            return True
        return False

    async def error(self, context: str, exc: BaseException) -> None:
        await self.send(f":rotating_light: **Scanner error** in {context}: "
                        f"`{type(exc).__name__}: {exc}`")

    async def heartbeat(self, detail: str) -> None:
        await self.send(f":green_heart: heartbeat — {detail}")


def _chunks(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit:
            out.append(cur)
            cur = ""
        while len(line) > limit:      # pathological single long line
            out.append(line[:limit])
            line = line[limit:]
        cur += line
    if cur:
        out.append(cur)
    return out
