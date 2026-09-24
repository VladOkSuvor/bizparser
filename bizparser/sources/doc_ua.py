"""doc.ua — отметка «клиника уже принимает запись через doc.ua».

Прямых контактов клиник doc.ua не публикует: на карточке — только номера их
собственного колл-центра (337-07-07), запись идёт через них. Их внутренний
поиск (`/api/`) закрыт в robots.txt. Поэтому модуль скромный: список карточек
берём из открытого sitemap (`clinic_card.xml`), из карточки — название и адрес,
и матчим на уже собранные лиды. Совпавшим ставится `automation.listing=doc.ua`:
клиника платит агрегатору комиссию за запись — готовый аргумент для питча
своего бота. Новых лидов без телефона по умолчанию не создаём (`--add-new`).
"""

from __future__ import annotations

import json
import logging
import re
from urllib.robotparser import RobotFileParser

from selectolax.parser import HTMLParser

from ..cities import City
from ..config import settings
from ..http import build_client, request
from ..chains import is_medical_chain
from ..publicsector import is_public_name
from .base import ExternalRecord, clean_listing_name
from .likarni import guess_category

log = logging.getLogger(__name__)

BASE = "https://doc.ua"
SITEMAP = f"{BASE}/v2/sitemap/clinic_card.xml"
# Только украиноязычные версии карточек — названия совпадут с OSM лучше, чем русские
CARD_RE = re.compile(r"<loc>(https://doc\.ua/ua/klinika/([\w\-]+)/[^<]+)</loc>")


def card_urls(xml: str, slug: str) -> list[str]:
    return [url for url, city_slug in CARD_RE.findall(xml) if city_slug == slug]


def parse_card(html: str, url: str, city: City) -> ExternalRecord | None:
    tree = HTMLParser(html)
    name = None
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text(strip=True), strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
        # Их MedicalClinic отдаёт "name": "undefined", живое название — в хлебных крошках
        if isinstance(data, dict) and data.get("@type") == "BreadcrumbList":
            items = data.get("itemListElement") or []
            if items:
                name = ((items[-1].get("item") or {}).get("name") or "").strip()
    if not name:
        return None
    if is_medical_chain(name) or is_public_name(name):
        return None
    category = guess_category(name)
    # «Abbe Optic — офтальмологічний центр … на Великій Васильківській» → «Abbe Optic»
    name = clean_listing_name(name, city.name)
    address_node = tree.css_first(".address__name")
    return ExternalRecord(
        source="doc_ua",
        ext_id=url.rstrip("/").rsplit("/", 1)[-1],
        name=name,
        category=category,
        city=city.name,
        address=address_node.text(strip=True) if address_node else None,
        automation={"listing": ["doc.ua"]},
        create_if_missing=False,
    )


def records(city: City, limit: int | None = None, add_new: bool = False) -> list[ExternalRecord]:
    if not city.doc_ua:
        raise ValueError(f"Для {city.name} нет slug doc.ua в cities_ua.json")
    out: list[ExternalRecord] = []
    with build_client() as client:
        robots = RobotFileParser()
        resp = request(client, "GET", f"{BASE}/robots.txt", delay=settings.doc_ua_delay)
        robots.parse(resp.text.splitlines() if resp is not None else [])

        sitemap = request(client, "GET", SITEMAP, delay=settings.doc_ua_delay)
        if sitemap is None:
            raise RuntimeError("doc.ua: sitemap недоступен")
        urls = card_urls(sitemap.text, city.doc_ua)
        if limit:
            urls = urls[:limit]
        log.info("doc.ua: %s — карточек %d, ~%.0f мин", city.name, len(urls),
                 len(urls) * settings.doc_ua_delay / 60)

        for url in urls:
            if settings.respect_robots and not robots.can_fetch(settings.user_agent, url):
                continue
            page = request(client, "GET", url, delay=settings.doc_ua_delay, retries=2)
            if page is None:
                continue
            rec = parse_card(page.text, url, city)
            if rec is not None:
                rec.create_if_missing = add_new
                out.append(rec)
    return out
