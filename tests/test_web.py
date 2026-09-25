"""CRM API: фильтры, смена статуса, история — на in-memory SQLite."""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from bizparser.models import Base, Business
from bizparser.web import app as web


def _biz(i, **kw) -> Business:
    data = dict(id=i, osm_id=f"node/{i}", osm_type="node", name=f"Lead {i}", category="clinic",
                city="Ужгород", source="osm", phone="+380501112233")
    data.update(kw)
    return Business(**data)


@pytest.fixture
def client():
    # StaticPool: TestClient ходит в приложение из другого потока, а у каждого
    # соединения к sqlite:// своя пустая база
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            _biz(1, website_status="not_found"),
            _biz(2, website="https://x.ua", crm_status="interested"),
            _biz(3, website_status="ddg_failed", city="Київ"),
            _biz(4, phone=None, name="Без телефону"),
        ])
        s.commit()

    def session():
        with Session(engine) as s:
            yield s
            s.commit()

    web.app.dependency_overrides[web.get_session] = session
    yield TestClient(web.app)
    web.app.dependency_overrides.clear()


def ids(resp) -> list[int]:
    assert resp.status_code == 200, resp.text
    return sorted(item["id"] for item in resp.json()["items"])


def test_new_status_includes_null(client):
    assert ids(client.get("/leads", params={"status": "new"})) == [1, 3, 4]
    assert ids(client.get("/leads", params={"status": "interested"})) == [2]


def test_website_filter_matches_cli_segment(client):
    assert ids(client.get("/leads", params={"website": "none"})) == [1]
    assert ids(client.get("/leads", params={"website": "has"})) == [2]
    # ddg_failed — «неизвестно», не «сайта нет»
    assert ids(client.get("/leads", params={"website": "unknown"})) == [3, 4]


def test_filters_and_total(client):
    r = client.get("/leads", params={"city": "Київ"})
    assert ids(r) == [3] and r.json()["total"] == 1
    assert ids(client.get("/leads", params={"with_phone": True})) == [1, 2, 3]
    assert ids(client.get("/leads", params={"q": "телефону"})) == [4]


def test_change_status_writes_history(client):
    r = client.patch("/leads/1/status", json={"status": "no_answer", "comment": "  зайнято "})
    assert r.status_code == 200 and r.json()["crm_status"] == "no_answer"

    # Тот же статус + комментарий — просто заметка
    client.patch("/leads/1/status", json={"status": "no_answer", "comment": "передзвонити о 15"})
    # Тот же статус без комментария — ничего не пишем
    client.patch("/leads/1/status", json={"status": "no_answer"})

    events = client.get("/leads/1").json()["events"]
    assert [(e["old_status"], e["new_status"], e["comment"]) for e in events] == [
        ("no_answer", "no_answer", "передзвонити о 15"),
        ("new", "no_answer", "зайнято"),
    ]
    meta = {s["key"]: s["count"] for s in client.get("/meta").json()["statuses"]}
    assert meta["no_answer"] == 1 and meta["new"] == 2


def test_rejects_unknown_status_and_missing_lead(client):
    assert client.patch("/leads/1/status", json={"status": "thinking"}).status_code == 422
    assert client.patch("/leads/999/status", json={"status": "won"}).status_code == 404
    assert client.get("/leads", params={"status": "thinking"}).status_code == 422


def test_password(client, monkeypatch):
    monkeypatch.setattr(web, "settings", replace(web.settings, crm_password="s3cret"))
    assert client.get("/meta").status_code == 401
    assert client.get("/meta", auth=("any", "wrong")).status_code == 401
    assert client.get("/meta", auth=("any", "s3cret")).status_code == 200


def test_sync_disabled_without_sheets(client, monkeypatch):
    monkeypatch.setattr(web.gsheets, "ENABLED", False)
    assert client.post("/sync").status_code == 503
