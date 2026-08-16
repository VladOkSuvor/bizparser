"""Извлечение и нормализация контактов из текста/HTML."""

from __future__ import annotations

import json
import re
from typing import Iterator
from urllib.parse import unquote, urljoin, urlparse

import phonenumbers
from selectolax.parser import HTMLParser

from .config import settings

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

# Короче этого номером быть не может даже без кода страны (UA: 0XX XXX XX XX)
MIN_PHONE_DIGITS = 9

# Мусорные адреса, которые встречаются в шаблонах, аналитике и заглушках
EMAIL_BLOCKLIST = re.compile(
    r"(example[.@]|sentry\.|wixpress\.|\.png|\.jpe?g|\.gif|\.svg|\.webp|@2x|"
    r"your(name|email)|email@|domain\.com|test@|noreply|no-reply|"
    r"mail@mail\.|user@|name@|info@site|^admin@localhost)",
    re.I,
)

# Наш собственный контакт из User-Agent. Некоторые сайты (WAF, дебаг-панели,
# «мы вас видим» блоки) печатают User-Agent запроса прямо в HTML — и тогда твой
# же адрес уезжает в базу как контакт клиента. Ловили на реальном сайте.
SELF_EMAILS = {e.lower() for e in re.findall(EMAIL_RE, settings.user_agent)}

SOCIAL_PATTERNS = {
    "instagram": re.compile(r"^https?://(?:www\.)?instagram\.com/([\w.\-]+)", re.I),
    "facebook": re.compile(r"^https?://(?:www\.|m\.)?facebook\.com/([\w.\-]+)", re.I),
    "telegram": re.compile(r"^https?://(?:t\.me|telegram\.me)/([\w.\-+]+)", re.I),
    "tiktok": re.compile(r"^https?://(?:www\.)?tiktok\.com/(@[\w.\-]+)", re.I),
    "youtube": re.compile(r"^https?://(?:www\.)?youtube\.com/(@?[\w.\-/]+)", re.I),
    "viber": re.compile(r"^viber://.*", re.I),
    "whatsapp": re.compile(r"^https?://(?:wa\.me|api\.whatsapp\.com)/(\S+)", re.I),
    "linkedin": re.compile(r"^https?://(?:[\w]+\.)?linkedin\.com/(company|in)/([\w.\-]+)", re.I),
}

# Соцсети самой платформы/шаблона, а не бизнеса
SOCIAL_JUNK = {"sharer", "share.php", "intent", "plugins", "profile.php?id=0"}

CONTACT_LINK_RE = re.compile(
    r"(контакт|contact|kontakt|звяж|зв.?язатися|связ|about|про[- _]?нас|о[- _]?нас|reach)",
    re.I,
)


def normalize_phone(raw: str | None, region: str | None = None) -> str | None:
    """Приводит к E.164 (+380671234567). Если не парсится — возвращает как есть, обрезав мусор."""
    if not raw:
        return None
    region = region or settings.default_region
    # В OSM несколько номеров пишут через ";" — берём первый валидный
    for candidate in re.split(r"[;/]| или ", raw):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            num = phonenumbers.parse(candidate, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(num):
            return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)

    # Не распарсилось — оставляем как есть только если это похоже на номер целиком.
    # Иначе в базу попадают обрубки вроде "+38" из битых tel:-ссылок, а они
    # выглядят как контакт и портят выгрузку для обзвона.
    cleaned = re.sub(r"[^\d+()\-\s]", "", raw).strip()
    return cleaned if len(re.sub(r"\D", "", cleaned)) >= MIN_PHONE_DIGITS else None


def find_phones(text: str, region: str | None = None, limit: int = 5) -> list[str]:
    """Ищет телефоны в свободном тексте. PhoneNumberMatcher надёжнее любой regex-простыни."""
    region = region or settings.default_region
    found: list[str] = []
    for match in phonenumbers.PhoneNumberMatcher(text, region):
        if not phonenumbers.is_valid_number(match.number):
            continue
        e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
        if e164 not in found:
            found.append(e164)
        if len(found) >= limit:
            break
    return found


def _email_ok(email: str) -> bool:
    return bool(email) and email not in SELF_EMAILS and not EMAIL_BLOCKLIST.search(email)


def find_emails(html: str, limit: int = 5) -> list[str]:
    found: list[str] = []
    # mailto: приоритетнее — там почти не бывает ложных срабатываний
    for raw in re.findall(r'mailto:([^"\'\s>?]+)', html, re.I):
        email = unquote(raw).strip().lower()
        if EMAIL_RE.fullmatch(email) and _email_ok(email) and email not in found:
            found.append(email)
    for email in EMAIL_RE.findall(html):
        email = email.lower()
        if not _email_ok(email) or email in found:
            continue
        found.append(email)
        if len(found) >= limit:
            break
    return found[:limit]


def find_socials(tree: HTMLParser, base_url: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href:
            continue
        url = href if href.startswith(("http", "viber:")) else urljoin(base_url, href)
        if any(junk in url for junk in SOCIAL_JUNK):
            continue
        for network, pattern in SOCIAL_PATTERNS.items():
            if network in out:
                continue
            if pattern.match(url):
                out[network] = url.split("?")[0]
    return out


def find_contact_pages(tree: HTMLParser, base_url: str, limit: int = 3) -> list[str]:
    """Ссылки, за которыми вероятнее всего лежат контакты."""
    base_host = urlparse(base_url).netloc
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if urlparse(url).netloc != base_host or url in seen:
            continue
        text = (node.text() or "") + " " + href
        if not CONTACT_LINK_RE.search(text):
            continue
        seen.add(url)
        # "контакты" важнее, чем "о нас"
        weight = 0 if re.search(r"контакт|contact|kontakt", text, re.I) else 1
        scored.append((weight, url))

    scored.sort(key=lambda pair: pair[0])
    return [url for _, url in scored[:limit]]


def visible_text(tree: HTMLParser) -> str:
    for tag in ("script", "style", "noscript", "svg"):
        for node in tree.css(tag):
            node.decompose()
    body = tree.body or tree.root
    return body.text(separator=" ", strip=True) if body else ""


def tel_links(html: str) -> list[str]:
    return [unquote(raw) for raw in re.findall(r'tel:([^"\'\s>]+)', html, re.I)]


def _ld_nodes(data: object) -> Iterator[dict]:
    """Разворачивает JSON-LD: объект, массив объектов или {"@graph": [...]}."""
    if isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                yield from _ld_nodes(node)
        else:
            yield data
    elif isinstance(data, list):
        for item in data:
            yield from _ld_nodes(item)


def _as_list(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def find_json_ld_contacts(tree: HTMLParser) -> tuple[list[str], list[str]]:
    """Телефон/почта из <script type="application/ld+json"> (schema.org/LocalBusiness
    и подвиды — Dentist, BeautySalon, Restaurant...). Многие сайты кладут контакт
    только сюда, для SEO/Google Business — глазами на странице его может и не быть.
    """
    phones: list[str] = []
    emails: list[str] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text(strip=True))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        for item in _ld_nodes(data):
            if not isinstance(item, dict):
                continue
            for raw_phone in _as_list(item.get("telephone")):
                phone = normalize_phone(str(raw_phone))
                if phone and phone not in phones:
                    phones.append(phone)
            for raw_email in _as_list(item.get("email")):
                email = str(raw_email).strip().lower()
                if EMAIL_RE.fullmatch(email) and _email_ok(email) and email not in emails:
                    emails.append(email)
    return phones, emails
