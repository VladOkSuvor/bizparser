"""Асинхронный HTTP: разные хосты параллельно, один хост — не чаще, чем можно.

Синхронный обход упирался в latency: 100 сайтов × (1с пауза + ~1-2с ответ) — это
почти пять минут ожидания на ровном месте. Здесь пауза выдерживается для каждого
хоста отдельно, а разные хосты идут одновременно под общим семафором.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import httpx

from .config import settings

log = logging.getLogger(__name__)


class HostRateLimiter:
    """Не чаще одного запроса в `delay` секунд к каждому хосту."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        lock = self._locks.setdefault(host, asyncio.Lock())
        # Лок держим на время сна: иначе два корутина одного хоста
        # прочитают одинаковый _last и стартанут вместе.
        async with lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last.get(host, 0.0)
            wait_for = self.delay - elapsed
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last[host] = loop.time()


def build_async_client(*, follow_redirects: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": settings.user_agent,
            "Accept-Language": "uk,ru;q=0.9,en;q=0.8",
        },
        timeout=settings.http_timeout,
        follow_redirects=follow_redirects,
        # Сайты малого бизнеса нередко с кривыми/просроченными сертификатами;
        # нам отсюда нужен только публичный текст, поэтому не падаем на этом.
        verify=False,
        limits=httpx.Limits(max_connections=settings.enrich_concurrency * 2),
    )


# httpx оборачивает не все ошибки TLS/DNS в HTTPError
NETWORK_ERRORS = (httpx.HTTPError, OSError)


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    limiter: HostRateLimiter,
    *,
    retries: int = 2,
) -> httpx.Response | None:
    backoff = 3.0
    for attempt in range(1, retries + 1):
        await limiter.wait(url)
        try:
            resp = await client.get(url)
        except NETWORK_ERRORS as exc:
            log.debug("GET %s: %s (попытка %d/%d)", url, exc, attempt, retries)
            if attempt < retries:
                await asyncio.sleep(backoff)
                backoff *= 2
            continue

        if resp.status_code in (429, 502, 503, 504):
            log.debug("%s вернул %d", url, resp.status_code)
            if attempt < retries:
                await asyncio.sleep(backoff)
                backoff *= 2
            continue
        if resp.status_code >= 400:
            return None
        return resp
    return None
