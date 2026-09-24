"""Настройки. Всё переопределяется через .env или переменные окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, ""))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, ""))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- storage ---
    db_url: str = field(default_factory=lambda: _env("DB_URL", f"sqlite:///{ROOT / 'businesses.db'}"))

    # --- сеть ---
    # Overpass и Nominatim требуют осмысленный User-Agent с контактом.
    # Без него публичные инстансы имеют полное право забанить.
    user_agent: str = field(
        default_factory=lambda: _env(
            "USER_AGENT",
            "bizparser/0.1 (lead research; contact: vladoksuvor@gmail.com)",
        )
    )
    http_timeout: float = field(default_factory=lambda: _env_float("HTTP_TIMEOUT", 30.0))

    overpass_url: str = field(
        default_factory=lambda: _env("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
    )
    nominatim_url: str = field(
        default_factory=lambda: _env("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
    )

    # --- rate limits (секунды между запросами к одному хосту) ---
    # Nominatim usage policy: жёстко не чаще 1 req/sec.
    nominatim_delay: float = field(default_factory=lambda: _env_float("NOMINATIM_DELAY", 1.2))
    # Overpass: тяжёлые запросы, публичный сервер общий на всех.
    overpass_delay: float = field(default_factory=lambda: _env_float("OVERPASS_DELAY", 3.0))
    # Сайты самих бизнесов — разные хосты, можно бодрее, но без фанатизма.
    site_delay: float = field(default_factory=lambda: _env_float("SITE_DELAY", 1.0))
    # DuckDuckGo HTML — серая зона, только медленно и только как fallback.
    ddg_delay: float = field(default_factory=lambda: _env_float("DDG_DELAY", 6.0))
    # Мед-агрегаторы: чужой сервер с листингами, ходим медленно, как люди
    likarni_delay: float = field(default_factory=lambda: _env_float("LIKARNI_DELAY", 3.0))
    doc_ua_delay: float = field(default_factory=lambda: _env_float("DOC_UA_DELAY", 3.0))
    # discover-all: пауза между городами поверх паузы между запросами Overpass,
    # чтобы прогон по всей стране не превращался в пиковую нагрузку на инстанс
    city_delay: float = field(default_factory=lambda: _env_float("CITY_DELAY", 30.0))

    # --- enrichment ---
    max_pages_per_site: int = 4  # главная + до 3 «контактных» страниц
    respect_robots: bool = field(default_factory=lambda: _env("RESPECT_ROBOTS", "1") == "1")
    default_region: str = field(default_factory=lambda: _env("PHONE_REGION", "UA"))
    # Сколько сайтов качаем одновременно. Лимит site_delay при этом остаётся
    # в силе для каждого хоста отдельно, так что чужой сервер не страдает.
    enrich_concurrency: int = field(default_factory=lambda: _env_int("ENRICH_CONCURRENCY", 8))

    # --- свежесть и дедуп ---
    stale_days: int = field(default_factory=lambda: _env_int("STALE_DAYS", 90))
    # Два POI ближе этого расстояния с похожим названием — одно и то же место
    dedupe_radius_m: float = field(default_factory=lambda: _env_float("DEDUPE_RADIUS_M", 30.0))
    dedupe_threshold: float = field(default_factory=lambda: _env_float("DEDUPE_THRESHOLD", 0.84))
    # Через сколько дней discover считает город+категорию «пора обновить»
    rerun_after_days: int = field(default_factory=lambda: _env_int("RERUN_AFTER_DAYS", 21))

    # --- Google Places (New) — опционально, платно, выключено по умолчанию ---
    # Пусто = фича молча выключена: discover/enrich её вообще не касаются.
    google_places_api_key: str = field(default_factory=lambda: _env("GOOGLE_PLACES_API_KEY", ""))
    google_places_url: str = field(
        default_factory=lambda: _env("GOOGLE_PLACES_URL", "https://places.googleapis.com/v1")
    )
    # Жёсткий потолок вызовов в месяц — независимо от квоты в Google Cloud Console.
    google_places_monthly_cap: int = field(
        default_factory=lambda: _env_int("GOOGLE_PLACES_MONTHLY_CAP", 500)
    )
    google_places_delay: float = field(default_factory=lambda: _env_float("GOOGLE_PLACES_DELAY", 0.2))

    # --- Google Sheets — общий вид на лиды для команды, бесплатно ---
    # Пусто = `sheets-sync` выключена. ID берётся из URL таблицы.
    google_sheets_id: str = field(default_factory=lambda: _env("GOOGLE_SHEETS_ID", ""))
    google_service_account_file: str = field(
        default_factory=lambda: _env("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    )


settings = Settings()
