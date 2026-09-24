"""Отсечка крупных сетей на этапе discovery.

Сільпо, Епіцентр и подобные — не адресат холодной рассылки: у них свой ИТ-отдел,
и решение о «поставить чат-бота» принимается не на уровне точки на карте.
Фильтруем по формату торговли (тег `shop`/`amenity`) и резервно — по известным
брендам, которые могут прийти под безобидным тегом (`shop=convenience` и т.п.).
"""

from __future__ import annotations

from . import publicsector

# Форматы, которые по определению не «малый бизнес»
BIG_CHAIN_SHOP_TAGS = {
    "supermarket", "department_store", "mall", "doityourself",
    "hypermarket", "wholesale", "chain_store", "variety_store",
}
BIG_CHAIN_AMENITY_TAGS = {"marketplace"}

# Резерв на случай, если бренд затесался под другим shop=*
BIG_CHAIN_BRANDS = {
    "сільпо", "silpo", "атб", "atb", "ашан", "auchan", "метро", "metro",
    "епіцентр", "epicentr", "новус", "novus", "фора", "варус", "varus",
    "watsons", "eva", "єва", "rozetka", "розетка", "comfy", "комфі",
    "фокстрот", "foxtrot", "мті", "mti",
}


# Мед-сети: лаборатории и аптеки в Украине почти целиком сетевые (пункт забора
# Synevo — не лид), плюс крупные сети клиник с централизованным маркетингом.
# Сравниваются по первым словам названия: «Сінево пункт забору» тоже отсекается.
MEDICAL_CHAIN_BRANDS = {
    # лаборатории
    "synevo", "сінево", "синэво", "синево", "діла", "dila", "дила", "ескулаб", "eskulab",
    "эскулаб", "csd", "csd lab", "invitro", "інвітро", "инвитро", "медлаб", "medlab",
    "астра-діа", "астра діа", "astra-dia", "astra dia",
    # аптеки
    "анц", "аптека низьких цін", "аптека низких цен", "подорожник", "бажаємо здоров'я",
    "аптека доброго дня", "доброго дня", "аптека 9-1-1", "9-1-1", "d.s.", "аптека d.s.",
    "аптека оптових цін", "копійка", "аптека копійка", "znahar", "знахар", "мед-сервіс",
    "аптека мед-сервіс", "tas", "аптека тас", "бам", "аптека бам",
    # сети клиник
    "добробут", "dobrobut", "into-sana", "інто-сана", "инто-сана", "медіком", "medikom",
    "оберіг", "oberig", "ісіда", "isida", "адоніс", "adonis", "медичний центр борис",
    "медікавер", "medicover", "інто сана", "into sana",
    "клініка борис",
}

_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})


def _norm(value: str | None) -> str:
    return (value or "").strip().lower().translate(_APOSTROPHES)


def _starts_with_brand(value: str, brands: set[str]) -> bool:
    """Совпадение по целым словам с начала: «діла лабораторія» да, «ділова» — нет."""
    words = value.replace("«", " ").replace("»", " ").replace('"', " ").split()
    return any(" ".join(words[:n]) in brands for n in range(1, min(len(words), 4) + 1))


def is_big_chain(tags: dict) -> bool:
    """True, если по тегам похоже на супермаркет/гипермаркет/крупную розницу/мед-сеть."""
    if _norm(tags.get("shop")) in BIG_CHAIN_SHOP_TAGS:
        return True
    if _norm(tags.get("amenity")) in BIG_CHAIN_AMENITY_TAGS:
        return True
    brand = _norm(tags.get("brand")) or _norm(tags.get("name"))
    if brand in BIG_CHAIN_BRANDS:
        return True
    # Мед-бренды проверяем только у медицины: «Оберіг» или «Копійка» среди
    # салонов и магазинов — совсем другие, вполне малые бизнесы
    if not publicsector.is_healthcare(tags):
        return False
    return is_medical_chain(tags.get("brand")) or is_medical_chain(tags.get("name"))


def is_medical_chain(name: str | None) -> bool:
    return bool(name) and _starts_with_brand(_norm(name), MEDICAL_CHAIN_BRANDS)
