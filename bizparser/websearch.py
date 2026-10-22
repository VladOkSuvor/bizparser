"""Fallback-поиск сайта через HTML-выдачу DuckDuckGo.

ЧЕСТНОЕ ПРЕДУПРЕЖДЕНИЕ: ключа это не требует и страница публична, но
автоматический разбор выдачи — серая зона по их ToS, и при частых запросах
прилетает бан по IP. Поэтому здесь: выключено по умолчанию, большая пауза
между запросами и жёсткий лимит на пачку. Держи это для узкого списка
«перспективных» мест, а не как основной канал discovery.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from selectolax.parser import HTMLParser

from .config import settings
from .extract import normalize_instagram
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


class SearchFailed(Exception):
    """DDG не ответил или показал капчу. Это не «сайта нет» — искать надо повторить позже."""


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str


def search(query: str) -> list[SearchResult]:
    """Одна выдача DDG. SearchFailed — запрос не прошёл (сеть, бан, капча)."""
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
        raise SearchFailed("нет ответа")
    # При подозрении на бота DDG отдаёт 200 со страницей-капчей вместо выдачи
    if "anomaly-modal" in resp.text or "challenge-form" in resp.text:
        raise SearchFailed("капча / антибот")

    results = []
    for block in HTMLParser(resp.text).css("div.result"):
        link = block.css_first("a.result__a") or block.css_first("a.result__url")
        url = _unwrap(link.attributes.get("href") or "") if link else None
        if not url:
            continue
        snippet = block.css_first(".result__snippet")
        results.append(SearchResult(
            url=url,
            title=link.text(strip=True),
            snippet=snippet.text(strip=True) if snippet else "",
        ))
    return results


def find_website(name: str, city: str, extra: str = "") -> str | None:
    """Ищет официальный сайт по названию + городу. Возвращает первый не-агрегатор.

    None — выдача честно пришла, но сайта в ней нет. Сбой запроса или бан
    по IP — SearchFailed: это разные состояния для website_status.
    """
    query = " ".join(part for part in (name, city, extra, "офіційний сайт") if part)
    for result in search(query):
        host = urlparse(result.url).netloc.lower().removeprefix("www.")
        if host in AGGREGATORS or any(host.endswith("." + a) for a in AGGREGATORS):
            continue
        return result.url.split("?")[0]
    return None


# «SILVERDENT – стоматологічний центр» → «SILVERDENT»: в точной фразе хвост-описание
# почти никогда не совпадает с именем профиля, и выдача приходит пустой
NAME_TAIL_RE = re.compile(r"\s+[–—-]\s+.*$|\s*[|,(].*$")


def _search_name(name: str) -> str:
    short = NAME_TAIL_RE.sub("", name).strip(" «»\"'")
    return short or name.strip()


def search_instagram(name: str, city: str) -> list[SearchResult]:
    """Профили Instagram из выдачи `site:instagram.com "<name>" <city>`.

    url уже нормализован до https://instagram.com/<username>; посты, рилсы и
    прочие не-профили отброшены. Подходит ли профиль лиду — решает instagram.py.
    """
    query = f'site:instagram.com "{_search_name(name)}" {city}'.strip()
    profiles, seen = [], set()
    for result in search(query):
        url = normalize_instagram(result.url)
        if url and url not in seen:
            seen.add(url)
            profiles.append(SearchResult(url=url, title=result.title, snippet=result.snippet))
    return profiles
