"""Список городов для discover-all и приведение названий городов из разных источников к одному виду.

Список — фиксированный конфиг (`data/cities_ua.json`), а не запрос к Nominatim каждый раз:
границы городов не двигаются, поэтому area ID после первого геокодинга кэшируется в БД
(таблица `city_areas`), и повторные прогоны Nominatim не трогают.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from .db import session_scope
from .geocode import Place, geocode_city
from .models import CityArea, utcnow

log = logging.getLogger(__name__)

DEFAULT_CITIES_FILE = Path(__file__).resolve().parent / "data" / "cities_ua.json"


@dataclass(frozen=True)
class City:
    name: str
    oblast: str | None = None
    pop: int = 0  # тысяч, примерно — только для порядка обхода
    ru: str | None = None
    doc_ua: str | None = None  # slug города в URL doc.ua
    likarni: str | None = None  # slug города в URL likarni.com
    old: tuple[str, ...] = field(default_factory=tuple)
    excluded: str | None = None  # причина пропуска по умолчанию

    @property
    def geocode_query(self) -> str:
        """Область в запросе разводит тёзок (Первомайськ есть и в Николаевской, и в Луганской)."""
        return f"{self.name}, {self.oblast} область" if self.oblast else self.name

    @property
    def aliases(self) -> set[str]:
        names = {self.name, *self.old}
        if self.ru:
            names.add(self.ru)
        return {normalize_city(n) for n in names}


def load_cities(path: Path | None = None) -> list[City]:
    """Читает JSON со списком городов. Порядок — по населению, крупные первыми."""
    raw = json.loads((path or DEFAULT_CITIES_FILE).read_text(encoding="utf-8"))
    items = raw["cities"] if isinstance(raw, dict) else raw
    cities = [
        City(
            name=item["name"],
            oblast=item.get("oblast"),
            pop=int(item.get("pop") or 0),
            ru=item.get("ru"),
            doc_ua=item.get("doc_ua"),
            likarni=item.get("likarni"),
            old=tuple(item.get("old") or ()),
            excluded=item.get("excluded"),
        )
        for item in items
    ]
    return sorted(cities, key=lambda c: -c.pop)


def select_cities(
    cities: list[City],
    *,
    only: list[str] | None = None,
    top: int | None = None,
    include_excluded: bool = False,
) -> list[City]:
    """Фильтр для discover-all. `only` включает даже исключённые города — это осознанный выбор."""
    if only:
        wanted = {normalize_city(n) for n in only}
        picked = [c for c in cities if c.aliases & wanted]
        missing = wanted - set().union(*(c.aliases for c in picked))
        if missing:
            raise ValueError(f"Нет в списке городов: {', '.join(sorted(missing))}")
        return picked
    picked = [c for c in cities if include_excluded or not c.excluded]
    return picked[:top] if top else picked


# --- нормализация названий --------------------------------------------------

_APOSTROPHES = re.compile(r"[’ʼ`´‘]")
_PREFIX = re.compile(r"^(м\.|місто|г\.|город|смт\.?|с\.)\s*", re.I)


def normalize_city(name: str | None) -> str:
    """'ІВАНО-ФРАНКІВСЬК' / 'м. Івано-Франківськ' / 'Кам’янське' → один и тот же ключ."""
    if not name:
        return ""
    value = _APOSTROPHES.sub("'", name.strip())
    value = _PREFIX.sub("", value)
    return re.sub(r"\s+", " ", value).lower().replace("ё", "е")


class CityIndex:
    """Любое написание города из источника (укр/рус/старое название/CAPS) → город из списка."""

    def __init__(self, cities: list[City]) -> None:
        self._by_alias: dict[str, City] = {}
        for city in cities:
            for alias in city.aliases:
                self._by_alias.setdefault(alias, city)

    def lookup(self, name: str | None) -> City | None:
        return self._by_alias.get(normalize_city(name))


# --- кэш area ID ------------------------------------------------------------


def resolve_area(city: str, query: str | None = None, country: str = "Ukraine") -> Place | None:
    """Place для Overpass: сначала из кэша в БД, иначе — Nominatim и запись в кэш."""
    key = normalize_city(city)
    with session_scope() as session:
        cached = session.scalar(select(CityArea).where(CityArea.key == key))
        if cached is not None:
            return Place(
                display_name=cached.display_name,
                osm_type=cached.osm_type,
                osm_id=cached.osm_id,
                lat=cached.lat,
                lon=cached.lon,
                bbox=tuple(cached.bbox),  # type: ignore[arg-type]
            )

    place = geocode_city(query or city, country)
    if place is None:
        return None

    with session_scope() as session:
        session.add(
            CityArea(
                key=key,
                name=city,
                display_name=place.display_name,
                osm_type=place.osm_type,
                osm_id=place.osm_id,
                lat=place.lat,
                lon=place.lon,
                bbox=list(place.bbox),
                cached_at=utcnow(),
            )
        )
    log.debug("Закэширована area для %s: %s", city, place.area_id)
    return place
