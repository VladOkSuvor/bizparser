"""Подходит ли профиль Instagram из выдачи DDG к лиду.

Выдача по `site:instagram.com "<name>" <city>` охотно подсовывает чужое:
одноимённую клинику из другого города, агрегатор, блогера, который отметил
место. Писать в директ такому аккаунту — сжечь лимит и репутацию, поэтому
профиль принимаем, только если совпало название (тот же порог похожести, что у
гео-дедупа) И в профиле упомянут город.

У украинских клиник название в базе кириллицей, а username — латиницей,
поэтому обе стороны сравниваем ещё и в транслите.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .config import settings
from .dedupe import name_similarity, normalize_name
from .websearch import SearchResult

# Упрощённый транслит UA/RU → латиница: ровно настолько, чтобы «Посмішка» и
# «posmishka» сошлись. Точность паспортного стандарта тут не нужна.
TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
    "ю": "iu", "я": "ia", "ы": "y", "э": "e", "ъ": "", "ё": "e", "'": "", "’": "",
})

# «Name (@handle) • Instagram photos and videos» → «Name»
TITLE_TAIL_RE = re.compile(r"\s*(\(@[^)]*\)|[•|·]\s*instagram.*$)", re.I)
NON_ALNUM_RE = re.compile(r"[\W_]+", re.UNICODE)

# Короче этого вложенность («дент» ⊂ «dentalpro») ничего не доказывает
MIN_CONTAINED = 4


def translit(text: str) -> str:
    return text.lower().translate(TRANSLIT)


def _compact(text: str) -> str:
    return NON_ALNUM_RE.sub("", translit(text))


def _similarity(a: str, b: str) -> float:
    """name_similarity из dedupe, но без поблажки за вложенность коротких строк."""
    if not a or not b:
        return 0.0
    if a != b and min(len(a), len(b)) < MIN_CONTAINED and (a in b or b in a):
        return SequenceMatcher(None, a, b).ratio()
    return name_similarity(a, b)


def username(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _city_variants(city: str) -> set[str]:
    city = city.lower().strip()
    if not city:
        return set()
    # Падежи: «у Харкові», «в Ужгороді» — сравниваем по основе
    stem = city[: max(MIN_CONTAINED, len(city) - 2)]
    return {city, stem, translit(city), _compact(city)}


@dataclass
class Match:
    url: str
    score: float
    city_ok: bool

    @property
    def handle(self) -> str:
        return username(self.url)


def score_profile(name: str, city: str, result: SearchResult) -> Match:
    title = TITLE_TAIL_RE.sub("", result.title).strip()
    ours = normalize_name(name)
    theirs = normalize_name(title) if title else ""
    handle = username(result.url)

    score = max(
        _similarity(ours, theirs),
        _similarity(translit(ours), translit(theirs)),
        # username без точек/подчёркиваний против названия без пробелов
        _similarity(_compact(ours), _compact(handle)),
    )

    haystack = f"{result.title} {result.snippet} {handle}".lower()
    hay_variants = {haystack, translit(haystack), _compact(haystack)}
    city_ok = any(v in h for v in _city_variants(city) for h in hay_variants)
    return Match(url=result.url, score=score, city_ok=city_ok)


def best_candidate(name: str, city: str, results: list[SearchResult]) -> Match | None:
    """Самый похожий профиль без порогов — чтобы в логе было видно, почему отказали."""
    return max((score_profile(name, city, r) for r in results),
               key=lambda m: (m.score, m.city_ok), default=None)


def pick_profile(
    name: str,
    city: str,
    results: list[SearchResult],
    threshold: float | None = None,
    require_city: bool = True,
) -> Match | None:
    """Лучший профиль, прошедший порог. None — подходящего в выдаче нет."""
    threshold = threshold if threshold is not None else settings.dedupe_threshold
    candidates = [score_profile(name, city, r) for r in results]
    good = [m for m in candidates if m.score >= threshold and (m.city_ok or not require_city)]
    return max(good, key=lambda m: (m.city_ok, m.score), default=None)
