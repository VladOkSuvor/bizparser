"""google_places.lookup() тратит реальные деньги за вызов — критично, чтобы сбой
разбора ответа не ронял вызывающего (тогда уже оплаченный вызов не попадёт в
_record_calls() и молча выпадет из месячного бюджета) и не валил весь батч."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from bizparser.google_places import lookup, needs_lookup
from bizparser.models import Business


def _biz(**kwargs) -> Business:
    defaults = dict(
        id=1, osm_id="node/1", osm_type="node", name="Test Dental",
        category="dentist", city="Київ", address="вул. Тестова 1",
        source="osm", google_place_id=None,
    )
    defaults.update(kwargs)
    return Business(**defaults)


def _resp(*, json_side_effect=None, json_return=None, text=""):
    resp = MagicMock()
    resp.text = text
    if json_side_effect is not None:
        resp.json.side_effect = json_side_effect
    else:
        resp.json.return_value = json_return
    return resp


@contextmanager
def _fake_client(*args, **kwargs):
    yield MagicMock()


def test_lookup_survives_non_json_search_response():
    """Регрессия: раньше не-JSON на первом (уже оплаченном!) вызове ронял lookup()
    целиком, и этот вызов никогда не попадал в _record_calls()."""
    bad = _resp(json_side_effect=ValueError("not json"), text="<html>502</html>")
    with patch("bizparser.google_places.build_client", _fake_client), \
         patch("bizparser.google_places.request", return_value=bad):
        result = lookup(_biz())
    assert result.called == 1  # вызов реально ушёл и должен быть учтён в бюджете
    assert result.place_id is None


def test_lookup_survives_non_json_details_response_and_keeps_both_calls_counted():
    """Регрессия: search прошёл нормально (потратил 1 вызов), а details вернул мусор —
    раньше это теряло оба потраченных вызова из бюджета, потому что исключение
    вылетало из lookup() до того, как apply_result()/_record_calls() успевали отработать."""
    search_ok = _resp(json_return={"places": [{"id": "place123"}]})
    details_bad = _resp(json_side_effect=ValueError("not json"), text="<html>502</html>")
    with patch("bizparser.google_places.build_client", _fake_client), \
         patch("bizparser.google_places.request", side_effect=[search_ok, details_bad]):
        result = lookup(_biz())
    assert result.called == 2  # оба вызова реально ушли — оба должны попасть в бюджет
    assert result.place_id == "place123"  # это успело сохраниться
    assert result.phone is None  # а это — нет, но без исключения


def test_lookup_survives_missing_id_in_search_result():
    """Если Google когда-нибудь поменяет схему ответа и уберёт 'id' — не падаем."""
    weird = _resp(json_return={"places": [{"displayName": "no id here"}]})
    with patch("bizparser.google_places.build_client", _fake_client), \
         patch("bizparser.google_places.request", return_value=weird):
        result = lookup(_biz())
    assert result.called == 1
    assert result.place_id is None


def test_lookup_survives_request_returning_none():
    with patch("bizparser.google_places.build_client", _fake_client), \
         patch("bizparser.google_places.request", return_value=None):
        result = lookup(_biz())
    assert result.place_id is None


def test_lookup_happy_path_fills_phone_and_address():
    search_ok = _resp(json_return={"places": [{"id": "place123"}]})
    details_ok = _resp(json_return={
        "internationalPhoneNumber": "+380671234567",
        "formattedAddress": "вул. Тестова 1, Київ",
    })
    with patch("bizparser.google_places.build_client", _fake_client), \
         patch("bizparser.google_places.request", side_effect=[search_ok, details_ok]):
        result = lookup(_biz())
    assert result.called == 2
    assert result.phone == "+380671234567"
    assert result.address == "вул. Тестова 1, Київ"


def test_lookup_skips_search_call_when_place_id_already_cached():
    """place_id кэшируется навсегда — повторный Text Search на то же место не нужен."""
    details_ok = _resp(json_return={"internationalPhoneNumber": "+380671234567"})
    with patch("bizparser.google_places.build_client", _fake_client), \
         patch("bizparser.google_places.request", return_value=details_ok) as mock_request:
        result = lookup(_biz(google_place_id="already-cached"))
    assert result.called == 1  # только Details, без Text Search
    assert mock_request.call_count == 1


def test_needs_lookup_false_once_already_checked():
    biz = _biz(google_checked_at="2026-01-01")
    assert needs_lookup(biz) is False


def test_needs_lookup_true_when_missing_phone_after_enrich():
    biz = _biz(last_verified_at="2026-01-01", phone=None)
    assert needs_lookup(biz) is True


def test_needs_lookup_true_for_hot_lead():
    biz = _biz(has_automation=False, phone="+380671234567")
    assert needs_lookup(biz) is True


def test_needs_lookup_false_for_fresh_unenriched_lead():
    biz = _biz(last_verified_at=None, has_automation=None, phone=None, email=None)
    assert needs_lookup(biz) is False
