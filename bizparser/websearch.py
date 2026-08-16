"""Fallback-поиск сайта через HTML-выдачу DuckDuckGo.

ЧЕСТНОЕ ПРЕДУПРЕЖДЕНИЕ: ключа это не требует и страница публична, но
автоматический разбор выдачи — серая зона по их ToS, и при частых запросах
прилетает бан по IP. Поэтому здесь: выключено по умолчанию, большая пауза
между запросами и жёсткий лимит на пачку. Держи это для узкого списка
«перспективных» мест, а не как основной канал discovery.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

from selectolax.parser import HTMLParser

from .config import settings
from .http import build_client, request

log = logging.getLogger(__name__)

DDG_URL = "https://html.duckduckgo.com/html/"

# Агрегаторы и каталоги — это не сайт бизнеса
AGGREGATORS = {
    "olx.ua", "prom.ua", "rieltor.ua", "dom.ria.com", "ria.com", "2gis.ua", "2gis.ru",
    "yell.ru", "ua.kompass.com", "b2btoday.com.ua", "flagma.ua", "allbiz.ua", "ua.all.biz",
    "tripadvisor.com", "booking.com", "foursquare.com", "yelp.com", "zoon.com.ua",
    "google.com", "maps.google.com", "youtube.com", "wikipedia.org", "work.ua", "rabota.ua",
    "facebook.com", "instagram.com", "t.me", "linkedin.com", "pinterest.com",
}


def _unwrap(href: str) -> str | None:
    """DDG заворачивает ссылки в /l/?uddg=<urlencoded>."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [])
        return target[0] if target else None
    return href if parsed.scheme in ("http", "https") else None


def find_website(name: str, city: str, extra: str = "") -> str | None:
    """Ищет официальный сайт по названию + городу. Возвращает первый не-агрегатор."""
    query = " ".join(part for part in (name, city, extra, "офіційний сайт") if part)
    resp = None
    with build_client() as client:
        resp = request(
            client,
            "POST",
            DDG_URL,
            delay=settings.ddg_delay,
            retries=1,
            data={"q": query, "kl": "ua-uk"},
        )
    if resp is None:
        return None

    tree = HTMLParser(resp.text)
    for node in tree.css("a.result__a, a.result__url"):
        href = node.attributes.get("href") or ""
        url = _unwrap(href)
        if not url:
            continue
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if host in AGGREGATORS or any(host.endswith("." + a) for a in AGGREGATORS):
            continue
        return url.split("?")[0]
    return None
