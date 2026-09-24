"""Тесты на bizparser.cities: список городов для discover-all и приведение названий."""
from __future__ import annotations

import json

import pytest

from bizparser.cities import CityIndex, load_cities, normalize_city, select_cities


@pytest.fixture(scope="module")
def cities():
    return load_cities()


def test_bundled_list_has_all_major_cities_sorted_by_population(cities):
    names = [c.name for c in cities]
    assert names[0] == "Київ"
    for city in ("Львів", "Харків", "Одеса", "Дніпро", "Ужгород", "Луцьк", "Чернівці"):
        assert city in names
    pops = [c.pop for c in cities]
    assert pops == sorted(pops, reverse=True)


def test_occupied_and_frontline_cities_are_excluded_by_default(cities):
    picked = {c.name for c in select_cities(cities)}
    for city in ("Маріуполь", "Донецьк", "Луганськ", "Херсон", "Сімферополь"):
        assert city not in picked
    assert "Львів" in picked


def test_include_excluded_returns_everything(cities):
    assert len(select_cities(cities, include_excluded=True)) == len(cities)


def test_only_picks_by_any_spelling_and_can_enable_excluded_city(cities):
    picked = select_cities(cities, only=["КИЇВ", "Днепр", "Херсон"])
    assert [c.name for c in picked] == ["Київ", "Дніпро", "Херсон"]


def test_only_with_unknown_city_raises(cities):
    with pytest.raises(ValueError, match="Нет в списке"):
        select_cities(cities, only=["Атлантида"])


def test_top_limits_to_largest_non_excluded(cities):
    picked = select_cities(cities, top=3)
    assert [c.name for c in picked] == ["Київ", "Харків", "Одеса"]


def test_normalize_city_handles_caps_prefix_and_apostrophes():
    assert normalize_city("ІВАНО-ФРАНКІВСЬК") == normalize_city("м. Івано-Франківськ")
    assert normalize_city("Кам’янське") == normalize_city("Кам'янське")


def test_city_index_maps_russian_and_old_names(cities):
    index = CityIndex(cities)
    assert index.lookup("Дніпропетровськ").name == "Дніпро"
    assert index.lookup("Червоноград").name == "Шептицький"
    assert index.lookup("Одесса").name == "Одеса"


def test_geocode_query_contains_oblast_to_disambiguate(cities):
    city = CityIndex(cities).lookup("Первомайськ")
    assert city.geocode_query == "Первомайськ, Миколаївська область"
    assert CityIndex(cities).lookup("Київ").geocode_query == "Київ"


def test_custom_cities_file(tmp_path):
    path = tmp_path / "cities.json"
    path.write_text(json.dumps([{"name": "Тест", "pop": 1}]), encoding="utf-8")
    assert [c.name for c in load_cities(path)] == ["Тест"]
