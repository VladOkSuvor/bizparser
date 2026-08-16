"""Тесты на bizparser.dedupe: нормализация имён, дистанция, поиск пар-дублей."""
from __future__ import annotations

import pytest

from bizparser.dedupe import (
    MergePair,
    find_duplicates,
    haversine_m,
    merge_pair,
    name_similarity,
    normalize_name,
)
from bizparser.models import Business


_next_id = iter(range(1, 1_000_000))


def make_business(**kwargs) -> Business:
    """find_duplicates/merge_pair сравнивают Business.id — в реальности это PK из БД.
    Здесь строк никто не коммитит, поэтому id надо проставлять руками (иначе все
    инстансы имеют id=None и сравнение 'None >= None' падает с TypeError).
    """
    defaults = dict(
        id=next(_next_id),
        osm_id=f"node/{next(_next_id)}",
        osm_type="node",
        name="Test",
        category="dentist",
        city="Київ",
        lat=50.45,
        lon=30.52,
        source="osm",
    )
    defaults.update(kwargs)
    return Business(**defaults)


def test_normalize_name_strips_punctuation_and_generic_words():
    assert normalize_name('Тату-студія «Слон»') == "слон"


def test_normalize_name_strips_legal_form_suffixes():
    assert normalize_name("ТОВ Ромашка") == "ромашка"


def test_normalize_name_all_stopwords_falls_back_to_raw_words():
    """Название состоит только из родовых слов — сравниваем как есть, не пустой строкой."""
    result = normalize_name("Салон Студія")
    assert result != ""


def test_normalize_name_yo_and_e_are_equivalent():
    assert normalize_name("Ёлки") == normalize_name("Елки")


def test_haversine_zero_distance_for_same_point():
    assert haversine_m(50.45, 30.52, 50.45, 30.52) == pytest.approx(0.0, abs=1e-6)


def test_haversine_known_distance_kyiv_lviv_roughly_540km():
    # Київ -> Львів, приблизительно 470-540 км по прямой
    dist = haversine_m(50.4501, 30.5234, 49.8397, 24.0297)
    assert 460_000 < dist < 560_000


def test_name_similarity_identical_is_one():
    assert name_similarity("слон", "слон") == 1.0


def test_name_similarity_substring_is_high():
    assert name_similarity("слон", "слон на подолі") == 0.95


def test_name_similarity_empty_strings_is_zero():
    assert name_similarity("", "слон") == 0.0
    assert name_similarity("", "") == 0.0


def test_name_similarity_unrelated_is_low():
    assert name_similarity("слон", "жираф") < 0.5


def test_find_duplicates_merges_close_same_name_same_category():
    a = make_business(osm_id="node/1", name="Слон", lat=50.4501, lon=30.5234)
    b = make_business(osm_id="way/1", name="Слон", lat=50.4502, lon=30.5235)
    pairs = find_duplicates([a, b], radius_m=30, threshold=0.8)
    assert len(pairs) == 1
    assert {pairs[0].keep.osm_id, pairs[0].drop.osm_id} == {"node/1", "way/1"}


def test_find_duplicates_ignores_different_category_same_location():
    """Разные категории в одной точке — обычно разные арендаторы, не дубль."""
    a = make_business(osm_id="node/1", name="Слон", category="dentist", lat=50.45, lon=30.52)
    b = make_business(osm_id="node/2", name="Слон", category="tattoo", lat=50.45, lon=30.52)
    assert find_duplicates([a, b], radius_m=30, threshold=0.8) == []


def test_find_duplicates_ignores_far_apart_same_name():
    a = make_business(osm_id="node/1", name="Слон", lat=50.4501, lon=30.5234)
    b = make_business(osm_id="node/2", name="Слон", lat=49.8397, lon=24.0297)  # Львів
    assert find_duplicates([a, b], radius_m=30, threshold=0.8) == []


def test_find_duplicates_skips_rows_without_coordinates():
    a = make_business(osm_id="node/1", name="Слон", lat=None, lon=None)
    b = make_business(osm_id="node/2", name="Слон", lat=50.45, lon=30.52)
    assert find_duplicates([a, b], radius_m=30, threshold=0.8) == []


def test_merge_pair_fills_empty_fields_from_drop():
    keep = make_business(osm_id="node/1", phone=None, website="http://x.com")
    drop = make_business(osm_id="node/2", phone="+380671234567", website=None)
    pair = MergePair(keep=keep, drop=drop, distance_m=5.0, similarity=1.0)
    merge_pair(pair)
    assert keep.phone == "+380671234567"
    assert keep.website == "http://x.com"  # не затёрлось


def test_merge_pair_does_not_overwrite_existing_keep_values():
    keep = make_business(osm_id="node/1", phone="+380671111111")
    drop = make_business(osm_id="node/2", phone="+380672222222")
    pair = MergePair(keep=keep, drop=drop, distance_m=5.0, similarity=1.0)
    merge_pair(pair)
    assert keep.phone == "+380671111111"


def test_merge_pair_tracks_merged_ids():
    keep = make_business(osm_id="node/1")
    drop = make_business(osm_id="way/2")
    pair = MergePair(keep=keep, drop=drop, distance_m=5.0, similarity=1.0)
    merge_pair(pair)
    assert "way/2" in keep.merged_ids


def test_merge_pair_merges_socials_without_overwriting():
    keep = make_business(osm_id="node/1", socials={"instagram": "https://instagram.com/a"})
    drop = make_business(osm_id="node/2", socials={"instagram": "https://instagram.com/b",
                                                     "facebook": "https://facebook.com/c"})
    pair = MergePair(keep=keep, drop=drop, distance_m=5.0, similarity=1.0)
    merge_pair(pair)
    assert keep.socials["instagram"] == "https://instagram.com/a"  # keep выигрывает
    assert keep.socials["facebook"] == "https://facebook.com/c"  # но новое добавляется
