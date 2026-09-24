"""Тесты на bizparser.sizing.estimate().

Три из этих тестов — прямая фиксация багов, найденных и исправленных
16.08.2026: internet_access=no считался как 'есть удобство', website и
contact:website считались как два разных цифровых канала, а capacity/beds
парсились через .isdigit(), падая на нечисловых значениях OSM.
"""
from __future__ import annotations

from bizparser.categories import CATEGORIES
from bizparser.sizing import BASELINE, estimate


def test_every_category_has_an_explicit_baseline():
    """Страховка от опечатки/забытой категории: без этого теста отсутствие в
    BASELINE молча даёт нейтральный дефолт вместо осознанного веса."""
    missing = set(CATEGORIES) - set(BASELINE)
    assert not missing, f"Категории без baseline в sizing.py: {sorted(missing)}"


def test_baseline_no_signals_is_micro():
    cls, signals = estimate({}, "hairdresser")
    assert cls == "micro"
    assert signals["score"] == 0


def test_baseline_unknown_category_defaults_to_one():
    cls, signals = estimate({}, "some_unknown_category")
    assert signals["score"] == 1  # неизвестная категория получает нейтральный baseline
    assert cls == "micro"  # 1 < SMALL_THRESHOLD (2) — сам по себе baseline в small не выводит


def test_internet_access_no_does_not_add_score():
    """Регрессия: internet_access='no' раньше давал +0.5, потому что bool('no') == True."""
    cls_no, signals_no = estimate({"internet_access": "no"}, "cafe")
    cls_plain, signals_plain = estimate({}, "cafe")
    assert signals_no["score"] == signals_plain["score"]
    assert cls_no == cls_plain == "micro"
    assert "amenities" not in signals_no


def test_internet_access_real_value_is_recorded_as_metadata_not_score():
    _cls, signals = estimate({"internet_access": "wlan"}, "cafe")
    assert signals["amenities"] == ["internet_access"]
    assert signals["score"] == 1  # baseline кафе, удобства больше не в score


def test_website_and_contact_website_count_as_one_channel():
    """Регрессия: дублирующий тег того же сайта не должен давать 'два канала'."""
    cls, signals = estimate(
        {"website": "http://x.com", "contact:website": "http://x.com"}, "beauty"
    )
    assert cls == "micro"  # только один реальный канал (сайт), не два
    assert "digital_presence" not in signals


def test_website_and_email_are_two_distinct_channels():
    cls, signals = estimate({"website": "http://x.com", "email": "a@x.com"}, "beauty")
    assert cls == "small"
    assert signals["digital_presence"] == {"web": True, "email": True}


def test_capacity_parses_from_string_with_units():
    """Регрессия: '50 seats'.isdigit() == False — раньше вместимость терялась."""
    _cls, signals = estimate({"capacity": "50 seats"}, "restaurant")
    assert signals["capacity"] == 50


def test_capacity_handles_non_string_tag_value():
    """Регрессия: raw.isdigit() падал с AttributeError, если тег пришёл как int."""
    cls, signals = estimate({"beds": 120}, "hotel")
    assert signals["beds"] == 120
    assert cls == "medium"


def test_branch_count_thresholds():
    _cls, small_bump = estimate({}, "cafe", branch_count=2)
    _cls, big_bump = estimate({}, "cafe", branch_count=4)
    assert small_bump["branches_same_name"] == 2
    assert big_bump["score"] > small_bump["score"]


def test_branch_count_never_goes_below_one():
    """Страховка от мусорного/нулевого branch_count из вызывающего кода."""
    _cls, signals = estimate({}, "cafe", branch_count=0)
    assert "branches_same_name" not in signals


def test_chain_tags_add_score_and_are_recorded():
    _cls, signals = estimate({"brand": "SomeChain"}, "cafe")
    assert signals["chain_tags"] == ["brand"]
    assert signals["score"] == 3  # baseline 1 + chain 2


def test_24_7_hours_add_score():
    _cls, signals = estimate({"opening_hours": "24/7"}, "cafe")
    assert signals["opening_hours"] == "24/7"
    assert signals["score"] == 2


def test_confidence_is_always_low():
    _cls, signals = estimate({"brand": "x", "opening_hours": "24/7"}, "clinic")
    assert signals["confidence"] == "low"


def test_scenario_solo_hairdresser_no_website():
    cls, signals = estimate({}, "hairdresser")
    assert (cls, signals["score"]) == ("micro", 0)


def test_scenario_cafe_with_site_and_email_three_branches():
    cls, signals = estimate(
        {"website": "http://x", "email": "a@x"}, "cafe", branch_count=3
    )
    assert cls == "small"
    assert signals["score"] == 3  # baseline 1 + branches 1 + digital 1


def test_scenario_chain_fitness_247_with_brand():
    cls, signals = estimate(
        {"opening_hours": "24/7", "brand": "FitCurves"}, "fitness", branch_count=5
    )
    assert cls == "medium"
    assert signals["score"] == 7  # baseline 2 + branches 2 + chain 2 + hours 1
