"""ORM-модель. SQLite по умолчанию, но схема совместима с Postgres."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite не хранит смещение и отдаёт naive-datetime, Postgres — aware.

    Всё, что мы пишем, — UTC, поэтому naive-значение из базы можно безопасно
    пометить как UTC. Без этого арифметика с utcnow() падает на
    "can't subtract offset-naive and offset-aware datetimes".
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# Сегмент апсейла «нет сайта»: сайта точно нет или он не открывается. ddg_failed и
# «не искали» (NULL) сюда не входят: про них неизвестно, есть ли сайт, — предлагать
# сайт такому лиду нельзя
NO_WEBSITE_STATUSES = ("not_found", "site_down")


class Base(DeclarativeBase):
    pass


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(primary_key=True)

    # OSM-идентификатор вида "node/123456" — уникален глобально. У записей, которые
    # пришли не из OSM (НСЗУ, likarni.com), тут "<источник>/<их id>", см. sources/
    osm_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    osm_type: Mapped[str] = mapped_column(String(8))
    # osm_id'ы записей, схлопнутых в эту при гео-дедупе
    merged_ids: Mapped[list | None] = mapped_column(JSON)

    name: Mapped[str] = mapped_column(String(255), index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    city: Mapped[str] = mapped_column(String(128), index=True)
    address: Mapped[str | None] = mapped_column(String(512))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)

    phone: Mapped[str | None] = mapped_column(String(64))
    website: Mapped[str | None] = mapped_column(String(512))
    email: Mapped[str | None] = mapped_column(String(255))
    # None = не проверяли, True/False = результат MX-проверки домена
    email_valid: Mapped[bool | None] = mapped_column(Boolean)
    socials: Mapped[dict | None] = mapped_column(JSON)
    # has_website / not_found / ddg_failed / site_down; None = сайта нет, но и не искали.
    # Всё, кроме has_website, — сегмент под апсейл «сделаем сайт заодно с ботом».
    website_status: Mapped[str | None] = mapped_column(String(16), index=True)

    # Из госреестров (НСЗУ): руководитель — это и есть ЛПР, которому звонят
    contact_person: Mapped[str | None] = mapped_column(String(255))
    edrpou: Mapped[str | None] = mapped_column(String(16), index=True)
    # Связь с внешними источниками — {"nszu": "<division_id>", "likarni": "<url>"}
    external_ids: Mapped[dict | None] = mapped_column(JSON)

    # --- главный сигнал для продаж: что у них уже стоит ---
    # {"booking": ["yclients"], "chat": ["tidio"], "platform": ["tilda"]}
    automation: Mapped[dict | None] = mapped_column(JSON)
    # True только для «сильных» видов (запись/чат/колбэк/CRM/колл-трекинг)
    has_automation: Mapped[bool | None] = mapped_column(Boolean, index=True)

    size_estimate: Mapped[str | None] = mapped_column(String(16))  # micro / small / medium
    size_signals: Mapped[dict | None] = mapped_column(JSON)  # чем именно обоснована оценка

    # osm / website_scrape / ddg_fallback — через запятую, если источников было несколько
    source: Mapped[str] = mapped_column(String(64), default="osm")
    status: Mapped[str] = mapped_column(String(24), default="new", index=True)
    # new -> enriched -> contacted -> replied / rejected / no_contacts
    notes: Mapped[str | None] = mapped_column(Text)
    # Стадия воронки из CRM (bizparser/web, список — crm.STATUSES). Отдельно от status:
    # тот ведёт пайплайн сбора, этот — звонари руками. None = «Новий», ещё не набирали.
    crm_status: Mapped[str | None] = mapped_column(String(16), index=True)
    crm_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw_tags: Mapped[dict | None] = mapped_column(JSON)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # когда последний раз реально стучались на сайт (для recheck --older-than)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # --- Google Places (New), опционально и платно — см. bizparser/google_places.py ---
    # place_id кэшируется навсегда: повторный Text Search на то же место — деньги на ветер
    google_place_id: Mapped[str | None] = mapped_column(String(128), index=True)
    google_places_data: Mapped[dict | None] = mapped_column(JSON)
    google_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_city_category", "city", "category"),
        Index("ix_status_enriched", "status", "enriched_at"),
    )

    @validates("website")
    def _track_website(self, _key: str, value: str | None) -> str | None:
        """Появился сайт — статус меняется сам, из какого бы места кода его ни проставили."""
        if value:
            self.website_status = "has_website"
        return value

    # --- удобные производные ---

    @property
    def has_direct_contact(self) -> bool:
        return bool(self.phone or self.email)

    @property
    def contact_score(self) -> int:
        """Грубый приоритет для обзвона: чем больше каналов, тем выше."""
        score = 0
        score += 3 if self.phone else 0
        score += 3 if self.email else 0
        score += 1 if self.website else 0
        score += min(len(self.socials or {}), 3)
        return score

    @property
    def automation_summary(self) -> str:
        """'booking:yclients, chat:tidio' — то, что видно в таблицах и CSV."""
        if not self.automation:
            return ""
        return ", ".join(
            f"{kind}:{v}" for kind, vendors in sorted(self.automation.items()) for v in vendors
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Business {self.osm_id} {self.name!r} {self.category}>"


class LeadEvent(Base):
    """История CRM: смена статуса или комментарий звонаря. Только дописывается."""

    __tablename__ = "lead_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    old_status: Mapped[str | None] = mapped_column(String(16))
    new_status: Mapped[str | None] = mapped_column(String(16))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Outreach(Base):
    """Очередь холодных сообщений для внешнего агента (Instagram Direct и т.п.).

    Одна строка = один лид в одном канале: второй раз тому же аккаунту первыми
    не пишем. Сам текст и ход событий дописываются ещё и в lead_events — эта
    таблица хранит только текущее состояние. Отправка — вне bizparser.
    """

    __tablename__ = "outreach"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="instagram")
    handle: Mapped[str] = mapped_column(String(64))
    # queued -> claimed -> sent -> replied; claimed -> failed / skipped
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    message: Mapped[str | None] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("business_id", "channel", name="uq_outreach_lead_channel"),)


class ScrapeRun(Base):
    """История прогонов: что искали, когда, сколько нашли."""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # discover / enrich / find_sites
    city: Mapped[str | None] = mapped_column(String(128), index=True)
    category: Mapped[str | None] = mapped_column(String(64), index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    found_count: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_run_target", "kind", "city", "category", "started_at"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ScrapeRun {self.kind} {self.city}/{self.category} {self.found_count}>"


class CityArea(Base):
    """Кэш геокодинга: граница города стабильна, Nominatim на каждый прогон не нужен."""

    __tablename__ = "city_areas"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)  # normalize_city()
    name: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(512))
    osm_type: Mapped[str] = mapped_column(String(16))
    osm_id: Mapped[int] = mapped_column(BigInteger)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    bbox: Mapped[list] = mapped_column(JSON)  # south, north, west, east
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiUsage(Base):
    """Счётчик вызовов платных API по месяцам — бюджетный потолок живёт в базе,
    а не только в квоте Google Cloud Console, чтобы код сам себя останавливал."""

    __tablename__ = "api_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    service: Mapped[str] = mapped_column(String(32), index=True)  # "google_places"
    period: Mapped[str] = mapped_column(String(7), index=True)  # "2026-08"
    calls: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("service", "period", name="uq_api_usage_service_period"),)
