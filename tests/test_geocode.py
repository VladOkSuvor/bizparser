"""geocode_city не должен падать на не-JSON ответе Nominatim (перегруз/502-страница) —
ровно та же ситуация, что overpass.py уже обрабатывает для Overpass."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bizparser.geocode import geocode_city


def _fake_response(*, json_side_effect=None, json_return=None, text=""):
    resp = MagicMock()
    resp.text = text
    if json_side_effect is not None:
        resp.json.side_effect = json_side_effect
    else:
        resp.json.return_value = json_return
    return resp


def test_geocode_city_returns_none_on_non_json_response():
    bad_resp = _fake_response(json_side_effect=ValueError("not json"), text="<html>502</html>")
    with patch("bizparser.geocode.request", return_value=bad_resp):
        assert geocode_city("Київ") is None


def test_geocode_city_returns_none_when_request_fails():
    with patch("bizparser.geocode.request", return_value=None):
        assert geocode_city("Київ") is None


def test_geocode_city_returns_none_on_empty_results():
    empty_resp = _fake_response(json_return=[])
    with patch("bizparser.geocode.request", return_value=empty_resp):
        assert geocode_city("Неизвестный Город Тест") is None


def test_geocode_city_skips_point_only_results_without_boundary():
    # node — точка, не полигон границы; area_id для неё не существует
    node_only = _fake_response(json_return=[{"osm_type": "node", "osm_id": 1}])
    with patch("bizparser.geocode.request", return_value=node_only):
        assert geocode_city("Тест") is None


def test_geocode_city_returns_place_for_relation_boundary():
    payload = [{
        "osm_type": "relation", "osm_id": 421866,
        "display_name": "Київ, Україна",
        "lat": "50.45", "lon": "30.52",
        "boundingbox": ["50.2", "50.6", "30.2", "30.8"],
    }]
    good_resp = _fake_response(json_return=payload)
    with patch("bizparser.geocode.request", return_value=good_resp):
        place = geocode_city("Київ")
    assert place is not None
    assert place.osm_type == "relation"
    assert place.area_id == 3_600_000_000 + 421866
