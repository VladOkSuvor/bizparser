"""Тесты на внешние источники: сопоставление с базой, НСЗУ, likarni.com, doc.ua."""
from __future__ import annotations

import csv

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from bizparser.cities import CityIndex, load_cities, select_cities
from bizparser.models import Base, Business
from bizparser.sources import doc_ua, likarni, nszu
from bizparser.sources.base import (
    ExternalRecord,
    Matcher,
    apply_records,
    clean_listing_name,
    same_address,
)

UZH = select_cities(load_cities(), only=["Ужгород"])[0]


def _biz(i, **kw) -> Business:
    data = dict(id=i, osm_id=f"node/{i}", osm_type="node", name="Test", category="clinic",
                city="Ужгород", source="osm")
    data.update(kw)
    return Business(**data)


def _rec(**kw) -> ExternalRecord:
    data = dict(source="nszu", ext_id="x1", name="Test", category="clinic", city="Ужгород")
    data.update(kw)
    return ExternalRecord(**data)


# --- base -------------------------------------------------------------------


def test_clean_listing_name_drops_seo_suffixes_and_city():
    assert clean_listing_name("ОН Клінік Ужгород, медичний центр", "Ужгород") == "ОН Клінік"
    assert clean_listing_name("Нова діагностика в Ужгороді", "Ужгород") == "Нова діагностика"
    assert clean_listing_name("Abbe Optic — офтальмологічний центр", "Київ") == "Abbe Optic"


def test_same_address_tolerates_street_type_and_declension():
    assert same_address("вул. Велика Васильківська, 131", "Велика Васильківська вулиця 131")
    assert not same_address("вул. Велика Васильківська, 131", "вул. Велика Васильківська, 13")
    assert not same_address(None, "вул. Шевченка, 1")


def test_match_by_phone_only_within_same_city():
    row = _biz(1, name="Естет", phone="+380662217733")
    matcher = Matcher([row])
    assert matcher.match(_rec(name="Зовсім інша назва", phones=["+380662217733"])) is row
    assert matcher.match(_rec(city="Київ", phones=["+380662217733"])) is None


def test_match_by_name_needs_address_or_coords_when_ambiguous():
    a = _biz(1, name="Prevention", lat=48.62, lon=22.29)
    b = _biz(2, name="Prevention", lat=48.60, lon=22.31)
    matcher = Matcher([a, b])
    assert matcher.match(_rec(name="Prevention", lat=48.6201, lon=22.2901)) is a
    # филиалы-тёзки без координат и адреса не угадываем
    assert matcher.match(_rec(name="Prevention")) is None


def test_match_by_unique_name_without_location():
    row = _biz(1, name="Он клінік")
    assert Matcher([row]).match(_rec(name="ОН Клінік")) is row


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_apply_records_fills_only_empty_fields_and_is_idempotent(session):
    session.add(_biz(1, name="Естет", phone="+380662217733", email="old@estet.ua"))
    session.flush()
    rec = _rec(name="ТОВ «ЕСТЕТ»", phones=["+380662217733", "+380501112233"],
               email="new@estet.ua", contact_person="Сливка Михайло")
    stats = apply_records(session, [rec])
    assert (stats.matched, stats.new) == (1, 0)
    row = session.scalar(select(Business))
    assert row.email == "old@estet.ua"  # собранное раньше не затираем
    assert row.contact_person == "Сливка Михайло"
    assert row.external_ids == {"nszu": "x1"}
    assert "osm,nszu" == row.source
    assert "+380501112233" in row.notes

    again = apply_records(session, [_rec(name="ТОВ «ЕСТЕТ»", phones=["+380662217733"])])
    assert (again.matched, again.new, again.updated) == (1, 0, 0)


def test_apply_records_creates_new_lead_with_source_prefixed_id(session):
    stats = apply_records(session, [_rec(ext_id="abc", name="Кабінет лікаря", phones=["0501234567"])])
    assert stats.new == 1
    row = session.scalar(select(Business))
    assert row.osm_id == "nszu/abc"
    assert row.phone == "+380501234567"


def test_listing_without_contacts_only_marks_existing(session):
    session.add(_biz(1, name="Он клінік"))
    session.flush()
    recs = [
        _rec(source="doc_ua", name="Он клінік", automation={"listing": ["doc.ua"]}, create_if_missing=False),
        _rec(source="doc_ua", ext_id="y", name="Невідома", create_if_missing=False),
    ]
    stats = apply_records(session, recs)
    assert (stats.matched, stats.new, stats.skipped) == (1, 0, 1)
    row = session.scalar(select(Business))
    assert row.automation == {"listing": ["doc.ua"]}
    assert row.has_automation is None  # листинг — слабый сигнал, «горячесть» не меняет


# --- website_status ---------------------------------------------------------


def test_website_status_follows_website_field():
    row = _biz(1)
    assert row.website_status is None
    row.website = "https://estet.ua"
    assert row.website_status == "has_website"
    assert Business(osm_id="n/2", osm_type="node", name="x", category="c", city="c",
                    website="https://x.ua").website_status == "has_website"


# --- НСЗУ -------------------------------------------------------------------

LEGAL_COLS = ["legal_entity_id", "legal_entity_edrpou", "legal_entity_name", "care_type",
              "property_type", "legal_entity_email", "legal_entity_website", "legal_entity_phone",
              "legal_entity_owner_name", "registration_area", "registration_settlement",
              "registration_address", "lat", "lng"]
DIV_COLS = ["legal_entity_id", "division_id", "division_name", "division_type", "division_phone",
            "division_email", "residence_area", "residence_settlement", "residence_addresses",
            "lat", "lng"]


def _write(path, cols, rows):
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "NULL") for c in cols})


def _legal(i, **kw):
    data = dict(legal_entity_id=i, legal_entity_edrpou="43642846",
                legal_entity_name='ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ "ЕСТЕТ"',
                care_type="Спеціалізована", property_type="Приватна (без ФОП)",
                legal_entity_email="info@estet.ua", legal_entity_phone="+380662217733, +380501112233",
                legal_entity_owner_name="Сливка Михайло Михайлович", registration_area="ЗАКАРПАТСЬКА",
                registration_settlement="УЖГОРОД", registration_address="вулиця Собранецька, 118")
    data.update(kw)
    return data


def test_nszu_records_keep_private_in_listed_cities(tmp_path):
    legal = [
        _legal("a"),
        _legal("b", property_type="Комунальна", legal_entity_name="КНП «ЦПМСД №1»"),
        _legal("c", property_type="ФОП", care_type="Первинна",
               legal_entity_name="ФІЗИЧНА ОСОБА-ПІДПРИЄМЕЦЬ ПЕТРЕНКО ОЛЕНА", legal_entity_edrpou="1234567890"),
        _legal("d", registration_settlement="СЕЛО ДАЛЕКЕ"),
        _legal("e", registration_settlement="ПЕРВОМАЙСЬК", registration_area="ЛУГАНСЬКА"),
    ]
    divisions = [
        dict(legal_entity_id="a", division_id="a1", division_type="Амбулаторія",
             division_phone="+380312000000", residence_area="ЗАКАРПАТСЬКА", residence_settlement="УЖГОРОД",
             residence_addresses="ЗАКАРПАТСЬКА область, місто УЖГОРОД, вулиця Собранецька, 118",
             lat="48.62", lng="22.30"),
        dict(legal_entity_id="a", division_id="a2", division_type="ФАП",
             residence_area="ЗАКАРПАТСЬКА", residence_settlement="УЖГОРОД"),
    ]
    paths = {"legal": tmp_path / "l.csv", "divisions": tmp_path / "d.csv"}
    _write(paths["legal"], LEGAL_COLS, legal)
    _write(paths["divisions"], DIV_COLS, divisions)

    stats = nszu.NszuStats()
    recs = {r.ext_id: r for r in nszu.records(paths, CityIndex(load_cities()), stats=stats)}
    assert set(recs) == {"a1", "c"}
    clinic = recs["a1"]
    assert clinic.name == "ТОВ «ЕСТЕТ»"
    assert clinic.city == "Ужгород"
    assert clinic.address == "вулиця Собранецька, 118"
    assert clinic.phones == ["+380312000000", "+380662217733", "+380501112233"]
    assert clinic.contact_person == "Сливка Михайло Михайлович"
    assert clinic.edrpou == "43642846"
    fop = recs["c"]
    assert fop.category == "doctors"
    assert fop.edrpou is None  # у ФОП это личный РНОКПП — не храним
    assert (stats.public, stats.fap, stats.other_city) == (1, 1, 2)


def test_nszu_short_name_handles_nested_quotes():
    name = 'ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ "КЛІНІКА "ВОДОЛІЙ""'
    assert nszu.short_name(name) == "ТОВ «КЛІНІКА ВОДОЛІЙ»"


# --- likarni / doc.ua -------------------------------------------------------

LIKARNI_CARD = """<html><head>
<script type="application/ld+json">
{"@context": "http://www.schema.org", "@type": "MedicalClinic",
 "name": "Естет, клініка естетичної медицини в Ужгороді",
 "email": "esthete.clinic@gmail.com", "telephone": "+380662217733",
 "description": "рядок
 з переводом",
 "location": {"@type": "Place", "address": {"@type": "PostalAddress",
   "addressLocality": "Ужгород", "streetAddress": "вул. Собранецька, 118"}}}
</script></head></html>"""


def test_likarni_parse_card_reads_json_ld():
    rec = likarni.parse_card(LIKARNI_CARD, "/clinic/estet", UZH)
    assert rec.name == "Естет"
    assert rec.phones == ["+380662217733"]
    assert rec.address == "вул. Собранецька, 118"
    assert rec.category == "cosmetology"
    assert rec.ext_id == "estet"


def test_likarni_drops_aggregator_call_center_number():
    recs = [_rec(source="likarni", ext_id=str(i), phones=["+380673283041"]) for i in range(3)]
    recs.append(_rec(source="likarni", ext_id="own", phones=["+380673283041", "+380501112233"]))
    cleaned = likarni.drop_platform_contacts(recs)
    assert cleaned[0].phones == [] and not cleaned[0].create_if_missing
    assert cleaned[3].phones == ["+380501112233"] and cleaned[3].create_if_missing


DOC_UA_CARD = """<html><head>
<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":[
 {"@type":"ListItem","position":"1","item":{"@id":"https://doc.ua/ua/","name":"doc.ua"}},
 {"@type":"ListItem","position":"2","item":{"@id":"x","name":"Он клінік — медичний центр в Ужгороді"}}]}
</script></head><body><span class="address__name">вул. Собранецька, 100</span></body></html>"""


def test_doc_ua_parse_card_uses_breadcrumbs_and_marks_listing():
    rec = doc_ua.parse_card(DOC_UA_CARD, "https://doc.ua/ua/klinika/uzhgorod/on-klinik", UZH)
    assert rec.name == "Он клінік"
    assert rec.address == "вул. Собранецька, 100"
    assert rec.automation == {"listing": ["doc.ua"]}
    assert rec.create_if_missing is False


def test_doc_ua_card_urls_filters_city_and_ukrainian_version():
    xml = (
        "<loc>https://doc.ua/ua/klinika/uzhgorod/a</loc>"
        "<loc>https://doc.ua/klinika/uzhgorod/a</loc>"
        "<loc>https://doc.ua/ua/klinika/kiev/b</loc>"
    )
    assert doc_ua.card_urls(xml, "uzhgorod") == ["https://doc.ua/ua/klinika/uzhgorod/a"]
