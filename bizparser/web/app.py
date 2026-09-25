"""FastAPI-бэкенд CRM. Источник правды — та же база, что у bizparser (DB_URL).

Данные о лидах пишет пайплайн (discover/enrich/find-sites), CRM их только
читает. Сама CRM владеет двумя вещами: crm_status и историей lead_events.
Google Sheets — витрина: POST /sync делает то же, что `bizparser sheets-sync`.
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import crm
from .. import sheets as gsheets
from ..config import settings
from ..db import init_db, session_scope
from ..export import row_dict
from ..models import NO_WEBSITE_STATUSES, Business, LeadEvent, utcnow

STATIC = Path(__file__).parent / "static"

_basic = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    if not settings.crm_password:
        return
    ok = creds is not None and secrets.compare_digest(
        creds.password.encode(), settings.crm_password.encode()
    )
    if not ok:
        raise HTTPException(401, "Потрібен пароль", headers={"WWW-Authenticate": "Basic"})


def get_session() -> Iterator[Session]:
    with session_scope() as session:
        yield session


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()  # досоздаёт crm_status / lead_events в уже собранной базе
    yield


app = FastAPI(title="bizparser CRM", lifespan=_lifespan, dependencies=[Depends(require_auth)])
# Статика без авторизации: в ней только код интерфейса, данные — за паролем в API
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --- схемы ------------------------------------------------------------------

WebsiteFilter = Literal["has", "none", "unknown"]


class StatusChange(BaseModel):
    status: str
    comment: str | None = Field(default=None, max_length=2000)


def _brief(biz: Business) -> dict:
    return {
        "id": biz.id,
        "name": biz.name,
        "phone": biz.phone,
        "email": biz.email,
        "city": biz.city,
        "category": biz.category,
        "website": biz.website,
        "website_status": biz.website_status,
        "crm_status": biz.crm_status or crm.DEFAULT,
        "crm_updated_at": biz.crm_updated_at.isoformat() if biz.crm_updated_at else None,
    }


def _event(ev: LeadEvent) -> dict:
    return {
        "old_status": ev.old_status,
        "new_status": ev.new_status,
        "comment": ev.comment,
        "created_at": ev.created_at.isoformat(),
    }


# --- эндпоинты --------------------------------------------------------------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/meta")
def meta(session: Session = Depends(get_session)) -> dict:
    """Всё, что фронту нужно для фильтров: статусы, города, категории, счётчики."""
    counts = dict(session.execute(
        select(Business.crm_status, func.count()).group_by(Business.crm_status)
    ).all())
    counts[crm.DEFAULT] = counts.pop(None, 0) + counts.get(crm.DEFAULT, 0)
    return {
        "statuses": [
            {"key": k, "label": lbl, "hint": hint, "count": counts.get(k, 0)}
            for k, (lbl, hint) in crm.STATUSES.items()
        ],
        "cities": list(session.scalars(select(Business.city).distinct().order_by(Business.city))),
        "categories": list(
            session.scalars(select(Business.category).distinct().order_by(Business.category))
        ),
        "sheets_enabled": gsheets.ENABLED,
    }


@app.get("/leads")
def list_leads(
    status: str | None = None,
    city: str | None = None,
    category: str | None = None,
    website: WebsiteFilter | None = None,
    q: str | None = Query(default=None, max_length=100),
    with_phone: bool = False,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    stmt = select(Business)
    if status:
        if status not in crm.STATUSES:
            raise HTTPException(422, f"Невідомий статус: {status}")
        cond = Business.crm_status == status
        if status == crm.DEFAULT:
            cond = or_(cond, Business.crm_status.is_(None))
        stmt = stmt.where(cond)
    if city:
        stmt = stmt.where(Business.city == city)
    if category:
        stmt = stmt.where(Business.category == category)
    if website == "has":
        stmt = stmt.where(Business.website_status == "has_website")
    elif website == "none":
        # Та же выборка, что `bizparser list --no-website`
        stmt = stmt.where(Business.website_status.in_(NO_WEBSITE_STATUSES))
    elif website == "unknown":
        stmt = stmt.where(or_(Business.website_status.is_(None),
                              Business.website_status == "ddg_failed"))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Business.name.ilike(like), Business.phone.like(like)))
    if with_phone:
        stmt = stmt.where(Business.phone.is_not(None))

    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    # С телефоном — наверх: без него звонарю с лидом делать нечего
    rows = session.scalars(
        stmt.order_by(Business.phone.is_(None), Business.name).limit(limit).offset(offset)
    )
    return {"total": total, "items": [_brief(b) for b in rows]}


def _get(session: Session, lead_id: int) -> Business:
    biz = session.get(Business, lead_id)
    if biz is None:
        raise HTTPException(404, "Лід не знайдено")
    return biz


@app.get("/leads/{lead_id}")
def get_lead(lead_id: int, session: Session = Depends(get_session)) -> dict:
    biz = _get(session, lead_id)
    events = session.scalars(
        select(LeadEvent).where(LeadEvent.business_id == lead_id)
        .order_by(LeadEvent.created_at.desc(), LeadEvent.id.desc())
    )
    return {
        **row_dict(biz),
        **_brief(biz),
        "automation": biz.automation_summary,
        "events": [_event(e) for e in events],
    }


@app.patch("/leads/{lead_id}/status")
def change_status(
    lead_id: int, body: StatusChange, session: Session = Depends(get_session)
) -> dict:
    """Смена стадии. Тот же статус + комментарий = просто заметка в истории."""
    if body.status not in crm.STATUSES:
        raise HTTPException(422, f"Невідомий статус: {body.status}")
    biz = _get(session, lead_id)
    old = biz.crm_status or crm.DEFAULT
    comment = (body.comment or "").strip() or None
    if old == body.status and not comment:
        return _brief(biz)

    session.add(LeadEvent(
        business_id=biz.id,
        old_status=old,
        new_status=body.status,
        comment=comment,
    ))
    if old != body.status:
        biz.crm_status = body.status
        biz.crm_updated_at = utcnow()
    return _brief(biz)


@app.post("/sync")
def sync() -> dict:
    if not gsheets.ENABLED:
        raise HTTPException(
            503, "Google Sheets не налаштовано: потрібні GOOGLE_SHEETS_ID і файл сервіс-акаунта"
        )
    try:
        pulled, pushed = gsheets.sync()
    except Exception as exc:  # noqa: BLE001 — сеть/права/квота Google, повтор безопасен
        raise HTTPException(502, f"Синхронізація не вдалась: {exc}") from exc
    return {"pulled": pulled, "pushed": pushed}
