from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table
from sqlalchemy import func, or_, select, update

from . import automation as auto
from . import categories as cat
from . import cities as ct
from . import dedupe as dd
from . import emailcheck
from . import export as exporter
from . import google_places as gp
from . import sheets as gsheets
from .config import settings
from .db import init_db, session_scope
from .enrich import SiteContacts, normalize_url, scrape_many
from .extract import normalize_phone
from .geocode import Place
from .models import Business, ScrapeRun, as_utc, utcnow
from .overpass import build_query, fetch, parse_elements
from .sizing import estimate
from .sources import base as src
from .websearch import SearchFailed, find_website

app = typer.Typer(add_completion=False, help="Сбор контактов малого бизнеса из OSM + сайтов.")
console = Console()
log = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Подробные логи")) -> None:
    _setup_logging(verbose)
    init_db()
    _backfill_website_status()


def _backfill_website_status() -> None:
    """Проставляет website_status записям, собранным до появления колонки. Идемпотентно."""
    with session_scope() as session:
        session.execute(
            update(Business)
            .where(Business.website_status.is_(None), Business.website.is_not(None))
            .values(website_status="has_website")
        )
        session.execute(
            update(Business)
            .where(Business.website_status.is_(None), Business.notes.like("%[ddg:none]%"))
            .values(website_status="not_found")
        )


# --- discovery -------------------------------------------------------------


@app.command("categories")
def list_categories() -> None:
    """Показать доступные категории и бандлы."""
    table = Table(title="Категории (OSM-теги)")
    table.add_column("Имя", style="cyan")
    table.add_column("Фильтры", style="dim")
    for name, filters in sorted(cat.CATEGORIES.items()):
        table.add_row(name, " | ".join(filters))
    console.print(table)

    bundles = Table(title="Бандлы")
    bundles.add_column("Имя", style="magenta")
    bundles.add_column("Входит", style="dim")
    for name, subs in cat.BUNDLES.items():
        bundles.add_row(name, ", ".join(subs))
    bundles.add_row("all", "все категории разом")
    console.print(bundles)


def _last_run(kind: str, city: str | None, category: str | None) -> ScrapeRun | None:
    with session_scope() as session:
        stmt = (
            select(ScrapeRun)
            .where(ScrapeRun.kind == kind, ScrapeRun.finished_at.is_not(None))
            .order_by(ScrapeRun.started_at.desc())
            .limit(1)
        )
        if city:
            stmt = stmt.where(ScrapeRun.city == city)
        if category:
            stmt = stmt.where(ScrapeRun.category == category)
        return session.scalar(stmt)


def _record_run(kind: str, city: str | None, category: str | None, **counts) -> None:
    with session_scope() as session:
        session.add(
            ScrapeRun(kind=kind, city=city, category=category, finished_at=utcnow(), **counts)
        )


@app.command()
def discover(
    city: str = typer.Option(..., "--city", "-c", help="Город, например 'Київ'"),
    category: list[str] = typer.Option(
        ..., "--category", "-k", help="Категория или бандл (можно несколько раз)"
    ),
    country: str = typer.Option("Ukraine", "--country", help="Страна для геокодинга"),
    force: bool = typer.Option(False, "--force", help="Гнать заново, даже если недавно уже гоняли"),
    no_dedupe: bool = typer.Option(False, "--no-dedupe", help="Не схлопывать гео-дубли после"),
    include_chains: bool = typer.Option(
        False, "--include-chains", help="Не отсеивать супермаркеты/гипермаркеты и известные сети"
    ),
    include_public: bool = typer.Option(
        False, "--include-public", help="Не отсеивать государственные/коммунальные медучреждения"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Показать запрос и выйти"),
) -> None:
    """Найти бизнесы через Overpass API и сложить в БД."""
    selected = _resolve_categories(category)
    known = ct.CityIndex(ct.load_cities()).lookup(city)
    place = ct.resolve_area(city, known.geocode_query if known else None, country)
    if place is None:
        console.print(f"[red]Не удалось геокодировать {city!r}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Область:[/green] {place.display_name} (area {place.area_id})")

    result = _discover_city(
        city, place, selected, force=force, include_chains=include_chains,
        include_public=include_public, dry_run=dry_run,
    )
    if dry_run:
        return
    console.print(f"[bold green]Итого:[/bold green] новых {result.new}, обновлено {result.updated}")
    if result.failed:
        console.print(f"[red]Overpass не ответил для: {', '.join(result.failed)} — повтори позже[/red]")
    _after_discover(city, result.new, no_dedupe, updated=result.updated)


def _resolve_categories(names: list[str]) -> dict[str, list[str]]:
    try:
        return cat.prioritize(cat.resolve(names))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


class _CityResult:
    def __init__(self) -> None:
        self.new = self.updated = self.skipped = 0
        self.failed: list[str] = []


def _discover_city(
    city: str, place: Place, selected: dict[str, list[str]], *,
    force: bool, include_chains: bool, include_public: bool, dry_run: bool = False,
) -> _CityResult:
    """Одна категория — один запрос Overpass. Прогон пишется в scrape_runs только при успехе:
    по этой отметке discover-all после падения продолжает с того места, где остановился."""
    result = _CityResult()
    for name, filters in selected.items():
        query = build_query(place, filters)
        if dry_run:
            console.print(f"[bold]{name}[/bold]")
            console.print(query, markup=False, highlight=False)  # в запросе есть [out:json]
            continue

        previous = _last_run("discover", city, name)
        if previous and not force:
            age = utcnow() - as_utc(previous.started_at)
            if age < timedelta(days=settings.rerun_after_days):
                console.print(
                    f"  [dim]{name}: уже гонялся {age.days} дн. назад "
                    f"({previous.found_count} мест) — пропускаю, --force чтобы повторить[/dim]"
                )
                result.skipped += 1
                continue

        with console.status(f"Overpass: {city} / {name}…"):
            elements = fetch(query)
        if elements is None:
            console.print(f"  [red]{name}: Overpass не ответил — город не помечен как обработанный[/red]")
            result.failed.append(name)
            continue
        records = list(
            parse_elements(
                elements, category=name, city=city,
                skip_chains=not include_chains,
                skip_public=not include_public and name in cat.HEALTHCARE_CATEGORIES,
            )
        )
        new, upd = _upsert(records)
        result.new += new
        result.updated += upd
        _record_run(
            "discover", city, name,
            found_count=len(records), new_count=new, updated_count=upd,
        )
        console.print(f"  {name}: найдено {len(records)}, новых {new}, обновлено {upd}")
    return result


def _after_discover(city: str | None, new: int, no_dedupe: bool, updated: int = 0) -> None:
    if not no_dedupe and new:
        merged = _apply_dedupe(city=city)
        if merged:
            console.print(f"[green]Схлопнуто гео-дублей: {merged}[/green]")
    # После дедупа: до него один и тот же адрес, пришедший node+way, сам себя
    # раздувает в branch_count (см. предупреждение в sizing.estimate)
    if new or updated:
        _recompute_sizes(city=city)
    # После дедупа: до него один и тот же адрес, пришедший node+way, сам себя
    # раздувает в branch_count (см. предупреждение в sizing.estimate)
    if total_new or total_upd:
        _recompute_sizes(city=city)


@app.command("discover-all")
def discover_all(
    category: list[str] = typer.Option(
        ["medical"], "--category", "-k",
        help="Категории/бандлы (по умолчанию medical; мед-категории всегда идут первыми)",
    ),
    cities_file: Optional[Path] = typer.Option(
        None, "--cities", help="Свой JSON со списком городов (формат как bizparser/data/cities_ua.json)"
    ),
    only: Optional[str] = typer.Option(
        None, "--only", help="Только эти города через запятую (включает и исключённые)"
    ),
    top: Optional[int] = typer.Option(None, "--top", help="Только N крупнейших городов"),
    include_excluded: bool = typer.Option(
        False, "--include-excluded", help="Не пропускать оккупированные/прифронтовые города"
    ),
    with_pharmacy: bool = typer.Option(False, "--with-pharmacy", help="Добавить аптеки"),
    include_chains: bool = typer.Option(False, "--include-chains"),
    include_public: bool = typer.Option(False, "--include-public"),
    force: bool = typer.Option(False, "--force", help="Игнорировать отметки «город уже обработан»"),
    city_delay: Optional[float] = typer.Option(
        None, "--city-delay", help=f"Пауза между городами, с (по умолчанию {settings.city_delay})"
    ),
    max_failures: int = typer.Option(
        3, "--max-failures", help="Остановиться после стольких сбоев Overpass подряд"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Показать план без запросов"),
) -> None:
    """Discovery по всей Украине: город за городом, с паузой и продолжением после сбоя.

    Прогресс — в таблице scrape_runs: пара город+категория, успешно пройденная
    меньше RERUN_AFTER_DAYS дней назад, пропускается. Упал на 15-м городе —
    просто запусти ту же команду ещё раз.
    """
    names = list(category) + (["pharmacy"] if with_pharmacy else [])
    selected = _resolve_categories(names)
    try:
        cities = ct.select_cities(
            ct.load_cities(cities_file),
            only=[c.strip() for c in only.split(",")] if only else None,
            top=top, include_excluded=include_excluded,
        )
    except (ValueError, OSError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    pending = [
        c for c in cities
        if force or any(_is_due(c.name, name) for name in selected)
    ]
    console.print(
        f"Городов: {len(cities)}, из них ещё не пройдено: {len(pending)}. "
        f"Категории: {', '.join(selected)}"
    )
    if dry_run:
        for c in cities:
            mark = "[green]ждёт[/green]" if c in pending else "[dim]готово[/dim]"
            console.print(f"  {c.name} ({c.oblast or '—'}, ~{c.pop} тыс.) {mark}")
        return

    delay = settings.city_delay if city_delay is None else city_delay
    total_new = total_upd = failures_in_row = 0
    failed_cities: list[str] = []
    for i, c in enumerate(pending, 1):
        console.rule(f"[bold]{i}/{len(pending)} {c.name}[/bold]")
        place = ct.resolve_area(c.name, c.geocode_query)
        if place is None:
            console.print(f"[red]Не удалось геокодировать {c.name} — пропускаю[/red]")
            failed_cities.append(c.name)
            continue

        result = _discover_city(
            c.name, place, selected, force=force,
            include_chains=include_chains, include_public=include_public,
        )
        total_new += result.new
        total_upd += result.updated
        if result.new:
            merged = _apply_dedupe(city=c.name)
            if merged:
                console.print(f"  [green]схлопнуто гео-дублей: {merged}[/green]")

        if result.failed:
            failed_cities.append(c.name)
            failures_in_row += 1
            if failures_in_row >= max_failures:
                console.print(
                    f"[red]Overpass не отвечает {failures_in_row} города подряд — останавливаюсь. "
                    f"Запусти команду позже: продолжит с {c.name}.[/red]"
                )
                break
        else:
            failures_in_row = 0

        # Пауза, только если реально ходили в Overpass
        if i < len(pending) and result.skipped < len(selected):
            time.sleep(delay)

    _recompute_sizes()
    console.print(f"[bold green]Итого:[/bold green] новых {total_new}, обновлено {total_upd}")
    if failed_cities:
        console.print(
            f"[yellow]Не до конца пройдены: {', '.join(failed_cities)}. "
            f"Повторный запуск доделает только их.[/yellow]"
        )


def _is_due(city: str, category: str) -> bool:
    previous = _last_run("discover", city, category)
    if previous is None:
        return True
    return utcnow() - as_utc(previous.started_at) >= timedelta(days=settings.rerun_after_days)


def _upsert(records: list[dict]) -> tuple[int, int]:
    """Вставляет новые, у существующих дозаполняет пустые поля (не затирая enrichment)."""
    new = updated = 0
    with session_scope() as session:
        for rec in records:
            rec = dict(rec)
            rec["phone"] = normalize_phone(rec.get("phone"))
            rec["website"] = normalize_url(rec.get("website"))
            existing = session.scalar(select(Business).where(Business.osm_id == rec["osm_id"]))
            if existing is None:
                size, signals = estimate(rec.get("raw_tags"), rec["category"])
                session.add(Business(**rec, size_estimate=size, size_signals=signals))
                new += 1
                continue

            changed = False
            for field in ("name", "address", "lat", "lon", "phone", "website", "email", "raw_tags"):
                value = rec.get(field)
                if value and not getattr(existing, field):
                    setattr(existing, field, value)
                    changed = True
            if rec.get("socials"):
                merged = dict(existing.socials or {})
                for k, v in rec["socials"].items():
                    merged.setdefault(k, v)
                if merged != (existing.socials or {}):
                    existing.socials = merged
                    changed = True
            if changed:
                existing.updated_at = utcnow()
                updated += 1
    return new, updated


def _recompute_sizes(city: str | None = None) -> None:
    """Пересчитывает size_estimate с учётом числа точек с одинаковым названием.

    Скоуп по городу — иначе тёзка в другом городе раздувает branch_count, и
    каждый прогон переписывает всю базу вместо только что тронутых записей.
    """
    with session_scope() as session:
        counts_stmt = select(Business.name, func.count(Business.id)).group_by(Business.name)
        rows_stmt = select(Business)
        if city:
            counts_stmt = counts_stmt.where(Business.city == city)
            rows_stmt = rows_stmt.where(Business.city == city)
        counts = dict(session.execute(counts_stmt).all())
        for biz in session.scalars(rows_stmt):
            size, signals = estimate(biz.raw_tags, biz.category, counts.get(biz.name, 1))
            biz.size_estimate = size
            biz.size_signals = signals


# --- дедуп -----------------------------------------------------------------


def _apply_dedupe(city: str | None = None, radius: float | None = None,
                  threshold: float | None = None, passes: int = 3) -> int:
    """Схлопывает дубли. Несколько проходов — из-за цепочек A≈B≈C."""
    total = 0
    for _ in range(passes):
        with session_scope() as session:
            stmt = select(Business)
            if city:
                stmt = stmt.where(Business.city == city)
            rows = list(session.scalars(stmt))
            pairs = dd.find_duplicates(rows, radius, threshold)
            if not pairs:
                break
            touched: set[int] = set()
            merged_now = 0
            for pair in pairs:
                if pair.keep.id in touched or pair.drop.id in touched:
                    continue  # оставим следующему проходу
                dd.merge_pair(pair)
                session.delete(pair.drop)
                touched.update({pair.keep.id, pair.drop.id})
                merged_now += 1
            total += merged_now
        if not merged_now:
            break
    return total


@app.command()
def dedupe(
    city: Optional[str] = typer.Option(None, "--city", "-c"),
    radius: float = typer.Option(None, "--radius", help="Метры (по умолчанию из настроек)"),
    threshold: float = typer.Option(None, "--threshold", help="Похожесть названий 0..1"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Только показать, что схлопнется"),
) -> None:
    """Схлопнуть дубли: одно место, пришедшее и как node, и как way."""
    if dry_run:
        with session_scope() as session:
            stmt = select(Business)
            if city:
                stmt = stmt.where(Business.city == city)
            pairs = dd.find_duplicates(list(session.scalars(stmt)), radius, threshold)
        if not pairs:
            console.print("[green]Дублей не нашлось.[/green]")
            return
        console.print(f"[yellow]Нашлось пар: {len(pairs)}[/yellow]")
        for pair in pairs:
            console.print(f"  {pair.describe()}")
        console.print("[dim]Это dry-run, ничего не изменено.[/dim]")
        return

    merged = _apply_dedupe(city, radius, threshold)
    console.print(
        f"[green]Схлопнуто: {merged}[/green]" if merged else "[green]Дублей не нашлось.[/green]"
    )


# --- enrichment ------------------------------------------------------------


@app.command()
def enrich(
    limit: int = typer.Option(50, "--limit", "-n", help="Сколько сайтов обойти за прогон"),
    city: Optional[str] = typer.Option(None, "--city", "-c"),
    category: Optional[str] = typer.Option(None, "--category", "-k"),
    older_than: Optional[int] = typer.Option(
        None, "--older-than", help="Дней: перепроверить тех, кого давно не трогали"
    ),
    recheck: bool = typer.Option(False, "--recheck", help="Обходить всех, включая уже обойдённых"),
    concurrency: int = typer.Option(
        None, "--concurrency", "-j", min=1,
        help=f"Параллельных сайтов (по умолчанию {settings.enrich_concurrency})",
    ),
) -> None:
    """Зайти на сайты бизнесов: контакты + что у них уже стоит из автоматизации."""
    with session_scope() as session:
        stmt = select(Business).where(Business.website.is_not(None))
        if older_than is not None:
            cutoff = utcnow() - timedelta(days=older_than)
            stmt = stmt.where(
                or_(Business.last_verified_at.is_(None), Business.last_verified_at < cutoff)
            )
        elif not recheck:
            stmt = stmt.where(Business.last_verified_at.is_(None))
        if city:
            stmt = stmt.where(Business.city == city)
        if category:
            stmt = stmt.where(Business.category == category)
        # Сначала те, у кого вообще нет прямого контакта — от них больше пользы
        stmt = stmt.order_by(Business.phone.is_not(None), Business.id).limit(limit)
        targets = [(b.id, b.website) for b in session.scalars(stmt)]

    if not targets:
        console.print("[yellow]Нечего обогащать под эти фильтры.[/yellow]")
        return

    workers = concurrency if concurrency is not None else settings.enrich_concurrency
    console.print(
        f"Обхожу {len(targets)} сайтов, {workers} параллельно "
        f"(пауза {settings.site_delay}с на каждый хост отдельно)…"
    )

    stats_found = {"contacts": 0, "automation": 0}

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), console=console,
    ) as progress:
        task = progress.add_task("enrich", total=len(targets))

        def persist(biz_id: int, result: SiteContacts) -> None:
            _persist_enrichment(biz_id, result, stats_found)
            progress.advance(task)

        asyncio.run(scrape_many(targets, persist, workers))

    _record_run("enrich", city, category, found_count=len(targets),
                updated_count=stats_found["contacts"])
    console.print(
        f"[green]Готово.[/green] Контакты найдены у {stats_found['contacts']} из {len(targets)}; "
        f"признаки автоматизации — у {stats_found['automation']}."
    )


SITE_DOWN_ERRORS = {"unreachable", "bad_url", "crashed"}


def _persist_enrichment(biz_id: int, result: SiteContacts, counters: dict) -> None:
    with session_scope() as session:
        row = session.get(Business, biz_id)
        if row is None:
            return

        if result.phones or result.emails or result.socials:
            counters["contacts"] += 1
        if result.phones and not row.phone:
            row.phone = result.phones[0]
        if result.emails and not row.email:
            row.email = result.emails[0]
            row.email_valid = None  # старый вердикт больше не про этот адрес
        if result.socials:
            merged = dict(row.socials or {})
            for network, link in result.socials.items():
                merged.setdefault(network, link)
            row.socials = merged

        if result.automation:
            # Глубокая копия: иначе auto.merge дописывает в тот же список, и SQLAlchemy
            # не видит изменения JSON-поля — новые вендоры при --recheck не сохраняются
            merged_auto = {k: list(v) for k, v in (row.automation or {}).items()}
            auto.merge(merged_auto, result.automation)
            row.automation = merged_auto
            if auto.is_automated(merged_auto):
                counters["automation"] += 1
        # False, а не None: сайт мы посмотрели, ничего не нашли — это тоже результат
        row.has_automation = auto.is_automated(row.automation)

        if not result.is_empty and "website_scrape" not in row.source:
            row.source = f"{row.source},website_scrape"

        now = utcnow()
        row.enriched_at = row.enriched_at or now
        row.last_verified_at = now
        if row.status in ("new", "no_contacts", "enriched"):
            row.status = "enriched" if row.has_direct_contact else "no_contacts"
        if result.error:
            row.notes = f"{(row.notes or '')} [site:{result.error}]".strip()
        # Сайт в OSM есть, но не открывается — для клиента это то же «сайта нет»,
        # и повод для разговора: «у вас сайт лежит». robots_disallow — не про это.
        if result.error in SITE_DOWN_ERRORS:
            row.website_status = "site_down"
        elif result.pages_visited:
            row.website_status = "has_website"
        extra = [p for p in result.phones[1:3] if p != row.phone]
        if extra:
            row.notes = f"{(row.notes or '')} доп.тел: {', '.join(extra)}".strip()


@app.command("find-sites")
def find_sites(
    limit: int = typer.Option(10, "--limit", "-n", help="Максимум запросов за прогон"),
    city: Optional[str] = typer.Option(None, "--city", "-c"),
    category: Optional[str] = typer.Option(None, "--category", "-k"),
    with_phone: bool = typer.Option(
        False, "--with-phone",
        help="Искать и у тех, у кого телефон уже есть — чтобы честно заполнить список `list --no-website`",
    ),
    yes: bool = typer.Option(False, "--yes", help="Не спрашивать подтверждение"),
) -> None:
    """FALLBACK: искать сайт через DuckDuckGo для мест без website в OSM.

    Серая зона по ToS DDG и риск бана по IP. Только для узкого списка мест.
    Итог пишется в website_status: not_found (выдача пришла, сайта нет) или
    ddg_failed (запрос не прошёл — такие поищутся снова при следующем прогоне).
    """
    if not yes:
        console.print(
            "[yellow]Это парсинг HTML-выдачи DuckDuckGo: не запрещено технически, "
            "но против их ToS при автоматизации и чревато баном по IP.\n"
            "Использовать точечно, малыми пачками.[/yellow]"
        )
        if not typer.confirm("Продолжить?"):
            raise typer.Abort()

    with session_scope() as session:
        stmt = select(Business).where(
            Business.website.is_(None),
            or_(Business.website_status.is_(None), Business.website_status != "not_found"),
            or_(Business.notes.is_(None), Business.notes.not_like("%ddg:none%")),
        )
        if not with_phone:
            stmt = stmt.where(Business.phone.is_(None))
        if city:
            stmt = stmt.where(Business.city == city)
        if category:
            stmt = stmt.where(Business.category == category)
        targets = list(session.scalars(stmt.order_by(Business.id).limit(limit)))

    if not targets:
        console.print("[yellow]Нет подходящих мест без сайта и телефона.[/yellow]")
        return

    console.print(f"Ищу сайты для {len(targets)} мест (пауза {settings.ddg_delay}с)…")
    hits = failed = 0
    for biz in targets:
        try:
            url, search_failed = find_website(biz.name, biz.city, biz.category), False
        except SearchFailed as exc:
            url, search_failed = None, True
            console.print(f"  [red]✗[/red] {biz.name}: DDG не ответил ({exc})")
        with session_scope() as session:
            row = session.get(Business, biz.id)
            if row is None:
                continue
            if search_failed:
                row.website_status = "ddg_failed"
                failed += 1
            elif url:
                row.website = url
                row.source = f"{row.source},ddg_fallback"
                hits += 1
                console.print(f"  [green]✓[/green] {biz.name} → {url}")
            else:
                row.notes = f"{(row.notes or '')} [ddg:none]".strip()
                row.website_status = "not_found"
                console.print(f"  [dim]— {biz.name}: не найдено[/dim]")
        if failed >= 3 and not hits:
            console.print("[red]DDG отказывает раз за разом — похоже на бан по IP, останавливаюсь.[/red]")
            break
    _record_run("find_sites", city, category, found_count=len(targets), updated_count=hits)
    console.print(f"[green]Найдено сайтов: {hits}/{len(targets)}.[/green] Дальше гоняй `enrich`.")
    if failed:
        console.print(f"[yellow]Сбоев поиска: {failed} — они поищутся снова при следующем запуске.[/yellow]")


@app.command("verify-emails")
def verify_emails(
    limit: int = typer.Option(500, "--limit", "-n"),
    recheck: bool = typer.Option(False, "--recheck", help="Перепроверить и уже проверенные"),
) -> None:
    """Проверить MX-записи доменов у собранных email."""
    if not emailcheck.DNS_AVAILABLE:
        console.print("[red]Нужен dnspython: pip install dnspython[/red]")
        raise typer.Exit(1)

    with session_scope() as session:
        stmt = select(Business).where(Business.email.is_not(None))
        if not recheck:
            stmt = stmt.where(Business.email_valid.is_(None))
        rows = list(session.scalars(stmt.limit(limit)))

    if not rows:
        console.print("[yellow]Нечего проверять.[/yellow]")
        return

    console.print(f"Проверяю домены у {len(rows)} адресов…")
    verdicts = emailcheck.check_many([r.email for r in rows])

    good = sum(bool(verdicts.get(row.email, False)) for row in rows)
    with session_scope() as session:
        session.execute(
            update(Business),
            [{"id": row.id, "email_valid": verdicts.get(row.email, False)} for row in rows],
        )

    bad = len(rows) - good
    console.print(f"[green]Живых доменов: {good}[/green], мёртвых: {bad}")
    if bad:
        console.print("[dim]Отсеять при выгрузке: `export --valid-email-only`[/dim]")


@app.command("google-places")
def google_places_cmd(
    city: Optional[str] = typer.Option(None, "--city", "-c"),
    limit: int = typer.Option(50, "--limit", "-n", help="Максимум лидов за прогон"),
    recheck: bool = typer.Option(False, "--recheck", help="Опросить и уже проверенных"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Показать кандидатов, без вызовов API"),
) -> None:
    """Точечно дозаполнить телефон/адрес через Google Places — платно, под бюджетным потолком."""
    if not gp.ENABLED:
        console.print("[red]Нужен GOOGLE_PLACES_API_KEY в .env — фича выключена.[/red]")
        raise typer.Exit(1)

    with session_scope() as session:
        stmt = select(Business)
        if city:
            stmt = stmt.where(Business.city == city)
        if not recheck:
            stmt = stmt.where(Business.google_checked_at.is_(None))
        rows = list(session.scalars(stmt))
    candidates = rows if recheck else [b for b in rows if gp.needs_lookup(b)]

    if not candidates:
        console.print("[yellow]Под гейт (недостающие данные / горячий лид) никто не попал.[/yellow]")
        return

    left = gp.budget_left()
    if left <= 0:
        console.print(
            f"[red]Месячный лимит Google Places исчерпан "
            f"({settings.google_places_monthly_cap} вызовов). Подними GOOGLE_PLACES_MONTHLY_CAP "
            f"или дождись следующего месяца.[/red]"
        )
        raise typer.Exit(1)

    # До 2 вызовов на лид (поиск id + детали) — режем список бюджетом заранее
    targets = candidates[: min(limit, max(left // 2, 1))]
    console.print(
        f"Кандидатов под гейт: {len(candidates)}, беру {len(targets)} "
        f"(бюджет на месяц: {left} вызовов из {settings.google_places_monthly_cap})."
    )
    if dry_run:
        for b in targets:
            console.print(f"  {b.name} ({b.city}, {b.category})")
        console.print("[dim]Это dry-run, вызовов к API не было.[/dim]")
        return

    filled = 0
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), console=console,
    ) as progress:
        task = progress.add_task("google-places", total=len(targets))
        for biz in targets:
            try:
                result = gp.lookup(biz)
            except Exception as exc:  # noqa: BLE001 — платный батч не должен падать целиком
                # lookup() уже страхует парсинг JSON, но это деньги: не наша вина, если
                # httpx/сеть кинут что-то совсем неожиданное — один лид не должен стоить
                # прогресса по всем остальным. Аналогично enrich.py: один битый сайт/лид
                # не роняет прогон.
                log.warning("google-places для %s (id=%d) упал: %s", biz.name, biz.id, exc)
                console.print(f"[red]{biz.name}: сбой запроса ({exc}) — пропускаю[/red]")
                progress.advance(task)
                continue
            if gp.apply_result(result):
                filled += 1
            progress.advance(task)

    console.print(f"[green]Дозаполнено: {filled} из {len(targets)}.[/green]")


@app.command("sheets-sync")
def sheets_sync() -> None:
    """Синхронизировать с Google Sheets: сначала забрать правки status/notes, потом обновить снимок."""
    if not gsheets.ENABLED:
        console.print(
            "[red]Нужны GOOGLE_SHEETS_ID и файл сервис-аккаунта "
            f"({settings.google_service_account_file}) — см. README, раздел Google Sheets.[/red]"
        )
        raise typer.Exit(1)

    try:
        with console.status("Забираю правки из таблицы…"):
            pulled = gsheets.pull()

        with session_scope() as session:
            rows = list(session.scalars(select(Business).order_by(Business.id)))

        with console.status(f"Обновляю таблицу ({len(rows)} записей)…"):
            pushed = gsheets.push(rows)
    except Exception as exc:  # noqa: BLE001 — API Google Sheets, сеть или права могли подвести
        # gspread кидает свои исключения (APIError и т.п.) без обёртки в sheets.py —
        # ловим на верхнем уровне, чтобы вместо голого трейсбека пользователь увидел
        # понятную причину. sheets-sync идемпотентна: безопасно просто повторить прогон.
        console.print(f"[red]Синхронизация не удалась: {exc}[/red]")
        console.print("[dim]Обычно помогает повторный запуск — операция идемпотентна.[/dim]")
        raise typer.Exit(1)

    console.print(
        f"[green]Готово.[/green] Правок из таблицы: {pulled}, в таблице теперь {pushed} строк."
    )


# --- внешние источники (мед) ------------------------------------------------


def _apply_source(
    source: str, records: list[src.ExternalRecord], *, city: str | None = None, create: bool = True
) -> None:
    with session_scope() as session:
        result = src.apply_records(session, records, create=create)
    _record_run(
        f"import_{source}", city, None,
        found_count=len(records), new_count=result.new, updated_count=result.updated,
    )
    console.print(
        f"[green]{source}{' / ' + city if city else ''}:[/green] записей {len(records)} → "
        f"совпало с базой {result.matched}, новых лидов {result.new}"
        + (f", филиалов склеено в один лид {result.merged}" if result.merged else "")
        + (f", без совпадения и без контактов {result.skipped}" if result.skipped else "")
    )
    _after_discover(city, result.new, no_dedupe=False, updated=result.updated)


def _pick_cities(only: Optional[str], attr: str | None = None) -> list[ct.City]:
    try:
        cities = ct.select_cities(
            ct.load_cities(), only=[c.strip() for c in only.split(",")] if only else None
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if attr:
        missing = [c.name for c in cities if not getattr(c, attr)]
        if missing and only:
            console.print(f"[yellow]Нет на сайте / нет slug в cities_ua.json: {', '.join(missing)}[/yellow]")
        cities = [c for c in cities if getattr(c, attr)]
    return cities


@app.command("import-nszu")
def import_nszu(
    refresh: bool = typer.Option(False, "--refresh", help="Перекачать CSV, даже если уже скачаны"),
    all_settlements: bool = typer.Option(
        False, "--all-settlements", help="Брать и сёла/города не из списка (по умолчанию — только список)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Показать, сколько найдётся, без записи"),
) -> None:
    """Частные клиники и ФОП-врачи из открытых данных НСЗУ (data.gov.ua): телефон, email, ЛПР."""
    from .sources import nszu

    try:
        with console.status("Качаю CSV НСЗУ с data.gov.ua…"):
            paths = nszu.download(force=refresh)
    except (RuntimeError, ValueError, KeyError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    skipped = nszu.NszuStats()
    records = list(nszu.records(paths, ct.CityIndex(ct.load_cities()),
                                all_settlements=all_settlements, stats=skipped))
    console.print(
        f"Частных точек: {len(records)}. Отсеяно: коммунальных/государственных юрлиц {skipped.public}, "
        f"сетей {skipped.chains}, ФАПов {skipped.fap}, вне списка городов {skipped.other_city}."
    )
    if dry_run:
        by_city: dict[str, int] = {}
        for rec in records:
            by_city[rec.city] = by_city.get(rec.city, 0) + 1
        console.print(", ".join(f"{k}={v}" for k, v in sorted(by_city.items(), key=lambda kv: -kv[1])))
        return
    _apply_source("nszu", records)


@app.command("import-likarni")
def import_likarni(
    only: Optional[str] = typer.Option(None, "--only", help="Города через запятую; по умолчанию все из списка"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Максимум карточек на город"),
) -> None:
    """Клиники с likarni.com: листинги города → JSON-LD карточек (телефон, email, адрес)."""
    from .sources import likarni

    for city in _pick_cities(only, "likarni"):
        with console.status(f"likarni.com: {city.name}…"):
            records = likarni.records(city, limit=limit)
        _apply_source("likarni", records, city=city.name)


@app.command("import-docua")
def import_docua(
    only: Optional[str] = typer.Option(None, "--only", help="Города через запятую; по умолчанию все из списка"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Максимум карточек на город"),
    add_new: bool = typer.Option(
        False, "--add-new", help="Заводить и несовпавшие клиники (без телефона — doc.ua их не отдаёт)"
    ),
) -> None:
    """Отметить лиды, которые уже принимают запись через doc.ua (контактов doc.ua не отдаёт)."""
    from .sources import doc_ua

    for city in _pick_cities(only, "doc_ua"):
        try:
            with console.status(f"doc.ua: {city.name}…"):
                records = doc_ua.records(city, limit=limit, add_new=add_new)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        _apply_source("doc_ua", records, city=city.name, create=add_new)


# --- вывод -----------------------------------------------------------------


@app.command("list")
def list_rows(
    city: Optional[str] = typer.Option(None, "--city", "-c"),
    category: Optional[str] = typer.Option(None, "--category", "-k"),
    status: Optional[str] = typer.Option(None, "--status", "-s"),
    with_contact: bool = typer.Option(False, "--with-contact", help="Только с телефоном или почтой"),
    no_automation: bool = typer.Option(
        False, "--no-automation", help="Горячие лиды: сайт проверен, автоматизации нет"
    ),
    has_automation: bool = typer.Option(
        False, "--has-automation", help="Уже есть решение — можно питчить замену"
    ),
    no_website: bool = typer.Option(
        False, "--no-website", help="Апсейл: сайта нет или он лежит — предлагать сайт вместе с ботом"
    ),
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Показать записи из БД."""
    rows = _query(city, category, status, with_contact or no_website, limit,
                  no_automation=no_automation, has_automation=has_automation,
                  no_website=no_website)
    rows.sort(key=lambda b: -b.contact_score)

    ell = {"overflow": "ellipsis", "no_wrap": True}
    table = Table(title=f"Найдено записей: {len(rows)}", box=None, pad_edge=False)
    table.add_column("id", style="dim", justify="right", min_width=3)
    table.add_column("Название", max_width=24, **ell)
    if not category:
        table.add_column("Кат.", max_width=10, **ell)
    table.add_column("Телефон", min_width=14, **ell)
    table.add_column("Email", max_width=22, **ell)
    table.add_column("Соцсети", max_width=18, **ell)
    table.add_column("Автоматизация", max_width=24, **ell)
    if no_website:
        table.add_column("Сайт", max_width=14, **ell)
        table.add_column("ЛПР", max_width=22, **ell)
    for biz in rows:
        cells = [str(biz.id), biz.name]
        if not category:
            cells.append(biz.category)
        if biz.has_automation:
            automation = f"[yellow]{biz.automation_summary}[/yellow]"
        elif biz.last_verified_at:
            automation = "[bold green]нет — горячий[/bold green]"
        else:
            automation = "[dim]не проверяли[/dim]"
        cells += [
            biz.phone or "[dim]—[/dim]",
            biz.email or "[dim]—[/dim]",
            ", ".join((biz.socials or {}).keys()) or "[dim]—[/dim]",
            automation,
        ]
        if no_website:
            cells += [
                WEBSITE_STATUS_LABELS.get(biz.website_status, WEBSITE_STATUS_LABELS[None]),
                biz.contact_person or "[dim]—[/dim]",
            ]
        table.add_row(*cells)
    console.print(table)
    if no_website:
        console.print(
            "[dim]«не искали» — сайт мог и найтись: прогони `find-sites --with-phone`, "
            "чтобы не предлагать сайт тому, у кого он есть.[/dim]"
        )
    console.print("[dim]Полные данные — в CSV: `export -o leads.csv`[/dim]")


WEBSITE_STATUS_LABELS = {
    "not_found": "[green]не найден[/green]",
    "site_down": "[yellow]не открывается[/yellow]",
    "ddg_failed": "[dim]поиск не прошёл[/dim]",
    None: "[dim]не искали[/dim]",
}
# Всё, кроме has_website: сайта нет, не открывается или его ещё не искали
NO_WEBSITE_STATUSES = ("not_found", "site_down", "ddg_failed")


@app.command()
def stats() -> None:
    """Сводка по базе."""
    with session_scope() as session:
        total = session.scalar(select(func.count(Business.id))) or 0
        if not total:
            console.print("[yellow]База пуста. Начни с `discover`.[/yellow]")
            return

        def count_where(*where) -> int:
            return session.scalar(select(func.count(Business.id)).where(*where)) or 0

        with_phone = count_where(Business.phone.is_not(None))
        with_email = count_where(Business.email.is_not(None))
        with_site = count_where(Business.website.is_not(None))
        checked = count_where(Business.last_verified_at.is_not(None))
        automated = count_where(Business.has_automation.is_(True))
        hot = count_where(
            Business.has_automation.is_(False),
            Business.last_verified_at.is_not(None),
            or_(Business.phone.is_not(None), Business.email.is_not(None)),
        )
        stale = count_where(Business.last_verified_at < utcnow() - timedelta(days=settings.stale_days))
        site_states = dict(
            session.execute(
                select(Business.website_status, func.count(Business.id))
                .where(or_(Business.phone.is_not(None), Business.email.is_not(None)))
                .group_by(Business.website_status)
            ).all()
        )
        by_source = session.execute(
            select(Business.source, func.count(Business.id)).group_by(Business.source)
        ).all()
        by_city = session.execute(
            select(Business.city, func.count(Business.id))
            .group_by(Business.city).order_by(func.count(Business.id).desc())
        ).all()
        by_cat = session.execute(
            select(Business.category, func.count(Business.id), func.count(Business.phone))
            .group_by(Business.category).order_by(func.count(Business.id).desc())
        ).all()

    console.print(f"[bold]Всего:[/bold] {total}")
    console.print(f"  телефон: {with_phone} ({with_phone / total:.0%})")
    console.print(f"  email:   {with_email} ({with_email / total:.0%})")
    console.print(f"  сайт:    {with_site} ({with_site / total:.0%})")
    shown = ", ".join(f"{c}={n}" for c, n in by_city[:15])
    rest = f" и ещё {len(by_city) - 15}" if len(by_city) > 15 else ""
    console.print(f"  города ({len(by_city)}): {shown}{rest}")
    sources: dict[str, int] = {}
    for combined, n in by_source:
        for name in (combined or "").split(","):
            sources[name] = sources.get(name, 0) + n
    console.print("  источники: " + ", ".join(f"{k}={v}" for k, v in sorted(sources.items())))

    no_site = sum(site_states.get(k, 0) for k in NO_WEBSITE_STATUSES)
    console.print(
        f"\n[bold]Апсейл «сайт»[/bold] (с контактом): сайта нет — {no_site} "
        f"(не найден {site_states.get('not_found', 0)}, не открывается {site_states.get('site_down', 0)}, "
        f"сбой поиска {site_states.get('ddg_failed', 0)}); не искали — {site_states.get(None, 0)}"
    )

    console.print(f"\n[bold]Автоматизация[/bold] (проверено сайтов: {checked})")
    if checked:
        console.print(f"  уже автоматизированы: {automated} ({automated / checked:.0%} от проверенных)")
        console.print(f"  [bold green]горячие лиды[/bold green] (нет автоматизации + есть контакт): {hot}")
    if stale:
        console.print(
            f"  [yellow]протухло[/yellow] (>{settings.stale_days} дн.): {stale} "
            f"— обнови через `enrich --older-than {settings.stale_days}`"
        )
    if gp.ENABLED:
        used = gp.usage_this_month()
        console.print(
            f"  Google Places: {used}/{settings.google_places_monthly_cap} вызовов в этом месяце"
        )

    table = Table(title="По категориям")
    table.add_column("Категория")
    table.add_column("Всего", justify="right")
    table.add_column("С телефоном", justify="right")
    for name, count, phones in by_cat:
        table.add_row(name, str(count), f"{phones} ({phones / count:.0%})")
    console.print(table)


@app.command()
def runs(limit: int = typer.Option(20, "--limit", "-n")) -> None:
    """История прогонов: что искали, когда и сколько нашли."""
    with session_scope() as session:
        rows = list(
            session.scalars(select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(limit))
        )
    if not rows:
        console.print("[yellow]Прогонов ещё не было.[/yellow]")
        return

    table = Table(title="Прогоны")
    for col in ("Когда", "Что", "Город", "Категория", "Найдено", "Новых", "Обновлено"):
        table.add_column(col)
    for run in rows:
        table.add_row(
            run.started_at.strftime("%Y-%m-%d %H:%M"), run.kind, run.city or "—",
            run.category or "—", str(run.found_count), str(run.new_count), str(run.updated_count),
        )
    console.print(table)


@app.command("export")
def export_cmd(
    out: Path = typer.Option(Path("leads.csv"), "--out", "-o"),
    city: Optional[str] = typer.Option(None, "--city", "-c"),
    category: Optional[str] = typer.Option(None, "--category", "-k"),
    status: Optional[str] = typer.Option(None, "--status", "-s"),
    with_contact: bool = typer.Option(True, "--with-contact/--all"),
    no_automation: bool = typer.Option(False, "--no-automation", help="Только горячие лиды"),
    has_automation: bool = typer.Option(False, "--has-automation", help="Только с конкурентом"),
    no_website: bool = typer.Option(False, "--no-website", help="Только апсейл-сегмент «нет сайта»"),
    valid_email_only: bool = typer.Option(
        False, "--valid-email-only", help="Отсеять адреса с мёртвым доменом"
    ),
    limit: int = typer.Option(10_000, "--limit", "-n"),
) -> None:
    """Выгрузить лиды в CSV или JSON (по расширению файла)."""
    rows = _query(city, category, status, with_contact, limit,
                  no_automation=no_automation, has_automation=has_automation,
                  valid_email_only=valid_email_only, no_website=no_website)
    if not rows:
        console.print("[yellow]Нечего выгружать под эти фильтры.[/yellow]")
        return
    count = exporter.to_json(rows, out) if out.suffix == ".json" else exporter.to_csv(rows, out)
    console.print(f"[green]Записано {count} строк →[/green] {out}")


@app.command("set-status")
def set_status(
    business_id: int = typer.Argument(..., help="id из `list`"),
    status: str = typer.Argument(..., help="new/enriched/contacted/replied/rejected"),
    note: Optional[str] = typer.Option(None, "--note"),
) -> None:
    """Отметить результат работы с лидом."""
    with session_scope() as session:
        row = session.get(Business, business_id)
        if row is None:
            console.print(f"[red]Нет записи с id={business_id}[/red]")
            raise typer.Exit(1)
        row.status = status
        if note:
            row.notes = f"{(row.notes or '')}\n{note}".strip()
        console.print(f"[green]{row.name} → {status}[/green]")


def _query(
    city: str | None, category: str | None, status: str | None,
    with_contact: bool, limit: int, *,
    no_automation: bool = False, has_automation: bool = False,
    valid_email_only: bool = False, no_website: bool = False,
) -> list[Business]:
    with session_scope() as session:
        stmt = select(Business)
        if city:
            stmt = stmt.where(Business.city == city)
        if category:
            stmt = stmt.where(Business.category == category)
        if status:
            stmt = stmt.where(Business.status == status)
        if with_contact:
            stmt = stmt.where(or_(Business.phone.is_not(None), Business.email.is_not(None)))
        if no_automation:
            # То же определение «горячего», что в stats()/google_places.needs_lookup():
            # именно False, а не NULL (непроверенные — не «горячие», а «неизвестные»),
            # и обязательно есть чем связаться — иначе это не лид, а балласт в выгрузке
            stmt = stmt.where(
                Business.has_automation.is_(False),
                Business.last_verified_at.is_not(None),
                or_(Business.phone.is_not(None), Business.email.is_not(None)),
            )
        if has_automation:
            stmt = stmt.where(Business.has_automation.is_(True))
        if valid_email_only:
            stmt = stmt.where(or_(Business.email.is_(None), Business.email_valid.is_(True)))
        if no_website:
            # Включая «не искали»: иначе сегмент пуст, пока не прогнан find-sites
            stmt = stmt.where(
                or_(Business.website_status.is_(None),
                    Business.website_status.in_(NO_WEBSITE_STATUSES))
            )
        return list(session.scalars(stmt.order_by(Business.name).limit(limit)))


if __name__ == "__main__":
    app()
