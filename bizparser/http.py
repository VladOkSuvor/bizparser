"""Общий HTTP-клиент с честным rate-limit'ом по хостам.

Правило простое: публичные бесплатные сервисы живут на пожертвованиях,
и единственная причина, по которой они не требуют ключ — что их не долбят.
Здесь троттлинг вшит в клиент, а не оставлен на дисциплину вызывающего кода.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

import httpx

from .config import settings

log = logging.getLogger(__name__)

_last_hit: dict[str, float] = {}
_lock = threading.Lock()

# httpx оборачивает не все ошибки TLS/DNS в HTTPError (см. ahttp.py — тот же троттлинг,
# только асинхронный, столкнулся с этим раньше)
NETWORK_ERRORS = (httpx.HTTPError, OSError)


def throttle(url: str, delay: float) -> None:
    """Блокирует поток, пока с последнего запроса к этому хосту не пройдёт `delay`."""
    host = urlparse(url).netloc
    with _lock:
        elapsed = time.monotonic() - _last_hit.get(host, 0.0)
        wait = delay - elapsed
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.monotonic()


def build_client(*, follow_redirects: bool = True, timeout: float | None = None) -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": settings.user_agent,
            "Accept-Language": "uk,ru;q=0.9,en;q=0.8",
        },
        timeout=timeout or settings.http_timeout,
        follow_redirects=follow_redirects,
    )


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    delay: float,
    retries: int = 3,
    **kwargs,
) -> httpx.Response | None:
    """Запрос с троттлингом и backoff'ом. Возвращает None, если не вышло."""
    backoff = 5.0
    for attempt in range(1, retries + 1):
        throttle(url, delay)
        try:
            resp = client.request(method, url, **kwargs)
        except NETWORK_ERRORS as exc:
            log.warning("%s %s: %s (попытка %d/%d)", method, url, exc, attempt, retries)
            time.sleep(backoff)
            backoff *= 2
            continue

        # 429/504 у Overpass означают «слишком много», а не «сломалось»
        if resp.status_code in (429, 502, 503, 504):
            log.warning("%s вернул %d, жду %.0fс", url, resp.status_code, backoff)
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code >= 400:
            log.info("%s вернул %d — пропускаю", url, resp.status_code)
            return None

        return resp

    log.error("Не удалось получить %s за %d попыток", url, retries)
    return None
