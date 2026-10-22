"""Поиск Instagram через DDG: разбор выдачи, сверка имени/города, команда find-instagram.
Без сети: request() из websearch подменён."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from bizparser import cli
from bizparser.instagram import pick_profile, score_profile, translit
from bizparser.models import Base, Business
from bizparser.websearch import SearchFailed, SearchResult, find_website, search_instagram


def _result(url: str, title: str, snippet: str = "") -> str:
    href = f"//duckduckgo.com/l/?uddg={quote(url, safe='')}&rut=abc"
    return (
        '<div class="result results_links web-result">'
        f'<h2 class="result__title"><a class="result__a" href="{href}">{title}</a></h2>'
        f'<a class="result__snippet" href="{href}">{snippet}</a></div>'
    )


def _serp(*results: str) -> str:
    return f"<html><body>{''.join(results)}</body></html>"


@contextmanager
def _fake_client(*args, **kwargs):
    yield MagicMock()


def _ddg(html: str | None):
    resp = None if html is None else MagicMock(text=html)
    return patch("bizparser.websearch.request", return_value=resp), \
        patch("bizparser.websearch.build_client", _fake_client)


def _run_search(html, fn, *args):
    req, client = _ddg(html)
    with req as mocked, client:
        return fn(*args), mocked


# --- websearch --------------------------------------------------------------


def test_search_instagram_builds_site_query_and_keeps_only_profiles():
    html = _serp(
        _result("https://www.instagram.com/p/Cxyz123/", "Пост"),
        _result("https://www.instagram.com/Smile.Dent.Lviv/?hl=uk",
                "Смайл Дент (@smile.dent.lviv) • Instagram photos and videos", "Стоматологія, Львів"),
        _result("https://instagram.com/smile.dent.lviv/reels/", "Reels"),  # тот же профиль
        _result("https://www.facebook.com/smile", "Facebook"),
    )
    found, mocked = _run_search(html, search_instagram, "Смайл Дент", "Львів")

    assert mocked.call_args.kwargs["data"]["q"] == 'site:instagram.com "Смайл Дент" Львів'
    assert [r.url for r in found] == ["https://instagram.com/smile.dent.lviv"]
    assert found[0].snippet == "Стоматологія, Львів"


def test_search_instagram_drops_descriptive_tail_from_phrase():
    _, mocked = _run_search(_serp(), search_instagram, "SILVERDENT – стоматологічний центр", "Київ")
    assert mocked.call_args.kwargs["data"]["q"] == 'site:instagram.com "SILVERDENT" Київ'


@pytest.mark.parametrize("html", [None, "<div class='anomaly-modal'>bots</div>"])
def test_search_instagram_raises_on_failure_or_captcha(html):
    with pytest.raises(SearchFailed):
        _run_search(html, search_instagram, "X", "Київ")


def test_find_website_still_skips_aggregators():
    html = _serp(
        _result("https://www.instagram.com/clinic/", "IG"),
        _result("https://clinic.com.ua/?utm_source=ddg", "Клініка"),
    )
    found, _ = _run_search(html, find_website, "Клініка", "Київ")
    assert found == "https://clinic.com.ua/"


# --- сверка профиля с лидом -------------------------------------------------


def _ig(handle: str, title: str, snippet: str = "") -> SearchResult:
    return SearchResult(url=f"https://instagram.com/{handle}", title=title, snippet=snippet)


def test_translit_matches_typical_handles():
    assert translit("Посмішка") == "posmishka"
    assert translit("Львів") == "lviv"
    assert translit("Київ") == "kyiv"


def test_cyrillic_name_matches_latin_profile_in_same_city():
    r = _ig("posmishka_lviv", "Posmishka Dental (@posmishka_lviv) • Instagram photos and videos")
    match = pick_profile("Стоматологія «Посмішка»", "Львів", [r])
    assert match is not None and match.handle == "posmishka_lviv" and match.city_ok


def test_city_found_in_snippet_with_case_ending():
    r = _ig("dentalux", "Денталюкс (@dentalux) • Instagram", "Найкраща клініка в Ужгороді")
    assert pick_profile("Денталюкс", "Ужгород", [r]) is not None


def test_same_name_other_city_rejected_unless_loose():
    r = _ig("posmishka_kyiv", "Посмішка (@posmishka_kyiv) • Instagram", "Київ, Хрещатик 1")
    assert pick_profile("Посмішка", "Львів", [r]) is None
    assert pick_profile("Посмішка", "Львів", [r], require_city=False) is not None


def test_different_name_rejected_even_in_same_city():
    r = _ig("beauty_lviv", "Beauty Room Lviv (@beauty_lviv) • Instagram", "Львів")
    assert pick_profile("Посмішка", "Львів", [r]) is None


def test_short_name_containment_is_not_a_match():
    # «АБ» ⊂ «abcclinic» — совпадение букв, а не названия
    r = _ig("abcclinic", "ABC Clinic Lviv • Instagram", "Львів")
    assert score_profile("АБ", "Львів", r).score < 0.84


def test_best_city_match_wins():
    other = _ig("posmishka", "Посмішка (@posmishka) • Instagram", "Київ")
    ours = _ig("posmishka.lviv", "Посмішка Львів (@posmishka.lviv) • Instagram", "")
    assert pick_profile("Посмішка", "Львів", [other, ours]).handle == "posmishka.lviv"


# --- команда find-instagram -------------------------------------------------


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    common = dict(osm_type="node", category="dentist", city="Львів", source="osm")
    session.add_all([
        Business(id=1, osm_id="node/1", name="Посмішка", **common),
        Business(id=2, osm_id="node/2", name="Нікому не відома", **common),
        Business(id=3, osm_id="node/3", name="Вже є", socials={"instagram": "https://instagram.com/x"},
                 **common),
    ])
    session.commit()
    monkeypatch.setattr(cli, "session_scope", contextmanager(lambda: (yield session)))
    yield session
    session.close()


def _fake_search(name, city):
    if name == "Посмішка":
        return [_ig("posmishka_lviv", "Posmishka (@posmishka_lviv) • Instagram", "Lviv")]
    return [_ig("random", "Random shop • Instagram", "Одеса")]


def test_find_instagram_dry_run_writes_nothing(db, monkeypatch):
    monkeypatch.setattr(cli, "search_instagram", MagicMock(side_effect=_fake_search))
    res = CliRunner().invoke(cli.app, ["find-instagram", "--limit", "5", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "posmishka_lviv" in res.output
    # Лид, у которого Instagram уже есть, не ищем
    assert [c.args[0] for c in cli.search_instagram.call_args_list] == ["Посмішка", "Нікому не відома"]
    db.expire_all()
    assert db.get(Business, 1).socials is None
    assert db.get(Business, 2).notes is None


def test_find_instagram_saves_match_and_marks_miss(db, monkeypatch):
    monkeypatch.setattr(cli, "search_instagram", _fake_search)
    res = CliRunner().invoke(cli.app, ["find-instagram", "--yes"])
    assert res.exit_code == 0, res.output

    hit, miss = db.get(Business, 1), db.get(Business, 2)
    assert hit.socials == {"instagram": "https://instagram.com/posmishka_lviv"}
    assert hit.source == "osm,ddg_instagram"
    assert "[ig:none]" in miss.notes

    # Повторный прогон не тратит запрос на уже проверенных
    search = MagicMock(side_effect=_fake_search)
    monkeypatch.setattr(cli, "search_instagram", search)
    CliRunner().invoke(cli.app, ["find-instagram", "--yes"])
    search.assert_not_called()


def test_find_instagram_stops_after_repeated_ddg_failures(db, monkeypatch):
    db.add_all(Business(id=i, osm_id=f"node/{i}", osm_type="node", name=f"Лід {i}",
                        category="dentist", city="Львів", source="osm") for i in range(10, 15))
    db.commit()
    search = MagicMock(side_effect=SearchFailed("капча"))
    monkeypatch.setattr(cli, "search_instagram", search)
    res = CliRunner().invoke(cli.app, ["find-instagram", "--yes"])
    assert res.exit_code == 0 and search.call_count == 3
    assert all("[ig:none]" not in (b.notes or "") for b in db.query(Business))
