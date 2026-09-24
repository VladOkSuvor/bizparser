"""Отсечка государственных и коммунальных медучреждений на этапе discovery.

Областная больница или КНП «ЦПМСД №3» — не адресат холодного звонка: решение там
принимает не собственник, а бюджет и тендер, и продать бота записи через
звонаря не получится. Цель мед-вертикали — частные клиники и кабинеты, где на
другом конце провода владелец или директор, который решает сам.

Аналогично chains.py: сначала явные теги (`operator:type`, `ownership`), потом
характерные слова в названии/операторе. Больница (`amenity=hospital`) по
умолчанию считается государственной — в Украине частные «лікарні» редкость, и
у них почти всегда есть признак: `operator:type=private` или «клініка»/«ТОВ» в названии.
"""

from __future__ import annotations

import re

HEALTHCARE_AMENITIES = {"hospital", "clinic", "doctors", "dentist", "pharmacy"}

PUBLIC_OPERATOR_TYPES = {"public", "government", "state", "municipal", "community"}
PRIVATE_OPERATOR_TYPES = {"private", "private_non_profit", "business", "commercial"}

# Слова в name / official_name / operator. Сокращения — только как отдельные слова:
# «КП» внутри «КПІ-Мед» не должно срабатывать.
PUBLIC_NAME_RE = re.compile(
    r"(?<!\w)(кнп|кп|кз|ку|кнмп|дз|ду|цпмсд|цпмсдп|црл|цмл|цмкл|омкл)(?!\w)"
    r"|комунальн|коммунальн|державн|государствен|міськ\w* рад|районн\w* рад|обласн\w* рад"
    r"|обласн\w* (клінічн\w* )?(лікарн|шпитал|госпітал|дитяч|центр)"
    r"|міськ\w* (клінічн\w* )?(лікарн|поліклінік|дитяч)"
    r"|(дитяч|районн|студентськ|стоматологічн)\w* поліклінік"
    r"|поліклінік\w* №|лікарн\w* №|поликлиник\w* №|больниц\w* №"
    r"|первинн\w* медико-санітарн|медико-санітарн\w* допомог"
    r"|амбулаторі\w* загальн\w* практик"
    r"|центральн\w* районн\w* лікарн|пологов\w* будин|родильн\w* дом"
    r"|військов|госпіталь ветеран|шпиталь|госпиталь"
    r"|міська лікарня|городская больница|областная больница"
    r"|науково-практичн|научно-практическ|інститут|институт|академі|університет|университет",
    re.I,
)

PRIVATE_NAME_RE = re.compile(
    r"приватн|частн|private|(?<!\w)(тов|фоп|пп|ооо|llc|ltd)(?!\w)|клінік|клиник|clinic"
    r"|медичн\w* центр|медицинск\w* центр|medical|med(?!\w)",
    re.I,
)


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def is_healthcare(tags: dict) -> bool:
    return _norm(tags.get("amenity")) in HEALTHCARE_AMENITIES or bool(tags.get("healthcare"))


def is_public_facility(tags: dict) -> bool:
    """True для государственных/коммунальных медучреждений. Не-медицину не трогает."""
    if not is_healthcare(tags):
        return False

    op_type = _norm(tags.get("operator:type")) or _norm(tags.get("ownership"))
    if op_type in PRIVATE_OPERATOR_TYPES:
        return False
    if op_type in PUBLIC_OPERATOR_TYPES:
        return True

    text = " | ".join(
        tags.get(key) or "" for key in ("name", "name:uk", "official_name", "operator")
    )
    if PUBLIC_NAME_RE.search(text):
        return True

    is_hospital = (
        _norm(tags.get("amenity")) == "hospital" or _norm(tags.get("healthcare")) == "hospital"
    )
    if is_hospital:
        return not PRIVATE_NAME_RE.search(text)
    return False


def is_public_name(name: str | None) -> bool:
    """Та же проверка по одному названию — для источников без OSM-тегов (НСЗУ, агрегаторы)."""
    return bool(name and PUBLIC_NAME_RE.search(name))
