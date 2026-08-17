"""Эвристика размера бизнеса.

Это именно эвристика, а не факт: OSM не хранит выручку и штат. Здесь мы
собираем косвенные сигналы и честно кладём их в size_signals, чтобы потом
было видно, на чём основана оценка. Для реальной валидации живости ФОП/ТОВ
нужен ЄДР с data.gov.ua — см. README, раздел «ЄДР».
"""

from __future__ import annotations

# Базовый вес по типу заведения: кофейня почти всегда меньше клиники
BASELINE = {
    "hairdresser": 0, "nails": 0, "tattoo": 0, "florist": 0, "photo": 0, "craft": 0,
    "jewelry": 0,
    "beauty": 1, "cafe": 1, "bakery": 1, "bar": 1, "cleaning": 1, "pet": 1, "optician": 1,
    "repair_car": 1, "lawyer": 1, "accountant": 1, "travel": 1, "real_estate": 1,
    "insurance": 1, "clothes": 1, "furniture": 1, "kids": 1,
    "restaurant": 2, "dentist": 2, "vet": 2, "fitness": 2, "spa": 2, "language_school": 2,
    "driving_school": 2,
    "clinic": 3, "hotel": 3, "pharmacy": 2,
}

CHAIN_TAGS = ("brand", "brand:wikidata", "operator", "operator:wikidata")

# Пороги классов — вынесены, чтобы не были «магией» в теле функции
SMALL_THRESHOLD = 2
MEDIUM_THRESHOLD = 5

# Значения OSM, которые фактически означают «нет» и не должны считаться сигналом.
# Без этого internet_access=no попадал в «истинность» и накидывал баллы.
_NEGATIVE = {"no", "none", "false", "0", ""}


def _present(value: object) -> bool:
    """True только если тег реально заполнен осмысленным значением (не 'no'/пусто)."""
    return value is not None and str(value).strip().lower() not in _NEGATIVE


def _as_int(raw: object) -> int | None:
    """Числовое OSM-значение → int. Терпит '50', ' 50 ', '50 seats', 'approx 120'.

    Возвращает первое встреченное целое число или None. Не падает, если раньше
    в тег прилетала не строка (например, int) — прежний raw.isdigit() падал.
    """
    if raw is None:
        return None
    digits = ""
    for ch in str(raw):
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else None


def estimate(tags: dict | None, category: str, branch_count: int = 1) -> tuple[str, dict]:
    """Возвращает ('micro'|'small'|'medium', signals)."""
    tags = tags or {}
    signals: dict[str, object] = {}
    score = BASELINE.get(category, 1)
    signals["baseline_category"] = score

    # branch_count должен приходить как число РАЗНЫХ точек (после dedupe): иначе
    # одно место, пришедшее и как node, и как way, само себя раздует. Страхуемся.
    branch_count = max(1, branch_count)
    if branch_count > 1:
        bump = 2 if branch_count >= 4 else 1
        score += bump
        signals["branches_same_name"] = branch_count

    chain = [t for t in CHAIN_TAGS if _present(tags.get(t))]
    if chain:
        score += 2
        signals["chain_tags"] = chain

    # Цифровая зрелость: считаем РЕАЛЬНЫЕ каналы, а не теги. website и
    # contact:website (то же с email) — это один и тот же канал, записанный
    # двумя ключами, и раньше давал ложные «2 канала».
    has_web = _present(tags.get("website")) or _present(tags.get("contact:website"))
    has_email = _present(tags.get("email")) or _present(tags.get("contact:email"))
    digital = int(has_web) + int(has_email)
    if digital >= 2:
        score += 1
        signals["digital_presence"] = {"web": has_web, "email": has_email}

    # Явные метрики вместимости, если кто-то их проставил
    for key in ("capacity", "rooms", "beds", "capacity:persons"):
        value = _as_int(tags.get(key))
        if value is not None:
            signals[key] = value
            if value >= 50:
                score += 2
            elif value >= 15:
                score += 1
            break

    hours = str(tags.get("opening_hours", ""))
    if "24/7" in hours:
        score += 1
        signals["opening_hours"] = "24/7"

    # Удобства (Wi-Fi/пандус) НЕ коррелируют с размером — у крошечной кофейни
    # есть вайфай. Оставляем как метаданные, но в score больше не вносим.
    amenities = []
    if tags.get("wheelchair") == "yes":
        amenities.append("wheelchair")
    if _present(tags.get("internet_access")):
        amenities.append("internet_access")
    if amenities:
        signals["amenities"] = amenities

    signals["score"] = score
    signals["confidence"] = "low"  # честно: сигналов мало и они косвенные

    if score >= MEDIUM_THRESHOLD:
        return "medium", signals
    if score >= SMALL_THRESHOLD:
        return "small", signals
    return "micro", signals
