"""Очередь рассылки: наполнение, claim/result для агента, дневной лимит, CRM."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from bizparser import cli
from bizparser import outreach as ob
from bizparser.models import Base, Business, LeadEvent, Outreach, utcnow
from bizparser.web import app as web


def _biz(i, ig: str | None = "auto", **kw) -> Business:
    data = dict(id=i, osm_id=f"node/{i}", osm_type="node", name=f"Lead {i}", category="dentist",
                city="Львів", source="osm")
    if ig:
        data["socials"] = {"instagram": f"https://instagram.com/lead{i}" if ig == "auto" else ig}
    data.update(kw)
    return Business(**data)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            _biz(1, website_status="not_found", automation={"booking": ["helsi"]},
                 size_estimate="small"),
            _biz(2, website="https://x.ua", category="clinic"),
            _biz(3, ig=None),
            _biz(4, socials={"telegram": "https://t.me/x"}, ig=None),
            _biz(5, crm_status="lost"),
            _biz(6, city="Київ", website_status="not_found"),
        ])
        s.commit()
    return engine


@pytest.fixture
def cap(monkeypatch):
    def set_cap(n: int, ttl: int = 120):
        monkeypatch.setattr(ob, "settings", replace(
            ob.settings, outreach_daily_cap=n, outreach_claim_ttl_min=ttl))
    set_cap(20)
    return set_cap


@pytest.fixture
def client(engine, cap):
    def session():
        with Session(engine) as s:
            yield s
            s.commit()

    web.app.dependency_overrides[web.get_session] = session
    yield TestClient(web.app)
    web.app.dependency_overrides.clear()


def _enqueue(engine, **filters) -> list[int]:
    with Session(engine) as s:
        items = ob.enqueue(s, ob.candidates(s, **filters))
        s.commit()
        return [i.business_id for i in items]


# --- наполнение очереди -----------------------------------------------------


def test_candidates_need_instagram_and_skip_closed_leads(engine):
    with Session(engine) as s:
        assert [b.id for b in ob.candidates(s)] == [1, 2, 6]
        assert [b.id for b in ob.candidates(s, no_website=True)] == [1, 6]
        assert [b.id for b in ob.candidates(s, category="clinic")] == [2]
        assert [b.id for b in ob.candidates(s, city="Київ")] == [6]


def test_same_account_on_two_leads_is_queued_once(engine):
    with Session(engine) as s:
        s.add_all([_biz(7, ig="https://www.instagram.com/Lead1/"), _biz(8, ig="@shared"),
                   _biz(9, ig="https://instagram.com/shared"), _biz(10, ig="https://instagram.com/p/x")])
        s.commit()
        # 7 — тот же аккаунт, что у 1; 9 — дубль 8; 10 — не профиль
        assert [b.id for b in ob.candidates(s)] == [1, 2, 6, 8]
        ob.enqueue(s, ob.candidates(s, city="Київ"))
        assert [b.id for b in ob.candidates(s)] == [1, 2, 8]
        ob.enqueue(s, ob.candidates(s))
        assert ob.candidates(s) == []


def test_enqueue_is_once_per_lead(engine):
    assert _enqueue(engine) == [1, 2, 6]
    assert _enqueue(engine) == []
    with Session(engine) as s:
        assert s.scalars(select(Outreach.handle).order_by(Outreach.id)).all() == [
            "lead1", "lead2", "lead6"]


def test_outreach_queue_cli_respects_filters_and_cap(engine, cap, monkeypatch):
    session = Session(engine)
    monkeypatch.setattr(cli, "session_scope", contextmanager(lambda: (yield session)))
    monkeypatch.setattr(cli, "settings", replace(cli.settings, outreach_daily_cap=1))

    res = CliRunner().invoke(cli.app, ["outreach-queue", "--no-website", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert session.scalar(select(Outreach.id)) is None

    res = CliRunner().invoke(cli.app, ["outreach-queue", "--no-website"])
    assert res.exit_code == 0, res.output
    assert session.scalars(select(Outreach.business_id)).all() == [1]  # лимит 1 за прогон
    session.close()


# --- claim ------------------------------------------------------------------


def test_claim_gives_lead_context_and_marks_claimed(engine, client):
    _enqueue(engine)
    r = client.post("/api/outreach/claim", params={"limit": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["business_id"] for i in body["items"]] == [1, 2]
    first = body["items"][0]
    assert first["status"] == "claimed" and first["attempts"] == 1
    assert first["url"] == "https://instagram.com/lead1"
    assert first["lead"] == {
        "id": 1, "name": "Lead 1", "category": "dentist", "city": "Львів",
        "has_website": False, "website": None, "automation": {"booking": ["helsi"]},
        "automation_summary": "booking:helsi", "has_automation": None, "size": "small",
    }
    assert body["items"][1]["lead"]["has_website"] is True
    assert body["remaining_today"] == 18

    # Взятое второй раз не выдаётся
    assert [i["business_id"] for i in client.post("/api/outreach/claim").json()["items"]] == [6]
    assert client.post("/api/outreach/claim").json()["items"] == []


def test_claim_respects_daily_cap(engine, client, cap):
    cap(2)
    _enqueue(engine)
    assert len(client.post("/api/outreach/claim", params={"limit": 10}).json()["items"]) == 2
    body = client.post("/api/outreach/claim").json()
    assert body["items"] == [] and body["remaining_today"] == 0


def test_skipped_does_not_eat_cap_but_old_claims_expire_from_window(engine, cap):
    cap(1)
    _enqueue(engine)
    with Session(engine) as s:
        now = utcnow()
        [first] = ob.claim(s, 5, now=now)
        ob.record_result(s, first, "skipped", "приватний акаунт")
        [second] = ob.claim(s, 5, now=now)  # skipped лимит не съел
        assert second.business_id == 2
        assert ob.claim(s, 5, now=now) == []
        # Через сутки окно освободилось; первым идёт забытый claim — он давно протух
        assert [i.business_id for i in ob.claim(s, 5, now=now + timedelta(hours=25))] == [2]


def test_stale_claim_returns_to_queue(engine, cap):
    cap(20, ttl=30)
    _enqueue(engine, city="Київ")
    with Session(engine) as s:
        now = utcnow()
        [item] = ob.claim(s, 1, now=now)
        assert ob.claim(s, 1, now=now + timedelta(minutes=10)) == []
        [again] = ob.claim(s, 1, now=now + timedelta(minutes=31))
        assert again.id == item.id and again.attempts == 2


def test_claim_skips_row_taken_by_parallel_agent(engine, cap, monkeypatch):
    """Строку выбрали, но до UPDATE её забрал другой агент — rowcount 0, не выдаём."""
    _enqueue(engine)
    real_scalars = Session.scalars

    def racing_scalars(self, stmt, *a, **kw):
        result = real_scalars(self, stmt, *a, **kw)
        if "outreach.id" in str(stmt) and "FROM outreach" in str(stmt) and not hasattr(self, "_raced"):
            self._raced = True
            ids = list(result)
            with Session(engine) as other:  # параллельный агент
                other.query(Outreach).filter(Outreach.id == ids[0]).update({"status": "claimed",
                                                                          "claimed_at": utcnow()})
                other.commit()
            return iter(ids)
        return result

    monkeypatch.setattr(Session, "scalars", racing_scalars)
    with Session(engine) as s:
        assert [i.business_id for i in ob.claim(s, 2)] == [2, 6]


# --- result -----------------------------------------------------------------


def _claim_one(client) -> int:
    return client.post("/api/outreach/claim", params={"limit": 1}).json()["items"][0]["id"]


def test_sent_then_replied_writes_history_and_moves_crm(engine, client):
    _enqueue(engine)
    oid = _claim_one(client)

    r = client.post(f"/api/outreach/{oid}/result", json={"status": "sent", "text": " Вітаю! "})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent" and r.json()["message"] == "Вітаю!"

    r = client.post(f"/api/outreach/{oid}/result", json={"status": "replied", "text": "Цікаво"})
    assert r.json()["reply"] == "Цікаво"

    lead = client.get("/leads/1").json()
    assert lead["crm_status"] == "in_progress"
    assert lead["outreach"]["status"] == "replied"
    assert [(e["old_status"], e["new_status"], e["comment"]) for e in lead["events"]] == [
        ("in_progress", "in_progress", "Instagram @lead1: відповіли\nЦікаво"),
        ("new", "in_progress", "Instagram @lead1: надіслано\nВітаю!"),
    ]


def test_sent_keeps_crm_status_that_is_already_further(engine, client):
    with Session(engine) as s:
        s.get(Business, 1).crm_status = "interested"
        s.commit()
    _enqueue(engine)
    oid = _claim_one(client)
    client.post(f"/api/outreach/{oid}/result", json={"status": "sent"})
    with Session(engine) as s:
        assert s.get(Business, 1).crm_status == "interested"
        assert s.scalar(select(LeadEvent.comment)) == "Instagram @lead1: надіслано"


def test_failed_stores_error(engine, client):
    _enqueue(engine)
    oid = _claim_one(client)
    r = client.post(f"/api/outreach/{oid}/result", json={"status": "failed", "text": "action blocked"})
    assert r.json()["status"] == "failed" and r.json()["error"] == "action blocked"


def test_result_rejects_bad_transitions(engine, client):
    _enqueue(engine)
    with Session(engine) as s:
        queued_id = s.scalar(select(Outreach.id).where(Outreach.business_id == 2))
    oid = _claim_one(client)

    # Ответ на неотправленное и отправка невзятого — перепутанный id
    assert client.post(f"/api/outreach/{oid}/result", json={"status": "replied"}).status_code == 409
    assert client.post(f"/api/outreach/{queued_id}/result", json={"status": "sent"}).status_code == 409
    assert client.post(f"/api/outreach/{oid}/result", json={"status": "queued"}).status_code == 422
    assert client.post("/api/outreach/999/result", json={"status": "sent"}).status_code == 404

    client.post(f"/api/outreach/{oid}/result", json={"status": "sent"})
    assert client.post(f"/api/outreach/{oid}/result", json={"status": "sent"}).status_code == 409


def test_outreach_api_behind_crm_password(client, monkeypatch):
    monkeypatch.setattr(web, "settings", replace(web.settings, crm_password="s3cret"))
    assert client.post("/api/outreach/claim").status_code == 401
    assert client.post("/api/outreach/1/result", json={"status": "sent"}).status_code == 401
    assert client.post("/api/outreach/claim", auth=("agent", "s3cret")).status_code == 200


def test_leads_list_shows_instagram_and_outreach_status(engine, client):
    _enqueue(engine, city="Київ")
    items = {i["id"]: i for i in client.get("/leads").json()["items"]}
    assert items[6]["instagram"] == "https://instagram.com/lead6"
    assert items[6]["outreach_status"] == "queued"
    assert items[1]["outreach_status"] is None
    assert items[3]["instagram"] is None
    assert client.get("/meta").json()["outreach_statuses"]["sent"] == "Надіслано"
