"""Тесты на отсечку государственных медучреждений и мед-сетей на discovery."""
from __future__ import annotations

import pytest

from bizparser.chains import is_big_chain
from bizparser.publicsector import is_public_facility, is_public_name


@pytest.mark.parametrize("tags", [
    {"amenity": "hospital", "name": "КНП «Обласна клінічна лікарня»"},
    {"amenity": "hospital", "name": "Центральна лікарня"},  # больница без признаков частной
    {"amenity": "clinic", "name": "Поліклініка №5"},
    {"amenity": "doctors", "name": "Амбулаторія загальної практики сімейної медицини №2"},
    {"amenity": "clinic", "name": "Центр первинної медико-санітарної допомоги"},
    {"amenity": "dentist", "name": "Міська стоматологічна поліклініка"},
    {"amenity": "clinic", "name": "Клініка", "operator:type": "public"},
    {"amenity": "clinic", "name": "Медцентр", "operator": "Київська міська рада"},
])
def test_public_facilities_are_detected(tags):
    assert is_public_facility(tags)


@pytest.mark.parametrize("tags", [
    {"amenity": "clinic", "name": "Медичний центр «Здоров'я»"},
    {"amenity": "dentist", "name": "Smart Dental"},
    {"amenity": "hospital", "name": "Лікарня", "operator:type": "private"},
    {"amenity": "hospital", "name": "Приватна клініка Оберіг"},
    {"amenity": "clinic", "name": "КПІ-Мед"},  # «КП» внутри слова — не комунальне
])
def test_private_facilities_pass(tags):
    assert not is_public_facility(tags)


def test_non_healthcare_is_never_filtered():
    assert not is_public_facility({"shop": "hairdresser", "name": "КП Перукарня"})


def test_is_public_name_for_sources_without_tags():
    assert is_public_name("КНП «ЦПМСД №3» Полтавської міської ради")
    assert not is_public_name("ТОВ «МЕДІКС»")


@pytest.mark.parametrize("name", ["Сінево", "Synevo пункт забору", "Аптека Доброго Дня", "Бажаємо здоров’я"])
def test_medical_chains_are_cut(name):
    assert is_big_chain({"amenity": "pharmacy", "name": name})


def test_medical_chain_match_is_by_whole_words():
    assert not is_big_chain({"amenity": "clinic", "name": "Ділова клініка"})


def test_medical_brands_do_not_affect_other_verticals():
    assert not is_big_chain({"shop": "beauty", "name": "Оберіг"})
