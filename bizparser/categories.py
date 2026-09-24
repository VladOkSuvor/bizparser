"""Пресеты категорий → OSM-теги.

Ключ — короткое имя для CLI. Значение — список тег-фильтров, которые
собираются в один Overpass-запрос через union.
Полный справочник тегов: https://wiki.openstreetmap.org/wiki/Map_features
"""

from __future__ import annotations

CATEGORIES: dict[str, list[str]] = {
    # красота / здоровье — классическая аудитория для сайтов и записи онлайн
    "hairdresser": ['shop=hairdresser'],
    "beauty": ['shop=beauty', 'shop=cosmetics', 'shop=massage'],
    "nails": ['shop=beauty][beauty=nails'],
    "tattoo": ['shop=tattoo'],
    # мед — приоритетная вертикаль (см. MEDICAL_PRIORITY ниже)
    "dentist": ['amenity=dentist', 'healthcare=dentist'],
    "clinic": ['amenity=clinic', 'healthcare=clinic', 'healthcare=centre'],
    # частные кабинеты: гинеколог, педиатр, семейный врач — часто ФОП без сайта
    "doctors": ['amenity=doctors', 'healthcare=doctor'],
    "lab": ['healthcare=laboratory', 'healthcare=sample_collection'],
    "rehab": ['healthcare=rehabilitation', 'healthcare=physiotherapist'],
    # косметология как мед. услуга (дерматолог/косметолог), а не салон красоты
    "cosmetology": ['healthcare:speciality~cosmetology|dermatology|aesthetic'],
    # больницы в OSM — в основном коммунальные; частные отбирает publicsector.py
    "hospital": ['amenity=hospital', 'healthcare=hospital'],
    "vet": ['amenity=veterinary'],
    # аптеки — другая экономика и другой ЛПР, поэтому не в бандле medical, а отдельно
    "pharmacy": ['amenity=pharmacy', 'healthcare=pharmacy'],
    "fitness": ['leisure=fitness_centre', 'leisure=sports_centre'],
    "spa": ['leisure=spa', 'amenity=spa'],
    # услуги
    "real_estate": ['office=estate_agent', 'shop=estate_agent'],
    "lawyer": ['office=lawyer', 'office=notary'],
    "accountant": ['office=accountant', 'office=financial'],
    "travel": ['shop=travel_agency'],
    "insurance": ['office=insurance'],
    "photo": ['shop=photo', 'craft=photographer'],
    "driving_school": ['amenity=driving_school'],
    "language_school": ['amenity=language_school', 'office=educational_institution'],
    "repair_car": ['shop=car_repair', 'shop=tyres', 'shop=car_parts'],
    "cleaning": ['shop=laundry', 'shop=dry_cleaning', 'craft=cleaning'],
    # HoReCa
    "cafe": ['amenity=cafe'],
    "restaurant": ['amenity=restaurant'],
    "bar": ['amenity=bar', 'amenity=pub'],
    "bakery": ['shop=bakery', 'shop=pastry'],
    "hotel": ['tourism=hotel', 'tourism=guest_house', 'tourism=hostel'],
    # ритейл
    "florist": ['shop=florist'],
    "clothes": ['shop=clothes', 'shop=shoes'],
    "furniture": ['shop=furniture', 'shop=interior_decoration'],
    "jewelry": ['shop=jewelry'],
    "pet": ['shop=pet', 'shop=pet_grooming'],
    "optician": ['shop=optician'],
    "kids": ['shop=toys', 'amenity=kindergarten'],
    # ремесленники — часто вообще без сайта, но с телефоном
    "craft": ['craft=*'],
}

# Наборы, которые логично гонять пачкой
BUNDLES: dict[str, list[str]] = {
    "beauty_all": ["hairdresser", "beauty", "nails", "tattoo", "spa"],
    # медицина для людей; vet и pharmacy — отдельно, осознанным выбором
    "medical": ["dentist", "clinic", "doctors", "lab", "rehab", "cosmetology", "hospital"],
    "horeca": ["cafe", "restaurant", "bar", "bakery"],
    "services": ["real_estate", "lawyer", "accountant", "travel", "photo", "cleaning"],
}


# Порядок обхода: мед первым, остальное — по остаточному принципу
MEDICAL_PRIORITY: tuple[str, ...] = tuple(BUNDLES["medical"])
# Категории, к которым применяется фильтр государственных/коммунальных учреждений
HEALTHCARE_CATEGORIES = frozenset(MEDICAL_PRIORITY) | {"pharmacy"}


def prioritize(selected: dict[str, list[str]]) -> dict[str, list[str]]:
    """Мед-категории вперёд (в порядке MEDICAL_PRIORITY), остальные — как были."""
    rank = {name: i for i, name in enumerate(MEDICAL_PRIORITY)}
    ordered = sorted(selected, key=lambda n: rank.get(n, len(rank)))
    return {name: selected[name] for name in ordered}


def resolve(names: list[str]) -> dict[str, list[str]]:
    """Разворачивает имена категорий и бандлов в {категория: [фильтры]}."""
    out: dict[str, list[str]] = {}
    for raw in names:
        name = raw.strip().lower()
        if name == "all":
            return dict(CATEGORIES)
        if name in BUNDLES:
            for sub in BUNDLES[name]:
                out[sub] = CATEGORIES[sub]
        elif name in CATEGORIES:
            out[name] = CATEGORIES[name]
        else:
            known = ", ".join(sorted(CATEGORIES) + sorted(BUNDLES))
            raise ValueError(f"Неизвестная категория {raw!r}. Доступно: {known}")
    return out
