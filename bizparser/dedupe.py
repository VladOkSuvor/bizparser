"""Гео-дедуп: схлопываем один и тот же бизнес, пришедший как node и как way.

`nwr` намеренно тянет node+way+relation, поэтому заведение легко прилетает дважды:
точка входа и контур здания. osm_id у них разный, и без второго прохода в базе
оказываются дубли — а значит, врут stats и раздувается список для обзвона.

Критерий склейки: похожее нормализованное название И расстояние меньше радиуса.
Одного названия мало (сетевики), одного расстояния — тоже (ТЦ, где в одной точке
десяток разных арендаторов).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from math import asin, cos, radians, sin, sqrt

from .config import settings
from .models import Business, as_utc

log = logging.getLogger(__name__)

EARTH_RADIUS_M = 6_371_000.0

# Родовые слова, которые не помогают отличить одно заведение от другого
STOPWORDS = {
    "салон", "студія", "студия", "студio", "studio", "тату", "tattoo", "tatoo",
    "барбершоп", "barbershop", "barber", "перукарня", "парикмахерская", "beauty",
    "клініка", "клиника", "clinic", "медичний", "центр", "center", "centre",
    "стоматологія", "стоматология", "dental", "кафе", "cafe", "ресторан",
    "restaurant", "бар", "bar", "магазин", "shop", "store", "маркет", "market",
    "тов", "фоп", "ооо", "чп", "пп", "llc", "ltd", "спа", "spa", "краси", "красоты",
}

PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
SPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """'Тату-студія «Слон»' -> 'слон'. Пустая строка, если остались одни стопворды."""
    lowered = PUNCT_RE.sub(" ", name.lower().replace("ё", "е"))
    words = [w for w in SPACE_RE.split(lowered) if w and w not in STOPWORDS]
    if not words:  # название состоит только из родовых слов — сравниваем как есть
        words = [w for w in SPACE_RE.split(lowered) if w]
    return " ".join(words)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    dp = p2 - p1
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(a))


def name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Вложенность («слон» ⊂ «слон на подолі») — частый случай для одного места
    if a in b or b in a:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class MergePair:
    keep: Business
    drop: Business
    distance_m: float
    similarity: float

    def describe(self) -> str:
        return (
            f"{self.drop.osm_id} → {self.keep.osm_id} | {self.drop.name!r} ≈ {self.keep.name!r} "
            f"| {self.distance_m:.0f} м, похожесть {self.similarity:.2f}"
        )


def _cell(lat: float, lon: float, size_deg: float) -> tuple[int, int]:
    return int(lat / size_deg), int(lon / size_deg)


def find_duplicates(
    rows: list[Business],
    radius_m: float | None = None,
    threshold: float | None = None,
) -> list[MergePair]:
    """Ищет пары-дубли. Сравнивает только соседей по сетке, а не всех со всеми."""
    radius_m = radius_m if radius_m is not None else settings.dedupe_radius_m
    threshold = threshold if threshold is not None else settings.dedupe_threshold

    # Ячейка чуть больше радиуса, чтобы хватало проверки 3×3 соседей
    size_deg = max(radius_m / 111_000.0, 1e-5) * 1.5
    grid: dict[tuple[int, int], list[Business]] = defaultdict(list)
    for row in rows:
        if row.lat is None or row.lon is None:
            continue
        grid[_cell(row.lat, row.lon, size_deg)].append(row)

    normalized = {row.id: normalize_name(row.name) for row in rows}
    seen_pairs: set[tuple[int, int]] = set()
    pairs: list[MergePair] = []

    for (cx, cy), bucket in grid.items():
        neighbours: list[Business] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbours.extend(grid.get((cx + dx, cy + dy), ()))

        for left in bucket:
            for right in neighbours:
                if left.id >= right.id:
                    continue
                key = (left.id, right.id)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)

                # Разные категории — обычно разные арендаторы в одном здании
                if left.category != right.category:
                    continue
                distance = haversine_m(left.lat, left.lon, right.lat, right.lon)
                if distance > radius_m:
                    continue
                similarity = name_similarity(normalized[left.id], normalized[right.id])
                if similarity < threshold:
                    continue

                keep, drop = _pick_survivor(left, right)
                pairs.append(MergePair(keep, drop, distance, similarity))

    return pairs


def _pick_survivor(a: Business, b: Business) -> tuple[Business, Business]:
    """Выживает запись с бо́льшим числом контактов; при равенстве — более ранняя."""
    rank_a = (a.contact_score, bool(a.automation), bool(a.address), -a.id)
    rank_b = (b.contact_score, bool(b.automation), bool(b.address), -b.id)
    return (a, b) if rank_a >= rank_b else (b, a)


MERGE_FIELDS = (
    "address", "lat", "lon", "phone", "website", "email", "raw_tags",
    "size_estimate", "size_signals", "automation", "has_automation",
)


def merge_pair(pair: MergePair) -> None:
    """Переливает непустые поля из drop в keep. Сам drop не удаляет — это делает вызывающий."""
    keep, drop = pair.keep, pair.drop

    for attr in MERGE_FIELDS:
        if getattr(keep, attr) in (None, "") and getattr(drop, attr) not in (None, ""):
            setattr(keep, attr, getattr(drop, attr))

    if drop.socials:
        merged = dict(keep.socials or {})
        for network, link in drop.socials.items():
            merged.setdefault(network, link)
        keep.socials = merged

    sources = set((keep.source or "").split(",")) | set((drop.source or "").split(","))
    keep.source = ",".join(sorted(s for s in sources if s))

    ids = list(keep.merged_ids or [])
    ids.append(drop.osm_id)
    ids.extend(drop.merged_ids or [])
    keep.merged_ids = sorted(set(ids))

    if drop.notes:
        keep.notes = f"{(keep.notes or '')} {drop.notes}".strip()

    # Не теряем факт проверки сайта: берём самую свежую дату из двух
    for attr in ("enriched_at", "last_verified_at"):
        theirs, ours = as_utc(getattr(drop, attr)), as_utc(getattr(keep, attr))
        if theirs and (ours is None or theirs > ours):
            setattr(keep, attr, theirs)
