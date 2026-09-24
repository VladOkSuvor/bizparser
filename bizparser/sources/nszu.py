"""НСЗУ: заклади й ФОП, що уклали договір за програмою медичних гарантій.

Почему не реестр лицензий МОЗ, как планировалось: у МОЗ машиночитаемой выгрузки
нет — на data.gov.ua его нет среди наборов министерства, а moz.gov.ua закрыт
Cloudflare-челленджем. Зато НСЗУ публикует на data.gov.ua открытые CSV, которые
обновляются ежедневно и для холодных звонков даже лучше лицензий:

  * `pmg_legal_entity_info.csv` — юрлицо/ФОП: ЄДРПОУ, форма собственности
    (Комунальна / Державна / Приватна / ФОП), телефон, email, сайт, **руководитель**;
  * `pmg_legal_entity_divisions_info.csv` — подразделения: адрес, координаты,
    свой телефон и email.

Ограничение: здесь только те, кто работает с НСЗУ (первичка — семейные врачи,
плюс специализированная помощь по договору). Частная стоматология или
косметология без договора с НСЗУ сюда не попадёт — их по-прежнему даёт OSM.
Лицензия МОЗ есть у всех, так что если МОЗ когда-нибудь выложит реестр
файлом — стоит подключить отдельно.
"""

from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..cities import CityIndex, normalize_city
from ..config import ROOT
from ..http import build_client, request
from ..publicsector import is_public_name
from .base import ExternalRecord

log = logging.getLogger(__name__)

DATASET_ID = "46fa40f9-875b-41ee-8d6f-be2cc3e40ace"
CKAN_API = "https://data.gov.ua/api/3/action/package_show"
FILES = {
    "legal": "pmg_legal_entity_info.csv",
    "divisions": "pmg_legal_entity_divisions_info.csv",
}
CACHE_DIR = ROOT / "data_cache" / "nszu"

PRIVATE_TYPES = {"ФОП", "Приватна (без ФОП)"}
# Екстрена — скорая помощь, для записи на приём не лид
CARE_CATEGORY = {"Первинна": "doctors", "Спеціалізована": "clinic"}
# ФАП — сельский пункт при амбулатории, отдельного ЛПР там нет
SKIP_DIVISION_TYPES = {"ФАП"}
# Сеть с таким числом подразделений — это уже не «владелец сам решает», а
# централизованный маркетинг, как у Добробута. Аналог chains.py для НСЗУ.
CHAIN_MIN_DIVISIONS = 10

LEGAL_FORMS = [
    (re.compile(r"ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ", re.I), "ТОВ"),
    (re.compile(r"ФІЗИЧНА ОСОБА[- ]ПІДПРИЄМЕЦЬ", re.I), "ФОП"),
    (re.compile(r"ПРИВАТНЕ ПІДПРИЄМСТВО", re.I), "ПП"),
    (re.compile(r"ПРИВАТНЕ АКЦІОНЕРНЕ ТОВАРИСТВО", re.I), "ПрАТ"),
    (re.compile(r"НЕКОМЕРЦІЙНЕ ПІДПРИЄМСТВО", re.I), "НП"),
]


def download(force: bool = False) -> dict[str, Path]:
    """Качает оба CSV в data_cache/. Ссылки берёт из CKAN API — они меняются при обновлении."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    paths = {kind: CACHE_DIR / name for kind, name in FILES.items()}
    if not force and all(p.exists() for p in paths.values()):
        return paths

    with build_client(timeout=120.0) as client:
        resp = request(client, "GET", CKAN_API, delay=1.0, params={"id": DATASET_ID})
        if resp is None:
            raise RuntimeError("data.gov.ua не ответил на запрос метаданных набора НСЗУ")
        resources = resp.json()["result"]["resources"]
        urls = {r.get("name"): r.get("url") for r in resources}
        for kind, name in FILES.items():
            url = urls.get(name)
            if not url:
                raise RuntimeError(f"В наборе НСЗУ нет файла {name} — формат поменялся?")
            data = request(client, "GET", url, delay=1.0)
            if data is None:
                raise RuntimeError(f"Не удалось скачать {name}")
            paths[kind].write_bytes(data.content)
            log.info("Скачан %s (%d КБ)", name, len(data.content) // 1024)
    return paths


def _read(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            yield {k: (v if v not in ("NULL", "") else None) for k, v in row.items()}


def short_name(legal_name: str) -> str:
    """'ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ "МЕДІКС"' → 'ТОВ «МЕДІКС»'."""
    name = legal_name.strip()
    for pattern, abbr in LEGAL_FORMS:
        name = pattern.sub(abbr, name)
    name = re.sub(r"\s+", " ", name).strip()
    # Кавычки в CSV бывают вложенными и непарными — снимаем все и ставим одни ёлочки
    form, _, rest = name.partition(" ")
    if form in {abbr for _, abbr in LEGAL_FORMS} and '"' in rest:
        core = rest.replace('"', "").strip(" «»")
        return f"{form} «{core}»"
    return name.replace('"', "")


def _street_address(full: str | None) -> str | None:
    """'ХМЕЛЬНИЦЬКА область, місто ІЗЯСЛАВ, вулиця Шевченка, 11' → 'вулиця Шевченка, 11'."""
    if not full:
        return None
    parts = [p.strip() for p in full.split(",")]
    skip = re.compile(r"(область|район|^україна$|^\d{5}$|^(місто|село|селище|смт|м\.)\s)", re.I)
    kept = [p for p in parts if p and not skip.search(p)]
    return ", ".join(kept) or None


def _same_oblast(area: str | None, oblast: str | None) -> bool:
    if not oblast:  # Київ: в CSV область «М.КИЇВ», сверять не с чем
        return True
    return normalize_city(area).startswith(normalize_city(oblast)[:5])


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


@dataclass
class NszuStats:
    public: int = 0
    other_city: int = 0
    emergency: int = 0
    fap: int = 0
    chains: int = 0


def records(
    paths: dict[str, Path], index: CityIndex, *, all_settlements: bool = False,
    stats: NszuStats | None = None,
) -> Iterator[ExternalRecord]:
    """По записи на подразделение частного поставщика; без подразделений — по юр. адресу."""
    stats = stats or NszuStats()
    legal: dict[str, dict] = {}
    for row in _read(paths["legal"]):
        if row["property_type"] not in PRIVATE_TYPES or is_public_name(row["legal_entity_name"]):
            stats.public += 1
            continue
        if row["care_type"] not in CARE_CATEGORY:
            stats.emergency += 1
            continue
        legal[row["legal_entity_id"]] = row

    sizes = Counter(div["legal_entity_id"] for div in _read(paths["divisions"]))
    for entity_id in [e for e in legal if sizes[e] >= CHAIN_MIN_DIVISIONS]:
        del legal[entity_id]
        stats.chains += 1

    with_divisions: set[str] = set()
    for div in _read(paths["divisions"]):
        entity = legal.get(div["legal_entity_id"])
        if entity is None:
            continue
        with_divisions.add(div["legal_entity_id"])
        if div["division_type"] in SKIP_DIVISION_TYPES:
            stats.fap += 1
            continue
        rec = _record(
            entity, index, all_settlements,
            ext_id=div["division_id"],
            settlement=div["residence_settlement"], area=div["residence_area"],
            address=div["residence_addresses"], lat=div["lat"], lon=div["lng"],
            phone=div["division_phone"], email=div["division_email"],
        )
        if rec is None:
            stats.other_city += 1
            continue
        yield rec

    for entity_id, entity in legal.items():
        if entity_id in with_divisions:
            continue
        rec = _record(
            entity, index, all_settlements,
            ext_id=entity_id,
            settlement=entity["registration_settlement"], area=entity["registration_area"],
            address=entity["registration_address"], lat=entity["lat"], lon=entity["lng"],
            phone=None, email=None,
        )
        if rec is None:
            stats.other_city += 1
            continue
        yield rec


def _record(
    entity: dict, index: CityIndex, all_settlements: bool, *, ext_id: str,
    settlement: str | None, area: str | None, address: str | None,
    lat: str | None, lon: str | None, phone: str | None, email: str | None,
) -> ExternalRecord | None:
    city = index.lookup(settlement)
    if city is not None and not _same_oblast(area, city.oblast):
        city = None  # тёзка из другой области
    if city is None and not all_settlements:
        return None
    if city is not None and city.excluded and not all_settlements:
        return None
    city_name = city.name if city else (settlement or "").title()

    is_fop = entity["property_type"] == "ФОП"
    # В одном поле бывает несколько номеров через запятую
    phones = [
        p.strip() for raw in (phone, entity["legal_entity_phone"]) if raw
        for p in raw.split(",") if p.strip()
    ]
    return ExternalRecord(
        source="nszu",
        ext_id=ext_id,
        name=short_name(entity["legal_entity_name"]),
        category=CARE_CATEGORY[entity["care_type"]],
        city=city_name,
        address=_street_address(address),
        lat=_float(lat),
        lon=_float(lon),
        phones=phones,
        email=email or entity["legal_entity_email"],
        website=entity["legal_entity_website"],
        contact_person=entity["legal_entity_owner_name"],
        # У ФОП в этом поле РНОКПП — личный налоговый номер. Не храним.
        edrpou=None if is_fop else entity["legal_entity_edrpou"],
    )
