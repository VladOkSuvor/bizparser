"""Тесты на bizparser.categories.resolve() — разворачивание категорий/бандлов для CLI."""
from __future__ import annotations

import pytest

from bizparser.categories import BUNDLES, CATEGORIES, prioritize, resolve


def test_resolve_single_known_category():
    assert resolve(["dentist"]) == {"dentist": CATEGORIES["dentist"]}


def test_resolve_is_case_insensitive_and_trims_whitespace():
    assert resolve([" Dentist "]) == {"dentist": CATEGORIES["dentist"]}


def test_resolve_bundle_expands_to_all_members():
    result = resolve(["medical"])
    assert set(result) == set(BUNDLES["medical"])


def test_resolve_all_returns_every_category():
    assert resolve(["all"]) == dict(CATEGORIES)


def test_resolve_multiple_names_merge_into_one_dict():
    result = resolve(["dentist", "cafe"])
    assert set(result) == {"dentist", "cafe"}


def test_resolve_unknown_category_raises_with_helpful_message():
    with pytest.raises(ValueError, match="Неизвестная категория"):
        resolve(["not_a_real_category"])


def test_every_bundle_member_exists_in_categories():
    """Страховка от опечатки при добавлении нового бандла/категории."""
    for bundle_name, members in BUNDLES.items():
        for member in members:
            assert member in CATEGORIES, f"{bundle_name} ссылается на неизвестную {member!r}"


def test_medical_bundle_is_human_medicine_only():
    medical = set(BUNDLES["medical"])
    assert {"dentist", "clinic", "doctors", "lab", "rehab"} <= medical
    # аптеки и ветеринария — другая экономика, только осознанным выбором
    assert "pharmacy" not in medical
    assert "vet" not in medical


def test_prioritize_puts_medical_first():
    selected = resolve(["cafe", "lab", "hairdresser", "dentist"])
    assert list(prioritize(selected))[:2] == ["dentist", "lab"]
