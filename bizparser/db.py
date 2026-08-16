from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

log = logging.getLogger(__name__)

_engine = create_engine(settings.db_url, future=True)
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def _add_missing_columns() -> None:
    """Мини-миграция: дописывает новые nullable-колонки в уже существующие таблицы.

    Полноценная Alembic здесь избыточна, но и терять собранную базу при
    добавлении поля не хочется. Все новые колонки nullable, поэтому
    ALTER TABLE ADD COLUMN проходит и на SQLite.
    """
    inspector = inspect(_engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable:
                log.warning(
                    "Колонка %s.%s не nullable — добавь её вручную или пересоздай базу",
                    table.name, column.name,
                )
                continue
            ddl = (
                f"ALTER TABLE {table.name} "
                f"ADD COLUMN {column.name} {column.type.compile(_engine.dialect)}"
            )
            with _engine.begin() as conn:
                conn.execute(text(ddl))
            log.info("Добавлена колонка %s.%s", table.name, column.name)


def init_db() -> None:
    Base.metadata.create_all(_engine)
    _add_missing_columns()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
