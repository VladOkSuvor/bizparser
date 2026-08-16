"""Тесты на bizparser.automation — детектор 'уже автоматизирован'."""
from __future__ import annotations

from bizparser.automation import describe, detect, is_automated, merge


def test_detect_finds_known_booking_widget():
    html = '<script src="https://n123.yclients.com/widget.js"></script>'
    found = detect(html)
    assert found == {"booking": ["yclients"]}


def test_detect_finds_multiple_kinds_on_one_page():
    html = """
    <script src="https://widget.tidiochat.com/x.js"></script>
    <script src="https://cdn-ru.bitrix24.ru/x.js"></script>
    """
    found = detect(html)
    assert found["chat"] == ["tidio"]
    assert found["crm"] == ["bitrix24"]


def test_detect_dedupes_same_vendor_within_one_page():
    html = "yclients.com yclients.com yclients.com"
    found = detect(html)
    assert found["booking"] == ["yclients"]  # не три раза


def test_detect_text_hint_only_when_no_known_booking_vendor():
    html = "<p>Оформіть онлайн-запис на прийом</p>"
    found = detect(html)
    assert found == {"booking_hint": ["custom_or_manual"]}


def test_detect_text_hint_suppressed_when_real_vendor_present():
    html = '<script src="calendly.com"></script><p>онлайн запис</p>'
    found = detect(html)
    assert "booking_hint" not in found
    assert found["booking"] == ["calendly"]


def test_detect_empty_html_returns_empty():
    assert detect("<html><body>Ласкаво просимо</body></html>") == {}


def test_is_automated_true_for_strong_kind():
    assert is_automated({"booking": ["yclients"]}) is True
    assert is_automated({"chat": ["tidio"]}) is True


def test_is_automated_false_for_weak_kind_only():
    """platform/form/messenger — слабые сигналы, не закрывают задачу записи."""
    assert is_automated({"platform": ["wordpress"]}) is False
    assert is_automated({"messenger": ["whatsapp_button"]}) is False


def test_is_automated_false_for_none_or_empty():
    assert is_automated(None) is False
    assert is_automated({}) is False


def test_merge_accumulates_across_pages_without_duplicates():
    target: dict[str, list[str]] = {"booking": ["yclients"]}
    merge(target, {"booking": ["yclients", "calendly"]})
    assert target["booking"] == ["yclients", "calendly"]


def test_merge_adds_new_kind():
    target: dict[str, list[str]] = {}
    merge(target, {"chat": ["tidio"]})
    assert target == {"chat": ["tidio"]}


def test_describe_formats_readable_string():
    assert describe({"booking": ["yclients"], "chat": ["tidio", "crisp"]}) == (
        "booking:yclients, chat:tidio/crisp"
    )


def test_describe_empty_is_dash():
    assert describe(None) == "—"
    assert describe({}) == "—"
