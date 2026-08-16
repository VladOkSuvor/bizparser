"""Google Places API (New) — точечное платное дообогащение, выключено по умолчанию.

Не основной канал: OSM бесплатен и покрывает discovery. Places включается только
там, где своих данных объективно не хватает или лид уже «горячий» — см.
`needs_lookup()`. Сверх месячного потолка (`ApiUsage`) код останавливает себя сам,
не дожидаясь квоты в Google Cloud Console.

Два запроса вместо одного Place Details:
  1. Text Search с field mask `places.id` — самый дешёвый SKU. `place_id`
     кэшируется НАВСЕГДА в `Business.google_place_id`: повторный поиск по тому
     же месту — деньги на ветер, дальше всегда идём через Place Details.
  2. Place Details по кэшированному id, field mask ограничен именем/адресом/
     телефоном/часами работы.

**Важно про тариф**: `internationalPhoneNumber` и `regularOpeningHours` сами по
себе уже попадают в SKU Enterprise ($35/1000 на момент написания), а не Pro
($32/1000) — Pro покрывает только `displayName`/`formattedAddress`/`location`.
Раз телефон и часы работы всё равно поднимают запрос до Enterprise, `rating` и
`reviews` сюда сознательно не добавлены — тариф от них так и так не «Enterprise»,
а `Enterprise + Atmosphere` (дороже), а для лидогена они не нужны. Актуальные
тарифы/SKU — в Google Cloud Console, Google их периодически пересматривает.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from .config import settings
from .db import session_scope
from .http import build_client, request
from .models import ApiUsage, Business, utcnow

log = logging.getLogger(__name__)

ENABLED = bool(settings.google_places_api_key)

FIELD_MASK_ID_ONLY = "places.id"
FIELD_MASK_DETAILS = "id,displayName,formattedAddress,internationalPhoneNumber,regularOpeningHours"


@dataclass
class LookupResult:
    biz_id: int
    place_id: str | None = None
    phone: str | None = None
    address: str | None = None
    raw: dict | None = None
    called: int = 0  # сколько реальных HTTP-вызовов ушло — считается в бюджет


def needs_lookup(biz: Business) -> bool:
    """Гейт: только тем, кому реально не хватает данных, или горячим лидам.

    «Горячий» — то же определение, что в `stats`/`list --no-automation`:
    сайт проверен, автоматизации нет, прямой контакт уже есть.
    """
    if biz.google_checked_at is not None:
        return False  # уже обогащали — дальше только из кэша, повторно не ходим
    missing_after_enrich = biz.last_verified_at is not None and not biz.phone
    is_hot = biz.has_automation is False and bool(biz.phone or biz.email)
    return missing_after_enrich or is_hot


def _current_period() -> str:
    return date.today().strftime("%Y-%m")


def usage_this_month() -> int:
    with session_scope() as session:
        row = session.scalar(
            select(ApiUsage).where(
                ApiUsage.service == "google_places", ApiUsage.period == _current_period()
            )
        )
        return row.calls if row else 0


def budget_left() -> int:
    return max(settings.google_places_monthly_cap - usage_this_month(), 0)


def _record_calls(n: int) -> None:
    if n <= 0:
        return
    period = _current_period()
    with session_scope() as session:
        row = session.scalar(
            select(ApiUsage).where(ApiUsage.service == "google_places", ApiUsage.period == period)
        )
        if row is None:
            row = ApiUsage(service="google_places", period=period, calls=0)
            session.add(row)
        row.calls += n


def _headers(field_mask: str) -> dict:
    return {
        "X-Goog-Api-Key": settings.google_places_api_key,
        "X-Goog-FieldMask": field_mask,
        "Content-Type": "application/json",
    }


def lookup(biz: Business) -> LookupResult:
    """Один лид — до двух платных вызовов. Синхронно и последовательно: это не
    массовый скрапинг, а деньги под ручным потолком, гнаться за скоростью незачем.
    """
    result = LookupResult(biz_id=biz.id, place_id=biz.google_place_id)
    with build_client(timeout=settings.http_timeout) as client:
        if result.place_id is None:
            query = f"{biz.name}, {biz.address}" if biz.address else f"{biz.name}, {biz.city}"
            resp = request(
                client, "POST", f"{settings.google_places_url}/places:searchText",
                delay=settings.google_places_delay, retries=1,
                headers=_headers(FIELD_MASK_ID_ONLY), json={"textQuery": query},
            )
            result.called += 1
            places = (resp.json().get("places") or []) if resp is not None else []
            if not places:
                return result
            result.place_id = places[0]["id"]

        resp = request(
            client, "GET", f"{settings.google_places_url}/places/{result.place_id}",
            delay=settings.google_places_delay, retries=1,
            headers=_headers(FIELD_MASK_DETAILS),
        )
        result.called += 1
        if resp is None:
            return result
        data = resp.json()
        result.raw = data
        result.phone = data.get("internationalPhoneNumber")
        result.address = data.get("formattedAddress")
    return result


def apply_result(result: LookupResult) -> bool:
    """Пишет результат в БД и сразу списывает вызовы в бюджет. True — если что-то дозаполнили."""
    _record_calls(result.called)
    changed = False
    with session_scope() as session:
        row = session.get(Business, result.biz_id)
        if row is None:
            return False
        row.google_checked_at = utcnow()
        if result.place_id:
            row.google_place_id = result.place_id
        if result.raw:
            row.google_places_data = result.raw
        if result.phone and not row.phone:
            row.phone = result.phone
            changed = True
        if result.address and not row.address:
            row.address = result.address
            changed = True
    return changed
