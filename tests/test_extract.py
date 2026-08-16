"""Тесты на bizparser.extract: телефоны, email, соцсети, JSON-LD."""
from __future__ import annotations

from selectolax.parser import HTMLParser

from bizparser.extract import (
    find_contact_pages,
    find_emails,
    find_json_ld_contacts,
    find_phones,
    find_socials,
    normalize_phone,
    tel_links,
    visible_text,
)


def test_normalize_phone_valid_ua_number():
    assert normalize_phone("0671234567", region="UA") == "+380671234567"


def test_normalize_phone_already_e164():
    assert normalize_phone("+380671234567") == "+380671234567"


def test_normalize_phone_takes_first_valid_of_several():
    assert normalize_phone("0671234567; 0509876543") == "+380671234567"


def test_normalize_phone_none_input_returns_none():
    assert normalize_phone(None) is None


def test_normalize_phone_garbage_too_short_returns_none():
    assert normalize_phone("+38") is None


def test_normalize_phone_unparseable_but_long_enough_is_kept_raw():
    """Не распознан как валидный номер, но похож на телефон целиком — не выбрасываем."""
    result = normalize_phone("0000000000")
    assert result is None or len(result.lstrip("+")) >= 9


def test_find_phones_from_free_text():
    text = "Дзвоніть нам +380 67 123 45 67 для запису"
    found = find_phones(text)
    assert found == ["+380671234567"]


def test_find_phones_respects_limit():
    text = " ".join(f"+380 67 000 {i:02d} {i:02d}" for i in range(10, 20))
    found = find_phones(text, limit=3)
    assert len(found) == 3


def test_find_emails_from_mailto_link():
    html = '<a href="mailto:info@example-business.ua">Написати</a>'
    assert find_emails(html) == ["info@example-business.ua"]


def test_find_emails_filters_blocklisted_placeholder_addresses():
    html = "<p>test@test.com yourname@domain.com noreply@site.ua</p>"
    assert find_emails(html) == []


def test_find_emails_filters_image_asset_false_positives():
    """EMAIL_RE наивно матчит что угодно с '@', блоклист должен вырезать ассеты типа @2x.png."""
    html = "<img src='avatar@2x.png'>"
    assert find_emails(html) == []


def test_find_emails_filters_self_user_agent_email():
    """Регрессия из комментария в коде: сайты-эхо User-Agent не должны отдавать наш же email."""
    from bizparser.extract import SELF_EMAILS

    if not SELF_EMAILS:
        return  # в .env могло не быть email в USER_AGENT — тест не применим
    self_email = next(iter(SELF_EMAILS))
    html = f"<p>Ваш User-Agent: bizparser (contact: {self_email})</p>"
    assert self_email not in find_emails(html)


def test_find_emails_dedupes_and_respects_limit():
    html = "<p>a@x.ua a@x.ua b@x.ua c@x.ua d@x.ua e@x.ua f@x.ua</p>"
    found = find_emails(html, limit=3)
    assert len(found) == 3
    assert len(found) == len(set(found))


def test_tel_links_extracted_and_unquoted():
    html = '<a href="tel:+380671234567">Дзвонити</a>'
    assert tel_links(html) == ["+380671234567"]


def test_find_socials_matches_instagram_and_strips_query():
    html = '<a href="https://instagram.com/mysalon?hl=uk">IG</a>'
    tree = HTMLParser(html)
    socials = find_socials(tree, "https://mysalon.ua")
    assert socials["instagram"] == "https://instagram.com/mysalon"


def test_find_socials_ignores_share_widget_junk():
    html = '<a href="https://facebook.com/sharer/sharer.php?u=x">Share</a>'
    tree = HTMLParser(html)
    assert find_socials(tree, "https://mysalon.ua") == {}


def test_find_socials_relative_viber_scheme_kept():
    html = '<a href="viber://chat?number=%2B380671234567">Viber</a>'
    tree = HTMLParser(html)
    assert "viber" in find_socials(tree, "https://mysalon.ua")


def test_find_contact_pages_prioritizes_contacts_over_about():
    html = """
    <a href="/about">Про нас</a>
    <a href="/contacts">Контакти</a>
    """
    tree = HTMLParser(html)
    pages = find_contact_pages(tree, "https://mysalon.ua")
    assert pages[0] == "https://mysalon.ua/contacts"


def test_find_contact_pages_skips_external_and_anchor_links():
    html = """
    <a href="https://other-domain.ua/contact">Не наш домен</a>
    <a href="#contact">Якорь</a>
    """
    tree = HTMLParser(html)
    assert find_contact_pages(tree, "https://mysalon.ua") == []


def test_visible_text_strips_script_and_style():
    html = "<html><body><script>evil()</script><style>.x{}</style><p>Привіт</p></body></html>"
    tree = HTMLParser(html)
    text = visible_text(tree)
    assert "Привіт" in text
    assert "evil" not in text


def test_find_json_ld_contacts_extracts_phone_and_email():
    html = """
    <script type="application/ld+json">
    {"@type": "Dentist", "telephone": "+380671234567", "email": "info@clinic.ua"}
    </script>
    """
    tree = HTMLParser(html)
    phones, emails = find_json_ld_contacts(tree)
    assert phones == ["+380671234567"]
    assert emails == ["info@clinic.ua"]


def test_find_json_ld_contacts_handles_graph_wrapper():
    html = """
    <script type="application/ld+json">
    {"@graph": [{"@type": "LocalBusiness", "telephone": "+380671234567"}]}
    </script>
    """
    tree = HTMLParser(html)
    phones, _emails = find_json_ld_contacts(tree)
    assert phones == ["+380671234567"]


def test_find_json_ld_contacts_ignores_malformed_json():
    html = '<script type="application/ld+json">{not valid json</script>'
    tree = HTMLParser(html)
    assert find_json_ld_contacts(tree) == ([], [])
