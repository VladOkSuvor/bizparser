"""Тесты на overpass.py: сборка фильтров и различие «пусто» vs «сбой» для discover-all."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bizparser.overpass import _tag_filter, fetch, parse_elements


def _resp(payload=None, *, bad_json=False):
    resp = MagicMock()
    resp.text = "<html>504</html>"
    if bad_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = payload
    return resp


def test_tag_filter_exact_wildcard_and_regex():
    assert _tag_filter("shop=beauty][beauty=nails") == '["shop"="beauty"]["beauty"="nails"]'
    assert _tag_filter("craft=*") == '["craft"]'
    assert _tag_filter("healthcare:speciality~cosmetology|dermatology") == (
        '["healthcare:speciality"~"cosmetology|dermatology"]'
    )


def test_fetch_returns_empty_list_for_honest_empty_answer():
    with patch("bizparser.overpass.request", return_value=_resp({"elements": []})):
        assert fetch("q") == []


def test_fetch_returns_none_on_failure_so_city_is_not_marked_done():
    with patch("bizparser.overpass.request", return_value=None):
        assert fetch("q") is None
    with patch("bizparser.overpass.request", return_value=_resp(bad_json=True)):
        assert fetch("q") is None


def test_fetch_treats_server_side_timeout_remark_as_failure():
    payload = {"elements": [], "remark": "runtime error: Query timed out in \"query\" at line 3"}
    with patch("bizparser.overpass.request", return_value=_resp(payload)):
        assert fetch("q") is None


def _el(i, **tags):
    return {"type": "node", "id": i, "lat": 48.6, "lon": 22.3, "tags": tags}


def test_parse_elements_skips_public_facilities_unless_asked():
    elements = [
        _el(1, amenity="hospital", name="Обласна клінічна лікарня"),
        _el(2, amenity="clinic", name="Медичний центр Естет"),
    ]
    kept = list(parse_elements(elements, category="clinic", city="Ужгород"))
    assert [r["name"] for r in kept] == ["Медичний центр Естет"]
    everything = list(parse_elements(elements, category="clinic", city="Ужгород", skip_public=False))
    assert len(everything) == 2
