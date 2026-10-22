"""Очередь холодных сообщений для внешнего агента.

bizparser сам ничего не отправляет: официальный Instagram API не даёт писать
первым, поэтому шлёт внешний агент, малыми порциями. Здесь только:

- `enqueue` — поставить лидов с Instagram в очередь (CLI `outreach-queue`);
- `claim` — атомарно выдать агенту порцию в пределах дневного лимита;
- `record_result` — принять отчёт агента (sent / failed / skipped / replied).

Каждый отчёт дописывается в lead_events, так что в CRM у лида видна вся
переписка рядом со звонками.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from . import crm
from .config import settings
from .extract import normalize_instagram
from .instagram import username
from .models import NO_WEBSITE_STATUSES, Business, LeadEvent, Outreach, utcnow

CHANNEL = "instagram"

STATUSES: dict[str, str] = {
    "queued": "У черзі",
    "claimed": "Взято агентом",
    "sent": "Надіслано",
    "replied": "Відповіли",
    "failed": "Не вдалося",
    "skipped": "Пропущено",
}

# Что агент может сообщить и из какого состояния. replied — только после sent:
# ответ на неотправленное сообщение означает перепутанный id.
TRANSITIONS: dict[str, set[str]] = {
    "sent": {"claimed"},
    "failed": {"claimed"},
    "skipped": {"queued", "claimed"},
    "replied": {"sent"},
}

# По этим лидам решение уже есть — писать им холодное бессмысленно
CLOSED_CRM = ("won", "lost")

# Попытки, которые съедают дневной лимит аккаунта. skipped — агент даже не писал.
COUNTS_TOWARDS_CAP = ("claimed", "sent", "replied", "failed")


class TransitionError(ValueError):
    pass


# --- наполнение очереди -----------------------------------------------------


def candidates(
    session: Session,
    *,
    category: str | None = None,
    city: str | None = None,
    no_website: bool = False,
) -> list[Business]:
    """Лиды с Instagram, которым ещё не писали в этот канал."""
    queued = select(Outreach.business_id).where(Outreach.channel == CHANNEL)
    stmt = select(Business).where(
        Business.socials.is_not(None),
        Business.id.not_in(queued),
        or_(Business.crm_status.is_(None), Business.crm_status.not_in(CLOSED_CRM)),
    )
    if category:
        stmt = stmt.where(Business.category == category)
    if city:
        stmt = stmt.where(Business.city == city)
    if no_website:
        stmt = stmt.where(Business.website_status.in_(NO_WEBSITE_STATUSES))
    # Один аккаунт бывает у нескольких лидов (филиалы, недосхлопнутые дубли) —
    # писать ему второй раз нельзя, даже от имени другого лида
    taken = set(session.scalars(select(Outreach.handle).where(Outreach.channel == CHANNEL)))
    picked = []
    # JSON-ключ фильтруем в Python: так одинаково на SQLite и Postgres
    for biz in session.scalars(stmt.order_by(Business.id)):
        url = normalize_instagram((biz.socials or {}).get(CHANNEL))
        if url and (handle := username(url)) not in taken:
            taken.add(handle)
            picked.append(biz)
    return picked


def enqueue(session: Session, rows: list[Business]) -> list[Outreach]:
    items = [
        Outreach(business_id=b.id, channel=CHANNEL,
                 handle=username(normalize_instagram(b.socials[CHANNEL])))
        for b in rows
    ]
    session.add_all(items)
    session.flush()
    return items


# --- выдача агенту ----------------------------------------------------------


def used_today(session: Session, now: datetime | None = None) -> int:
    """Сколько задач агент взял за скользящие 24 часа."""
    since = (now or utcnow()) - timedelta(hours=24)
    return session.scalar(
        select(func.count()).select_from(Outreach).where(
            Outreach.channel == CHANNEL,
            Outreach.claimed_at >= since,
            Outreach.status.in_(COUNTS_TOWARDS_CAP),
        )
    ) or 0


def remaining_today(session: Session, now: datetime | None = None) -> int:
    return max(0, settings.outreach_daily_cap - used_today(session, now))


def _claimable(now: datetime):
    stale = now - timedelta(minutes=settings.outreach_claim_ttl_min)
    return and_(
        Outreach.channel == CHANNEL,
        or_(
            Outreach.status == "queued",
            # Агент упал, не отчитавшись, — задача снова в очереди
            and_(Outreach.status == "claimed", Outreach.claimed_at < stale),
        ),
    )


def claim(session: Session, limit: int, now: datetime | None = None) -> list[Outreach]:
    """Атомарно помечает до `limit` задач как claimed, не выходя за дневной лимит.

    Каждая строка забирается условным UPDATE … WHERE status='queued': если
    параллельный агент успел первым, rowcount будет 0 и строка достанется ему.
    """
    now = now or utcnow()
    budget = min(limit, remaining_today(session, now))
    if budget <= 0:
        return []

    cond = _claimable(now)
    ids = list(session.scalars(select(Outreach.id).where(cond).order_by(Outreach.id).limit(budget * 2)))
    claimed: list[int] = []
    for item_id in ids:
        if len(claimed) >= budget:
            break
        res = session.execute(
            update(Outreach)
            .where(Outreach.id == item_id, cond)
            .values(status="claimed", claimed_at=now, attempts=Outreach.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount == 1:
            claimed.append(item_id)
    session.flush()
    if not claimed:
        return []
    # UPDATE шёл мимо ORM — перечитываем, а не берём устаревшие объекты из сессии
    return list(session.scalars(
        select(Outreach).where(Outreach.id.in_(claimed)).order_by(Outreach.id)
        .execution_options(populate_existing=True)
    ))


def lead_context(biz: Business) -> dict:
    """Что агенту нужно, чтобы написать по делу, а не шаблоном."""
    return {
        "id": biz.id,
        "name": biz.name,
        "category": biz.category,
        "city": biz.city,
        "has_website": biz.website_status == "has_website",
        "website": biz.website,
        "automation": biz.automation or {},
        "automation_summary": biz.automation_summary,
        "has_automation": biz.has_automation,
        "size": biz.size_estimate,
    }


# --- отчёт агента -----------------------------------------------------------


def record_result(
    session: Session, item: Outreach, status: str, text: str | None = None,
    now: datetime | None = None,
) -> Outreach:
    """sent → text = отправленное сообщение, replied → ответ, failed/skipped → причина."""
    if status not in TRANSITIONS:
        raise TransitionError(f"Невідомий результат: {status}")
    if item.status not in TRANSITIONS[status]:
        raise TransitionError(f"Не можна перейти {item.status} → {status}")
    now = now or utcnow()
    text = (text or "").strip() or None

    if status == "sent":
        item.message, item.sent_at, item.error = text, now, None
    elif status == "replied":
        item.reply, item.replied_at = text, now
    else:
        item.error = text
    item.status = status

    biz = session.get(Business, item.business_id)
    old = biz.crm_status or crm.DEFAULT
    new = old
    # Первое сообщение ушло — лид уже «в роботі», а не нетронутый «Новий»
    if status == "sent" and old == crm.DEFAULT:
        new = "in_progress"
        biz.crm_status, biz.crm_updated_at = new, now
    note = f"Instagram @{item.handle}: {STATUSES[status].lower()}"
    session.add(LeadEvent(
        business_id=biz.id, old_status=old, new_status=new,
        comment=f"{note}\n{text}" if text else note,
    ))
    return item


def serialize(item: Outreach, biz: Business | None = None) -> dict:
    data = {
        "id": item.id,
        "business_id": item.business_id,
        "channel": item.channel,
        "handle": item.handle,
        "url": f"https://instagram.com/{item.handle}",
        "status": item.status,
        "attempts": item.attempts,
        "message": item.message,
        "reply": item.reply,
        "error": item.error,
        "claimed_at": item.claimed_at.isoformat() if item.claimed_at else None,
        "sent_at": item.sent_at.isoformat() if item.sent_at else None,
    }
    if biz is not None:
        data["lead"] = lead_context(biz)
    return data
