"""Google Sheets — общий вид на лиды для команды: статус/заметки редактируют
прямо в таблице, всё остальное подтягивается из БД.

Не два независимых направления синхронизации, а один порядок, всегда один и
тот же: `pull()` сначала переносит правки колонок `status`/`notes` из таблицы
в БД («коворкеры владеют» именно этими двумя полями), потом `push()`
перезаписывает всю таблицу свежим снимком из БД. pull всегда перед push —
иначе свежий снимок затрёт то, что кто-то успел вписать между синками.

Бесплатно: в отличие от Google Places, Sheets API не тарифицируется по
вызовам. Нужен только сервис-аккаунт (см. README) — работает из скрипта
headless, без браузерного OAuth-логина.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gspread
from gspread.exceptions import WorksheetNotFound

from .config import settings
from .db import session_scope
from .export import COLUMNS, row_dict
from .models import Business

log = logging.getLogger(__name__)

SHEET_TITLE = "Leads"

ENABLED = bool(settings.google_sheets_id) and Path(settings.google_service_account_file).exists()


def _worksheet() -> gspread.Worksheet:
    client = gspread.service_account(filename=settings.google_service_account_file)
    sh = client.open_by_key(settings.google_sheets_id)
    try:
        return sh.worksheet(SHEET_TITLE)
    except WorksheetNotFound:
        return sh.add_worksheet(title=SHEET_TITLE, rows=1000, cols=len(COLUMNS))


def pull() -> int:
    """Переносит правки status/notes из таблицы в БД. Возвращает число изменённых записей."""
    records = _worksheet().get_all_records()
    updated = 0
    with session_scope() as session:
        for rec in records:
            try:
                biz_id = int(rec.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if not biz_id:
                continue
            row = session.get(Business, biz_id)
            if row is None:
                continue

            new_status = str(rec.get("status") or "").strip()
            if new_status and new_status != row.status:
                row.status = new_status
                updated += 1

            new_notes = str(rec.get("notes") or "").strip()
            if new_notes != (row.notes or "").strip():
                row.notes = new_notes
                updated += 1
    return updated


def push(rows: list[Business]) -> int:
    """Полностью перезаписывает таблицу свежим снимком из БД."""
    ws = _worksheet()
    ws.resize(rows=len(rows) + 1, cols=len(COLUMNS))
    data = [COLUMNS] + [[row_dict(b).get(c, "") for c in COLUMNS] for b in rows]
    ws.clear()
    ws.update(data)
    ws.freeze(rows=1)
    return len(rows)
