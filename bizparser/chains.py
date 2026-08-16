"""Отсечка крупных сетей на этапе discovery.

Сільпо, Епіцентр и подобные — не адресат холодной рассылки: у них свой ИТ-отдел,
и решение о «поставить чат-бота» принимается не на уровне точки на карте.
Фильтруем по формату торговли (тег `shop`/`amenity`) и резервно — по известным
брендам, которые могут прийти под безобидным тегом (`shop=convenience` и т.п.).
"""

from __future__ import annotations

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


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def is_big_chain(tags: dict) -> bool:
    """True, если по тегам похоже на супермаркет/гипермаркет/крупную розницу."""
    if _norm(tags.get("shop")) in BIG_CHAIN_SHOP_TAGS:
        return True
    if _norm(tags.get("amenity")) in BIG_CHAIN_AMENITY_TAGS:
        return True
    brand = _norm(tags.get("brand")) or _norm(tags.get("name"))
    return brand in BIG_CHAIN_BRANDS
