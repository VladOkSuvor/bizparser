"""Nominatim: превращаем "Київ" в area ID для Overpass."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import settings
from .http import build_client, request

log = logging.getLogger(__name__)

# Overpass кодирует area ID как смещение от OSM ID объекта-границы
AREA_OFFSET = {"relation": 3_600_000_000, "way": 2_400_000_000}


@dataclass
class Place:
    display_name: str
    osm_type: str
    osm_id: int
    lat: float
    lon: float
    bbox: tuple[float, float, float, float]  # south, north, west, east

    @property
    def area_id(self) -> int | None:
        offset = AREA_OFFSET.get(self.osm_type)
        return offset + self.osm_id if offset else None

    @property
    def bbox_clause(self) -> str:
        s, n, w, e = self.bbox
        return f"({s},{w},{n},{e})"


def geocode_city(city: str, country: str | None = "Ukraine") -> Place | None:
    """Ищет город и возвращает объект границы (relation/way), пригодный как area."""
    params = {
        "q": f"{city}, {country}" if country else city,
        "format": "jsonv2",
        "limit": "5",
        "addressdetails": "0",
    }
    with build_client() as client:
        resp = request(
            client,
            "GET",
            f"{settings.nominatim_url}/search",
            delay=settings.nominatim_delay,
            params=params,
        )
    if resp is None:
        return None

    try:
        results = resp.json()
    except ValueError:
        # Как и Overpass, Nominatim под нагрузкой иногда отдаёт не-JSON
        # (HTML-страницу ошибки) вместо валидного ответа.
        log.error("Nominatim вернул не-JSON (вероятно, перегружен): %s", resp.text[:200])
        return None
    if not results:
        log.error("Nominatim ничего не нашёл по запросу %r", city)
        return None

    # Нужен именно полигон границы — node как area работать не будет
    for item in results:
        if item.get("osm_type") in AREA_OFFSET:
            bb = [float(x) for x in item["boundingbox"]]
            return Place(
                display_name=item["display_name"],
                osm_type=item["osm_type"],
                osm_id=int(item["osm_id"]),
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                bbox=(bb[0], bb[1], bb[2], bb[3]),
            )

    log.error("Для %r нашлись только точечные объекты без границы", city)
    return None
