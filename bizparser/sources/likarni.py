"""likarni.com — каталог клиник с реальными телефонами клиник.

Их внутренний поиск (`*/search*`, `/ajax`) закрыт в robots.txt, поэтому идём
по публичным листингам города — `/kliniki/<город>/page/N` (клиники) и
`/clinics/<город>/page/N` (диагностика), — а из карточки клиники берём
schema.org JSON-LD: там название, телефон, email и адрес. Всё это разрешено
robots.txt, но страницы — чужой сервер, поэтому пауза как у DDG, а не как у
сайтов бизнесов.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

from ..cities import City
from ..config import settings
from ..extract import ld_nodes
from ..http import build_client, request
from ..chains import is_medical_chain
from ..publicsector import is_public_name
from .base import ExternalRecord, clean_listing_name

log = logging.getLogger(__name__)

BASE = "https://likarni.com"
SECTIONS = ("kliniki", "clinics")
CARD_RE = re.compile(r'href="(?:https://likarni\.com)?(/clinic/[\w\-]+)"')
MAX_PAGES = 200  # предохранитель от бесконечной пагинации

CATEGORY_HINTS = [
    (re.compile(r"стоматолог|dental|дент", re.I), "dentist"),
    (re.compile(r"лаборатор|laborator|аналіз|анализ", re.I), "lab"),
    (re.compile(r"реабіліт|реабилит|rehab|фізіотерап|физиотерап", re.I), "rehab"),
    (re.compile(r"косметолог|дерматолог|cosmetolog|aesthetic|естетичн|эстетич", re.I), "cosmetology"),
]


def guess_category(name: str, ld_type: str | None = None) -> str:
    if ld_type == "Dentist":
        return "dentist"
    for pattern, category in CATEGORY_HINTS:
        if pattern.search(name):
            return category
    return "clinic"


class Crawler:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.robots = RobotFileParser()
        resp = request(client, "GET", f"{BASE}/robots.txt", delay=settings.likarni_delay)
        self.robots.parse(resp.text.splitlines() if resp is not None else [])

    def get(self, path: str) -> str | None:
        url = f"{BASE}{path}"
        if settings.respect_robots and not self.robots.can_fetch(settings.user_agent, url):
            log.debug("robots.txt запрещает %s", url)
            return None
        resp = request(self.client, "GET", url, delay=settings.likarni_delay, retries=2)
        return resp.text if resp is not None else None

    def card_paths(self, slug: str) -> list[str]:
        """Все карточки клиник города по листингам, без дублей и в порядке появления."""
        seen: dict[str, None] = {}
        for section in SECTIONS:
            for page in range(1, MAX_PAGES + 1):
                path = f"/{section}/{slug}" + (f"/page/{page}" if page > 1 else "")
                html = self.get(path)
                if html is None:
                    break
                found = [p for p in CARD_RE.findall(html) if p not in seen]
                if not found:  # страница за последней отдаёт тот же список или пусто
                    break
                seen.update(dict.fromkeys(found))
        return list(seen)


def parse_card(html: str, path: str, city: City) -> ExternalRecord | None:
    """JSON-LD MedicalClinic/Dentist → ExternalRecord. None, если разметки нет."""
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            # У likarni в описаниях бывают сырые переводы строк — strict=False
            data = json.loads(node.text(strip=True), strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
        for item in ld_nodes(data):
            if not isinstance(item, dict) or item.get("@type") in ("BreadcrumbList", "Review"):
                continue
            full_name = (item.get("name") or "").strip()
            if not full_name or not (item.get("telephone") or item.get("address")):
                continue
            if is_medical_chain(full_name) or is_public_name(full_name):
                return None
            name = clean_listing_name(full_name, city.name)
            address = _street(item)
            phones = item.get("telephone") or []
            return ExternalRecord(
                source="likarni",
                ext_id=path.rsplit("/", 1)[-1],
                name=name,
                category=guess_category(full_name, item.get("@type")),
                city=city.name,
                address=address,
                phones=phones if isinstance(phones, list) else [phones],
                email=item.get("email"),
                # Сама карточка на агрегаторе — слабый сигнал «уже где-то принимают запись»
                automation={"listing": ["likarni"]},
            )
    return None


def _street(item: dict) -> str | None:
    location = item.get("location") or {}
    address = location.get("address") if isinstance(location, dict) else None
    if not isinstance(address, dict):
        address = item.get("address") if isinstance(item.get("address"), dict) else {}
    street = (address or {}).get("streetAddress")
    return street.strip(" ,") if isinstance(street, str) and street.strip() else None


# Номер, который стоит на стольких карточках разных клиник, — колл-центр самого
# likarni (подменный номер для записи через них), а не телефон клиники
SHARED_PHONE_MIN_CARDS = 3


def drop_platform_contacts(recs: list[ExternalRecord]) -> list[ExternalRecord]:
    """Убирает телефоны/почты агрегатора. Без них карточка — только сигнал, не новый лид."""
    counts = Counter(p for rec in recs for p in set(rec.phones))
    shared = {p for p, n in counts.items() if n >= SHARED_PHONE_MIN_CARDS}
    for rec in recs:
        rec.phones = [p for p in rec.phones if p not in shared]
        if rec.email and rec.email.lower().endswith("@likarni.com"):
            rec.email = None
        rec.create_if_missing = bool(rec.phones or rec.email)
    if shared:
        log.info("likarni.com: номера колл-центра агрегатора отброшены: %s", ", ".join(sorted(shared)))
    return recs


def records(city: City, limit: int | None = None) -> list[ExternalRecord]:
    """Все карточки города разом: подменный номер виден только на выборке целиком."""
    if not city.likarni:
        raise ValueError(f"Для {city.name} нет slug likarni.com в cities_ua.json")
    out: list[ExternalRecord] = []
    with build_client() as client:
        crawler = Crawler(client)
        paths = crawler.card_paths(city.likarni)
        if limit:
            paths = paths[:limit]
        log.info("likarni.com: %s — карточек %d, ~%.0f мин", city.name, len(paths),
                 len(paths) * settings.likarni_delay / 60)
        for path in paths:
            html = crawler.get(path)
            if html is None:
                continue
            rec = parse_card(html, path, city)
            if rec is not None:
                out.append(rec)
    return drop_platform_contacts(out)
