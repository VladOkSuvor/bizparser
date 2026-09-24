"""Сопоставление записей внешних источников с базой и их запись."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import automation as auto
from ..dedupe import haversine_m, name_similarity, normalize_name
from ..enrich import normalize_url
from ..extract import normalize_phone
from ..models import Business, utcnow
from ..sizing import estimate

# Внешний источник и OSM описывают одно место с разной точностью координат:
# точка входа vs геокод адреса. 30 м, как в гео-дедупе, тут мало.
MATCH_RADIUS_M = 150.0
MATCH_THRESHOLD = 0.8
# Без координат и адреса с обеих сторон матчим только по почти полному совпадению
# названия — и только если в городе такой кандидат один (у сетей филиалы тёзки)
NAME_ONLY_THRESHOLD = 0.95


@dataclass
class ExternalRecord:
    source: str  # nszu / likarni / doc_ua
    ext_id: str  # стабильный id внутри источника
    name: str
    category: str
    city: str
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    phones: list[str] = field(default_factory=list)
    email: str | None = None
    website: str | None = None
    contact_person: str | None = None
    edrpou: str | None = None
    automation: dict[str, list[str]] = field(default_factory=dict)
    # Пустую карточку агрегатора без контактов не заводим как новый лид — только матчим
    create_if_missing: bool = True

    @property
    def key(self) -> str:
        """Значение для Business.osm_id у записей, которых нет в OSM. Влезает в 64 символа."""
        ext = self.ext_id if len(self.ext_id) <= 40 else hashlib.sha1(self.ext_id.encode()).hexdigest()[:16]
        return f"{self.source}/{ext}"


# --- сравнение адресов ------------------------------------------------------

_STREET_WORDS = re.compile(
    r"\b(вул|вулиця|ул|улица|просп|проспект|пр-т|бульв|бульвар|бул|пров|провулок|пер|переулок"
    r"|пл|площа|площадь|шосе|шоссе|узвіз|спуск|будинок|буд|дом|д|корпус|корп|кв|офіс|оф"
    r"|україна|украина|місто|город|м|г|область|обл)\b\.?",
    re.I,
)


def _address_parts(address: str | None) -> tuple[str | None, set[str]]:
    """'вул. Велика Васильківська, 131' → ('131', {'велик', 'васил'})."""
    if not address:
        return None, set()
    text = _STREET_WORDS.sub(" ", address.lower().replace("ё", "е"))
    tokens = re.findall(r"\w+", text)
    house = next((t for t in tokens if t[0].isdigit()), None)
    # Первые 5 букв — грубый стемминг: «Васильківська» / «Васильківській»
    words = {t[:5] for t in tokens if t.isalpha() and len(t) >= 4}
    return house, words


def same_address(a: str | None, b: str | None) -> bool:
    house_a, words_a = _address_parts(a)
    house_b, words_b = _address_parts(b)
    if not house_a or not house_b or house_a != house_b:
        return False
    return bool(words_a & words_b)


# --- индекс базы ------------------------------------------------------------


class Matcher:
    """Индекс существующих записей: по внешнему id, телефону и городу."""

    def __init__(self, rows: list[Business]) -> None:
        self.by_key: dict[str, Business] = {}
        # Телефон сверяем только внутри города: у сети один номер на все филиалы
        self.by_phone: dict[tuple[str, str], Business] = {}
        self.by_city: dict[str, list[Business]] = defaultdict(list)
        for row in rows:
            self.add(row)

    def add(self, row: Business) -> None:
        self.by_key[row.osm_id] = row
        # Запись, схлопнутая гео-дедупом, живёт в merged_ids выжившей — не заводим её заново
        for merged_id in row.merged_ids or ():
            self.by_key.setdefault(merged_id, row)
        for source, ext_id in (row.external_ids or {}).items():
            self.by_key[f"{source}/{ext_id}"] = row
        if row.phone:
            self.by_phone.setdefault((row.city, row.phone), row)
        self.by_city[row.city].append(row)

    def match(self, rec: ExternalRecord) -> Business | None:
        found = self.by_key.get(rec.key) or self.by_key.get(f"{rec.source}/{rec.ext_id}")
        if found is not None:
            return found
        for phone in rec.phones:
            if (rec.city, phone) in self.by_phone:
                return self.by_phone[(rec.city, phone)]

        target = normalize_name(rec.name)
        best, best_score = None, 0.0
        name_only: list[Business] = []
        for row in self.by_city.get(rec.city, ()):
            score = name_similarity(target, normalize_name(row.name))
            if score < MATCH_THRESHOLD:
                continue
            if score >= NAME_ONLY_THRESHOLD:
                name_only.append(row)
            near = (
                rec.lat is not None and row.lat is not None
                and haversine_m(rec.lat, rec.lon, row.lat, row.lon) <= MATCH_RADIUS_M
            )
            if score > best_score and (near or same_address(rec.address, row.address)):
                best, best_score = row, score
        if best is None and len(name_only) == 1:
            return name_only[0]
        return best


_CITY_SUFFIX = re.compile(r"\s+(в|у|во)\s*$", re.I)


def clean_listing_name(name: str, city: str) -> str:
    """'Медікавер (Medicover), медичний центр в Ужгороді' → 'Медікавер (Medicover)'.

    Агрегаторы дописывают к названию вид учреждения и город (для SEO) —
    с названием из OSM такое не совпадёт.
    """
    head = name.split(",")[0].split(" — ")[0].strip()
    stem = city.lower()[:5]
    words = [w for w in head.split() if not w.lower().startswith(stem)]
    cleaned = _CITY_SUFFIX.sub("", " ".join(words)).strip()
    return cleaned or name.strip()


# --- запись -----------------------------------------------------------------


def _clean(rec: ExternalRecord) -> ExternalRecord:
    phones: list[str] = []
    for raw in rec.phones:
        phone = normalize_phone(raw)
        if phone and phone not in phones:
            phones.append(phone)
    rec.phones = phones
    rec.website = normalize_url(rec.website)
    rec.email = (rec.email or "").strip().lower() or None
    return rec


def _fill(row: Business, rec: ExternalRecord) -> bool:
    """Дозаполняет пустые поля. Ничего из уже собранного не затирает — как и _upsert в cli."""
    changed = False
    simple = {
        "address": rec.address, "lat": rec.lat, "lon": rec.lon, "website": rec.website,
        "contact_person": rec.contact_person, "edrpou": rec.edrpou,
    }
    for attr, value in simple.items():
        if value and not getattr(row, attr):
            setattr(row, attr, value)
            changed = True
    if rec.phones and not row.phone:
        row.phone = rec.phones[0]
        changed = True
    if rec.email and not row.email:
        row.email = rec.email
        row.email_valid = None
        changed = True

    extra = [p for p in rec.phones if p != row.phone and p not in (row.notes or "")]
    if extra:
        row.notes = f"{(row.notes or '')} доп.тел({rec.source}): {', '.join(extra[:2])}".strip()
        changed = True

    ids = dict(row.external_ids or {})
    if ids.get(rec.source) != rec.ext_id:
        ids[rec.source] = rec.ext_id
        row.external_ids = ids
        changed = True

    if rec.automation:
        # Глубокая копия: JSON-поле с изменённым на месте списком SQLAlchemy не заметит
        merged = {kind: list(vendors) for kind, vendors in (row.automation or {}).items()}
        before = auto.describe(merged)
        auto.merge(merged, rec.automation)
        # has_automation не трогаем: листинг на агрегаторе — слабый вид (не в
        # STRONG_KINDS), горячий лид от него холодным не становится
        if auto.describe(merged) != before:
            row.automation = merged
            changed = True

    if rec.source not in (row.source or "").split(","):
        row.source = f"{row.source},{rec.source}" if row.source else rec.source
        changed = True
    if changed:
        row.updated_at = utcnow()
    return changed


def _create(rec: ExternalRecord) -> Business:
    size, signals = estimate(None, rec.category)
    return Business(
        osm_id=rec.key,
        osm_type=rec.source,
        name=rec.name,
        category=rec.category,
        city=rec.city,
        address=rec.address,
        lat=rec.lat,
        lon=rec.lon,
        phone=rec.phones[0] if rec.phones else None,
        email=rec.email,
        website=rec.website,
        contact_person=rec.contact_person,
        edrpou=rec.edrpou,
        external_ids={rec.source: rec.ext_id},
        automation=rec.automation or None,
        notes=f"доп.тел: {', '.join(rec.phones[1:3])}" if len(rec.phones) > 1 else None,
        source=rec.source,
        size_estimate=size,
        size_signals=signals,
    )


@dataclass
class ApplyStats:
    new: int = 0
    updated: int = 0
    matched: int = 0  # совпало с тем, что было в базе до импорта
    merged: int = 0  # склеилось с записью из этого же импорта (филиалы с общим телефоном)
    skipped: int = 0  # не нашлось в базе, а создавать запрещено


def apply_records(session: Session, records, *, create: bool = True) -> ApplyStats:
    """Матчит поток ExternalRecord на базу: совпало — дозаполнить, нет — завести новый лид."""
    existing = list(session.scalars(select(Business)))
    before = {row.id for row in existing}
    matcher = Matcher(existing)
    stats = ApplyStats()
    for rec in records:
        rec = _clean(rec)
        row = matcher.match(rec)
        if row is not None:
            if row.id in before:
                stats.matched += 1
            else:
                stats.merged += 1
            if _fill(row, rec):
                stats.updated += 1
            continue
        if not (create and rec.create_if_missing):
            stats.skipped += 1
            continue
        row = _create(rec)
        session.add(row)
        session.flush()  # нужен id — по нему сравнивает гео-дедуп
        matcher.add(row)  # филиалы одного ФОПа с общим телефоном склеятся в один лид
        stats.new += 1
    return stats
