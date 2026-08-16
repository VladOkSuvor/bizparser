"""Enrichment: досасываем контакты и признаки автоматизации с сайтов бизнесов.

Никаких ключей и API — обычный HTTP + разбор HTML. Ходим только по домену
самого бизнеса, уважаем robots.txt, держим паузу на каждый хост, но разные
хосты обходим параллельно.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Sequence
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

from . import automation as auto
from .ahttp import HostRateLimiter, build_async_client, fetch
from .config import settings
from .extract import (
    find_contact_pages,
    find_emails,
    find_json_ld_contacts,
    find_phones,
    find_socials,
    normalize_phone,
    tel_links,
    visible_text,
)

log = logging.getLogger(__name__)


def normalize_url(raw: str | None) -> str | None:
    """В OSM website пишут как попало: 'example.com', 'www.x.ua', 'http://…'."""
    if not raw:
        return None
    url = raw.strip().split(";")[0].split(" ")[0]
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return url


@dataclass
class SiteContacts:
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    automation: dict[str, list[str]] = field(default_factory=dict)
    pages_visited: int = 0
    error: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.phones or self.emails or self.socials or self.automation)


class RobotsCache:
    """Один robots.txt на хост за прогон, с защитой от гонки корутин."""

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def allowed(
        self, client: httpx.AsyncClient, url: str, limiter: HostRateLimiter
    ) -> bool:
        if not settings.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._cache:
                parser: RobotFileParser | None = None
                resp = await fetch(client, f"{origin}/robots.txt", limiter, retries=1)
                if resp is not None and resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                self._cache[origin] = parser  # None == robots.txt нет, значит можно
        parser = self._cache[origin]
        return True if parser is None else parser.can_fetch(settings.user_agent, url)


def _harvest(html: str, url: str, result: SiteContacts) -> HTMLParser:
    """Вытаскивает всё, что можно, из одной страницы."""
    # Детектор автоматизации — по сырому HTML, до вырезания скриптов:
    # виджеты живут именно в <script src> и inline-JS.
    auto.merge(result.automation, auto.detect(html))

    tree = HTMLParser(html)

    # tel: ссылки — самый чистый источник телефонов
    for raw in tel_links(html):
        phone = normalize_phone(raw)
        if phone and phone not in result.phones:
            result.phones.append(phone)

    # JSON-LD (schema.org/LocalBusiness и подвиды) — структурированные данные для
    # SEO/Google Business. Бывает единственным источником: номер в разметке есть,
    # а текстом на странице не отрендерен (JS-виджет, картинка, скрыт до клика).
    ld_phones, ld_emails = find_json_ld_contacts(tree)
    for phone in ld_phones:
        if phone not in result.phones:
            result.phones.append(phone)
    for email in ld_emails:
        if email not in result.emails:
            result.emails.append(email)

    for email in find_emails(html):
        if email not in result.emails:
            result.emails.append(email)

    for network, link in find_socials(tree, url).items():
        result.socials.setdefault(network, link)

    text = visible_text(tree)
    for phone in find_phones(text):
        if phone not in result.phones:
            result.phones.append(phone)

    return tree


async def scrape_site(
    client: httpx.AsyncClient,
    url: str,
    limiter: HostRateLimiter,
    robots: RobotsCache,
    max_pages: int | None = None,
) -> SiteContacts:
    """Главная + до N «контактных» страниц того же домена."""
    max_pages = max_pages or settings.max_pages_per_site
    result = SiteContacts()
    normalized = normalize_url(url)
    if not normalized:
        result.error = "bad_url"
        return result

    if not await robots.allowed(client, normalized, limiter):
        result.error = "robots_disallow"
        return result

    resp = await fetch(client, normalized, limiter, retries=2)
    if resp is None:
        result.error = "unreachable"
        return result
    if "html" not in resp.headers.get("content-type", ""):
        result.error = "not_html"
        return result

    result.pages_visited += 1
    final_url = str(resp.url)
    tree = _harvest(resp.text, final_url, result)

    for page in find_contact_pages(tree, final_url, limit=max_pages - 1):
        if result.pages_visited >= max_pages:
            break
        if not await robots.allowed(client, page, limiter):
            continue
        sub = await fetch(client, page, limiter, retries=1)
        if sub is None or "html" not in sub.headers.get("content-type", ""):
            continue
        result.pages_visited += 1
        _harvest(sub.text, str(sub.url), result)

    return result


async def scrape_many(
    items: Sequence[tuple[int, str]],
    on_result: Callable[[int, SiteContacts], None] | Callable[[int, SiteContacts], Awaitable[None]],
    concurrency: int | None = None,
) -> None:
    """items = [(business_id, url)]. `on_result` вызывается по мере готовности.

    Пауза settings.site_delay соблюдается для каждого хоста отдельно, поэтому
    несколько страниц одного сайта не бьют по нему очередью, а разные сайты
    качаются одновременно.
    """
    concurrency = concurrency or settings.enrich_concurrency
    limiter = HostRateLimiter(settings.site_delay)
    robots = RobotsCache()
    semaphore = asyncio.Semaphore(concurrency)

    async with build_async_client() as client:

        async def worker(biz_id: int, url: str) -> tuple[int, SiteContacts]:
            async with semaphore:
                try:
                    return biz_id, await scrape_site(client, url, limiter, robots)
                except Exception as exc:  # один битый сайт не должен ронять прогон
                    log.debug("scrape %s упал: %s", url, exc)
                    return biz_id, SiteContacts(error="crashed")

        tasks = [asyncio.create_task(worker(biz_id, url)) for biz_id, url in items]
        for completed in asyncio.as_completed(tasks):
            biz_id, result = await completed
            outcome = on_result(biz_id, result)
            if asyncio.iscoroutine(outcome):
                await outcome


async def check_sites(urls: Iterable[str]) -> dict[str, SiteContacts]:
    """Удобная точка входа для разовой проверки списка URL (например, из тестов)."""
    results: dict[str, SiteContacts] = {}
    items = list(enumerate(urls))
    index_to_url = dict(items)

    def collect(idx: int, res: SiteContacts) -> None:
        results[index_to_url[idx]] = res

    await scrape_many(items, collect)
    return results
