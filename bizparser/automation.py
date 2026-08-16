"""Детектор «уже автоматизирован»: что у бизнеса стоит на сайте.

Самый ценный сигнал для продажи ботов записи — не размер бизнеса, а наличие
(или отсутствие) виджета записи, чата, колбэка, CRM. Добывается бесплатно на том
же проходе, что и enrichment: смотрим <script src>, <iframe src>, ссылки и inline-JS.

Два применения:
  * `--no-automation` — горячие лиды, у которых нет вообще ничего;
  * `--has-automation` — есть конкурентское решение, можно питчить замену.
"""

from __future__ import annotations

import re

# (regex, kind, vendor). Ищем по сырому HTML — сигнатуры выбраны так, чтобы
# встречаться в URL ассетов, а не в обычном тексте.
SIGNATURES: list[tuple[str, str, str]] = [
    # --- онлайн-запись ---
    (r"yclients\.com|n\d+\.yclients", "booking", "yclients"),
    (r"alteg\.io|altegio\.com", "booking", "altegio"),
    (r"dikidi\.(net|ru|app)", "booking", "dikidi"),
    (r"calendly\.com", "booking", "calendly"),
    (r"setmore\.com|my\.setmore", "booking", "setmore"),
    (r"simplybook\.(me|it)", "booking", "simplybook"),
    (r"booksy\.com", "booking", "booksy"),
    (r"fresha\.com", "booking", "fresha"),
    (r"reservio\.com", "booking", "reservio"),
    (r"appointlet\.com", "booking", "appointlet"),
    (r"squareup\.com/appointments", "booking", "square_appointments"),
    (r"cal\.com/(embed|api)", "booking", "cal.com"),
    (r"zapisonline|onlinezapis", "booking", "zapisonline"),
    (r"helsi\.me", "booking", "helsi"),
    (r"doc\.ua", "booking", "doc.ua"),
    (r"likarni\.com", "booking", "likarni"),
    (r"restoplace\.(ws|cc)", "booking", "restoplace"),
    (r"thefork\.com|lafourchette", "booking", "thefork"),
    (r"bookform\.ru|widget\.bnovo", "booking", "bnovo"),
    # --- чат-виджеты ---
    (r"tidio\.co|tidiochat", "chat", "tidio"),
    (r"crisp\.chat", "chat", "crisp"),
    (r"jivo(site)?\.(ru|com|chat)", "chat", "jivosite"),
    (r"tawk\.to", "chat", "tawk"),
    (r"chatra\.io", "chat", "chatra"),
    (r"widget\.intercom\.io|intercomcdn", "chat", "intercom"),
    (r"smartsupp\.com", "chat", "smartsupp"),
    (r"helpcrunch\.com", "chat", "helpcrunch"),
    (r"js\.driftt\.com", "chat", "drift"),
    (r"static\.zdassets\.com|zopim", "chat", "zendesk"),
    (r"livechatinc\.com", "chat", "livechat"),
    (r"verbox\.ru|siteheart", "chat", "verbox"),
    (r"replain\.cc", "chat", "replain"),
    (r"umnico\.com", "chat", "umnico"),
    (r"chaport\.com", "chat", "chaport"),
    (r"chatbot\.com|manychat\.com", "chat", "manychat"),
    # --- CRM и колл-трекинг (в UA очень распространены) ---
    (r"bitrix24|b24-[\w]+\.bitrix24|cdn-ru\.bitrix24", "crm", "bitrix24"),
    (r"keycrm\.app", "crm", "keycrm"),
    (r"salesdrive\.me", "crm", "salesdrive"),
    (r"binotel\.(com|ua)", "calltracking", "binotel"),
    (r"ringostat\.(com|net)", "calltracking", "ringostat"),
    (r"phonet\.com\.ua", "calltracking", "phonet"),
    (r"stream-?telecom|zadarma\.com/widget", "calltracking", "other_telephony"),
    # --- обратный звонок ---
    (r"callbackhunter|callback-?hunter", "callback", "callbackhunter"),
    (r"envybox|envycdn", "callback", "envybox"),
    (r"callbackkiller|marquiz\.ru", "callback", "other_callback"),
    # --- мессенджер-кнопки (слабее, чем полноценная запись) ---
    (r"telegram\.org/js/telegram-widget", "messenger", "telegram_widget"),
    (r"t\.me/[\w_]+\?start=", "messenger", "telegram_bot"),
    (r"wa\.me/\d|api\.whatsapp\.com/send", "messenger", "whatsapp_button"),
    (r"viber://(chat|pa)", "messenger", "viber_button"),
    (r"getbutton\.io", "messenger", "getbutton"),
    # --- формы самозаписи (тоже слабый сигнал) ---
    (r"docs\.google\.com/forms", "form", "google_forms"),
    (r"typeform\.com", "form", "typeform"),
    (r"jotform\.com", "form", "jotform"),
    # --- платформа сайта: значит, есть подрядчик или конструктор ---
    (r"cdn\.shopify\.com", "platform", "shopify"),
    (r"tildacdn|tilda\.ws", "platform", "tilda"),
    (r"wixstatic\.com|parastorage\.com", "platform", "wix"),
    (r"horoshop\.ua", "platform", "horoshop"),
    (r"prom\.ua/cabinet|khoroshop", "platform", "prom"),
    (r"/wp-content/|/wp-includes/", "platform", "wordpress"),
    (r"squarespace\.com|static1\.squarespace", "platform", "squarespace"),
    (r"cdn\.readymag|webflow\.com", "platform", "webflow"),
]

COMPILED = [(re.compile(pattern, re.I), kind, vendor) for pattern, kind, vendor in SIGNATURES]

# Текстовая подсказка: кнопка «записатися онлайн» без известного вендора.
# Значит, запись есть, но самописная — тоже полезно знать.
TEXT_HINT = re.compile(
    r"(запис(атися|атись|аться|ь)\s+онлайн|онлайн[-\s]?запис|online\s+booking|"
    r"book\s+(now|online)|забронювати|забронировать|записатися\s+на\s+прийом)",
    re.I,
)

# Виды, которые реально означают «этот клиент уже автоматизирован».
# platform/form/messenger — слабые, они не закрывают задачу записи.
STRONG_KINDS = frozenset({"booking", "chat", "callback", "crm", "calltracking"})


def detect(html: str) -> dict[str, list[str]]:
    """Возвращает {kind: [vendor, ...]} по одной странице."""
    found: dict[str, list[str]] = {}
    for pattern, kind, vendor in COMPILED:
        if pattern.search(html):
            bucket = found.setdefault(kind, [])
            if vendor not in bucket:
                bucket.append(vendor)
    if "booking" not in found and TEXT_HINT.search(html):
        found.setdefault("booking_hint", []).append("custom_or_manual")
    return found


def merge(target: dict[str, list[str]], addition: dict[str, list[str]]) -> None:
    """Аккумулирует находки с нескольких страниц одного сайта (in-place)."""
    for kind, vendors in addition.items():
        bucket = target.setdefault(kind, [])
        for vendor in vendors:
            if vendor not in bucket:
                bucket.append(vendor)


def is_automated(automation: dict[str, list[str]] | None) -> bool:
    """Есть ли хоть один «сильный» признак автоматизации."""
    if not automation:
        return False
    return any(kind in STRONG_KINDS for kind in automation)


def describe(automation: dict[str, list[str]] | None) -> str:
    if not automation:
        return "—"
    return ", ".join(f"{k}:{'/'.join(v)}" for k, v in sorted(automation.items()))
