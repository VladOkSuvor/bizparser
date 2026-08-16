"""Overpass API — discovery. Бесплатно, без ключа, без биллинга."""

from __future__ import annotations

import logging
from typing import Iterable, Iterator

from . import chains
from .config import settings
from .geocode import Place
from .http import build_client, request

log = logging.getLogger(__name__)


def _tag_filter(spec: str) -> str:
    """'shop=beauty][beauty=nails' -> '["shop"="beauty"]["beauty"="nails"]', 'craft=*' -> '["craft"]'."""
    parts = []
    for chunk in spec.split("]["):
        key, _, value = chunk.partition("=")
        key = key.strip()
        value = value.strip()
        if not value or value == "*":
            parts.append(f'["{key}"]')
        else:
            parts.append(f'["{key}"="{value}"]')
    return "".join(parts)


def build_query(place: Place, filters: Iterable[str], timeout: int = 180) -> str:
    """Собирает Overpass QL. Использует area, если у города есть граница, иначе bbox."""
    area_id = place.area_id
    if area_id:
        header = f"area({area_id})->.searchArea;"
        scope = "(area.searchArea)"
    else:  # деградация до прямоугольника — грубее, но лучше чем ничего
        header = ""
        scope = place.bbox_clause

    # nwr = node + way + relation: салон может быть и точкой, и контуром здания
    body = "\n".join(f"  nwr{_tag_filter(f)}{scope};" for f in filters)
    return f"[out:json][timeout:{timeout}];\n{header}\n(\n{body}\n);\nout center tags;"


def fetch(query: str) -> list[dict]:
    """Выполняет запрос. Возвращает сырые elements."""
    with build_client(timeout=max(settings.http_timeout, 200.0)) as client:
        resp = request(
            client,
            "POST",
            settings.overpass_url,
            delay=settings.overpass_delay,
            data={"data": query},
        )
    if resp is None:
        return []
    try:
        return resp.json().get("elements", [])
    except ValueError:
        # Overpass при перегрузе отдаёт HTML-страницу с ошибкой вместо JSON
        log.error("Overpass вернул не-JSON (вероятно, перегружен): %s", resp.text[:200])
        return []


# --- разбор элементов ------------------------------------------------------

PHONE_KEYS = ("contact:phone", "phone", "contact:mobile", "mobile", "contact:whatsapp")
WEBSITE_KEYS = ("contact:website", "website", "url", "contact:url")
EMAIL_KEYS = ("contact:email", "email")
SOCIAL_KEYS = {
    "instagram": ("contact:instagram", "instagram"),
    "facebook": ("contact:facebook", "facebook"),
    "telegram": ("contact:telegram", "telegram"),
    "tiktok": ("contact:tiktok",),
    "viber": ("contact:viber",),
    "youtube": ("contact:youtube",),
}


def _first(tags: dict, keys: Iterable[str]) -> str | None:
    for key in keys:
        value = tags.get(key)
        if value:
            return value.strip()
    return None


def _address(tags: dict) -> str | None:
    street = tags.get("addr:street")
    house = tags.get("addr:housenumber")
    city = tags.get("addr:city")
    parts = [" ".join(p for p in (street, house) if p), city]
    joined = ", ".join(p for p in parts if p)
    return joined or tags.get("address") or None


def parse_elements(
    elements: list[dict], *, category: str, city: str, skip_chains: bool = True
) -> Iterator[dict]:
    """Сырые elements → плоские словари под модель Business."""
    skipped_chains = 0
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("name:uk") or tags.get("brand")
        if not name:
            continue  # безымянные POI для холодного контакта бесполезны

        if skip_chains and chains.is_big_chain(tags):
            skipped_chains += 1
            continue

        center = el.get("center") or {}
        socials = {}
        for network, keys in SOCIAL_KEYS.items():
            value = _first(tags, keys)
            if value:
                socials[network] = value

        yield {
            "osm_id": f"{el['type']}/{el['id']}",
            "osm_type": el["type"],
            "name": name.strip(),
            "category": category,
            "city": city,
            "address": _address(tags),
            "lat": el.get("lat") or center.get("lat"),
            "lon": el.get("lon") or center.get("lon"),
            "phone": _first(tags, PHONE_KEYS),
            "website": _first(tags, WEBSITE_KEYS),
            "email": _first(tags, EMAIL_KEYS),
            "socials": socials or None,
            "raw_tags": tags,
            "source": "osm",
        }

    if skipped_chains:
        log.info("Пропущено крупных сетей: %d", skipped_chains)
