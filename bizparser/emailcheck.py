"""Проверка доменов email по MX-записям.

Regex достаёт синтаксически валидные адреса, но не факт что живые: домен мог
протухнуть, а опечатка в вёрстке жить годами. Один DNS-запрос на домен — дёшево,
а репутацию отправителя при холодной рассылке бережёт.

Проверяем именно домен, а не конкретный ящик: SMTP-верификация мейлбокса (RCPT TO
без отправки) выглядит для принимающей стороны как разведка перед спамом и быстро
приводит в блек-листы. Домен без MX — гарантированный bounce, этого достаточно.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

try:
    import dns.exception
    import dns.resolver

    DNS_AVAILABLE = True
except ImportError:  # pragma: no cover
    DNS_AVAILABLE = False

_cache: dict[str, bool] = {}

# Публичные почтовики — заведомо валидны, DNS дёргать незачем
KNOWN_GOOD = {
    "gmail.com", "googlemail.com", "ukr.net", "i.ua", "meta.ua", "outlook.com",
    "hotmail.com", "yahoo.com", "icloud.com", "proton.me", "protonmail.com",
    "bigmir.net", "online.ua", "email.ua",
}


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower()


def domain_has_mx(domain: str, timeout: float = 5.0) -> bool:
    """True, если домен способен принимать почту (есть MX или хотя бы A-запись)."""
    if domain in KNOWN_GOOD:
        return True
    if domain in _cache:
        return _cache[domain]
    if not DNS_AVAILABLE:
        log.warning("dnspython не установлен — проверка MX пропущена")
        return True

    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    resolver.timeout = timeout

    result = False
    try:
        answers = resolver.resolve(domain, "MX")
        result = len(answers) > 0
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        # По RFC 5321 при отсутствии MX почта идёт на A-запись домена
        try:
            resolver.resolve(domain, "A")
            result = True
        except dns.exception.DNSException:
            result = False
    except dns.exception.DNSException as exc:
        # Таймаут — не приговор домену, но и подтверждением не считаем
        log.debug("DNS для %s не ответил: %s", domain, exc)
        result = False

    _cache[domain] = result
    return result


def check_email(email: str | None) -> bool | None:
    """None — если проверять нечего."""
    if not email or "@" not in email:
        return None
    return domain_has_mx(domain_of(email))


def check_many(emails: list[str], workers: int = 8) -> dict[str, bool]:
    """DNS-запросы блокирующие, но независимые — раскидываем по потокам."""
    unique = sorted({domain_of(e) for e in emails if e and "@" in e})
    with ThreadPoolExecutor(max_workers=workers) as pool:
        verdicts = dict(zip(unique, pool.map(domain_has_mx, unique)))
    return {email: verdicts.get(domain_of(email), False) for email in emails if email}
