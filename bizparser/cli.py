from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table
from sqlalchemy import func, or_, select

from . import automation as auto
from . import categories as cat
from . import dedupe as dd
from . import emailcheck
from . import export as exporter
from . import google_places as gp
from . import sheets as gsheets
from .config import settings
from .db import init_db, session_scope
from .enrich import SiteContacts, normalize_url, scrape_many
from .extract import normalize_phone
from .geocode import geocode_city
from .models import Business, ScrapeRun, as_utc, utcnow
from .overpass import build_query, fetch, parse_elements
from .sizing import estimate
from .websearch import find_website

app = typer.Typer(add_completion=False, help="Сбор контактов малого бизнеса из OSM + сайтов.")
console = Console()


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
    dry_run: bool = typer.Option(False, "--dry-run", help="Показать запрос и выйти"),
) -> None:
    """Найти бизнесы через Overpass API и сложить в БД."""
    try:
        selected = cat.resolve(category)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    place = geocode_city(city, country)
    if place is None:
        console.print(f"[red]Не удалось геокодировать {city!r}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Область:[/green] {place.display_name} (area {place.area_id})")

    total_new = total_upd = 0
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
                continue

        with console.status(f"Overpass: {name}…"):
            elements = fetch(query)
        records = list(
            parse_elements(elements, category=name, city=city, skip_chains=not include_chains)
        )
        new, upd = _upsert(records)
        total_new += new
        total_upd += upd
        _record_run(
            "discover", city, name,
            found_count=len(records), new_count=new, updated_count=upd,
        )
        console.print(f"  {name}: найдено {len(records)}, новых {new}, обновлено {upd}")

    if dry_run:
        return

    console.print(f"[bold green]Итого:[/bold green] новых {total_new}, обновлено {total_upd}")
    _recompute_sizes()
    if not no_dedupe and total_new:
        merged = _apply_dedupe(city=city)
        if merged:
            console.print(f"[green]Схлопнуто гео-дублей: {merged}[/green]")


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


def _recompute_sizes() -> None:
    """Пересчитывает size_estimate с учётом числа точек с одинаковым названием."""
    with session_scope() as session:
        counts = dict(
            session.execute(
                select(Business.name, func.count(Business.id)).group_by(Business.name)
            ).all()
        )
        for biz in session.scalars(select(Business)):
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
        None, "--concurrency", "-j", help=f"Параллельных сайтов (по умолчанию {settings.enrich_concurrency})"
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

    workers = concurrency or settings.enrich_concurrency
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
            merged_auto = dict(row.automation or {})
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
        extra = [p for p in result.phones[1:3] if p != row.phone]
        if extra:
            row.notes = f"{(row.notes or '')} доп.тел: {', '.join(extra)}".strip()


@app.command("find-sites")
def find_sites(
    limit: int = typer.Option(10, "--limit", "-n", help="Максимум запросов за прогон"),
    city: Optional[str] = typer.Option(None, "--city", "-c"),
    category: Optional[str] = typer.Option(None, "--category", "-k"),
    yes: bool = typer.Option(False, "--yes", help="Не спрашивать подтверждение"),
) -> None:
    """FALLBACK: искать сайт через DuckDuckGo для мест без website в OSM.

    Серая зона по ToS DDG и риск бана по IP. Только для узкого списка мест.
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
            Business.phone.is_(None),
            or_(Business.notes.is_(None), Business.notes.not_like("%ddg:none%")),
        )
        if city:
            stmt = stmt.where(Business.city == city)
        if category:
            stmt = stmt.where(Business.category == category)
        targets = list(session.scalars(stmt.order_by(Business.id).limit(limit)))

    if not targets:
        console.print("[yellow]Нет подходящих мест без сайта и телефона.[/yellow]")
        return

    console.print(f"Ищу сайты для {len(targets)} мест (пауза {settings.ddg_delay}с)…")
    hits = 0
    for biz in targets:
        url = find_website(biz.name, biz.city, biz.category)
        with session_scope() as session:
            row = session.get(Business, biz.id)
            if row is None:
                continue
            if url:
                row.website = url
                row.source = f"{row.source},ddg_fallback"
                hits += 1
                console.print(f"  [green]✓[/green] {biz.name} → {url}")
            else:
                row.notes = f"{(row.notes or '')} [ddg:none]".strip()
                console.print(f"  [dim]— {biz.name}: не найдено[/dim]")
    _record_run("find_sites", city, category, found_count=len(targets), updated_count=hits)
    console.print(f"[green]Найдено сайтов: {hits}/{len(targets)}.[/green] Дальше гоняй `enrich`.")


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

    good = 0
    with session_scope() as session:
        for row in rows:
            valid = verdicts.get(row.email, False)
            good += bool(valid)
            session.get(Business, row.id).email_valid = valid

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
            result = gp.lookup(biz)
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

    with console.status("Забираю правки из таблицы…"):
        pulled = gsheets.pull()

    with session_scope() as session:
        rows = list(session.scalars(select(Business).order_by(Business.id)))

    with console.status(f"Обновляю таблицу ({len(rows)} записей)…"):
        pushed = gsheets.push(rows)

    console.print(
        f"[green]Готово.[/green] Правок из таблицы: {pulled}, в таблице теперь {pushed} строк."
    )


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
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Показать записи из БД."""
    rows = _query(city, category, status, with_contact, limit,
                  no_automation=no_automation, has_automation=has_automation)
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
        table.add_row(*cells)
    console.print(table)
    console.print("[dim]Полные данные — в CSV: `export -o leads.csv`[/dim]")


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
        by_city = session.execute(
            select(Business.city, func.count(Business.id)).group_by(Business.city)
        ).all()
        by_cat = session.execute(
            select(Business.category, func.count(Business.id), func.count(Business.phone))
            .group_by(Business.category).order_by(func.count(Business.id).desc())
        ).all()

    console.print(f"[bold]Всего:[/bold] {total}")
    console.print(f"  телефон: {with_phone} ({with_phone / total:.0%})")
    console.print(f"  email:   {with_email} ({with_email / total:.0%})")
    console.print(f"  сайт:    {with_site} ({with_site / total:.0%})")
    console.print("  города: " + ", ".join(f"{c}={n}" for c, n in by_city))

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
    valid_email_only: bool = typer.Option(
        False, "--valid-email-only", help="Отсеять адреса с мёртвым доменом"
    ),
    limit: int = typer.Option(10_000, "--limit", "-n"),
) -> None:
    """Выгрузить лиды в CSV или JSON (по расширению файла)."""
    rows = _query(city, category, status, with_contact, limit,
                  no_automation=no_automation, has_automation=has_automation,
                  valid_email_only=valid_email_only)
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
    valid_email_only: bool = False,
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
            # Именно False, а не NULL: непроверенные — не «горячие», а «неизвестные»
            stmt = stmt.where(
                Business.has_automation.is_(False), Business.last_verified_at.is_not(None)
            )
        if has_automation:
            stmt = stmt.where(Business.has_automation.is_(True))
        if valid_email_only:
            stmt = stmt.where(or_(Business.email.is_(None), Business.email_valid.is_(True)))
        return list(session.scalars(stmt.order_by(Business.name).limit(limit)))


if __name__ == "__main__":
    app()
