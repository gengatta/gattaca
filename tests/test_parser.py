from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from assistant.parser import ParseError, format_offset, parse_event, parse_offsets

TZ = ZoneInfo("Europe/Warsaw")
# Четверг, 15 октября 2026, 12:00
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=TZ)


def p(text):
    return parse_event(text, NOW)


@pytest.mark.parametrize(
    "text, title, expected",
    [
        ("25.10 18:30 Стоматолог", "Стоматолог", datetime(2026, 10, 25, 18, 30)),
        ("Стоматолог 25.10.2026 в 18:30", "Стоматолог", datetime(2026, 10, 25, 18, 30)),
        ("2026-11-02 09:15 отчёт", "Отчёт", datetime(2026, 11, 2, 9, 15)),
        ("завтра в 10:00 созвон с Анной", "Созвон с Анной", datetime(2026, 10, 16, 10, 0)),
        ("Сегодня 19:00 ужин", "Ужин", datetime(2026, 10, 15, 19, 0)),
        ("послезавтра день рождения мамы", "День рождения мамы", datetime(2026, 10, 17, 9, 0)),
        ("в пятницу 15:00 маникюр", "Маникюр", datetime(2026, 10, 16, 15, 0)),
        ("во вторник 8:00 бассейн", "Бассейн", datetime(2026, 10, 20, 8, 0)),
        ("в четверг 9:00 йога", "Йога", datetime(2026, 10, 22, 9, 0)),  # сегодня чт, но 9:00 прошло
        ("в 18:00 позвонить маме", "Позвонить маме", datetime(2026, 10, 15, 18, 0)),
        ("11:00 планёрка", "Планёрка", datetime(2026, 10, 16, 11, 0)),  # уже прошло -> завтра
        ("10.01 визит к врачу", "Визит к врачу", datetime(2027, 1, 10, 9, 0)),  # следующий год
        ("через 2 часа забрать посылку", "Забрать посылку", datetime(2026, 10, 15, 14, 0)),
        ("через 30 минут чай", "Чай", datetime(2026, 10, 15, 12, 30)),
        ("через час выйти из дома", "Выйти из дома", datetime(2026, 10, 15, 13, 0)),
        ("через 3 дня оплатить счёт", "Оплатить счёт", datetime(2026, 10, 18, 12, 0)),
    ],
)
def test_parse(text, title, expected):
    ev = p(text)
    assert ev.title == title
    assert ev.starts_at == expected.replace(tzinfo=TZ)


@pytest.mark.parametrize(
    "text",
    ["просто текст", "", "завтра", "15.10.2026 10:00 прошло", "31.02 10:00 бред", "сегодня 10:00 поздно"],
)
def test_errors(text):
    with pytest.raises(ParseError):
        p(text)


def test_default_time():
    ev = parse_event("завтра купить цветы", NOW, default_time=time(10, 30))
    assert ev.starts_at == datetime(2026, 10, 16, 10, 30, tzinfo=TZ)


def test_offsets():
    assert parse_offsets("1d, 2h,30m,0m") == [
        timedelta(days=1), timedelta(hours=2), timedelta(minutes=30), timedelta(0)
    ]
    assert parse_offsets("1д,1ч") == [timedelta(days=1), timedelta(hours=1)]
    with pytest.raises(ParseError):
        parse_offsets("завтра")


def test_format_offset():
    assert format_offset(timedelta(0)) == "в момент начала"
    assert format_offset(timedelta(days=1)) == "за 1 день"
    assert format_offset(timedelta(hours=3)) == "за 3 часа"
    assert format_offset(timedelta(minutes=15)) == "за 15 минут"
    assert format_offset(timedelta(minutes=21)) == "за 21 минуту"


def test_extract_partial():
    from assistant.parser import extract, looks_like_schedule

    d = extract("послезавтра день рождения мамы", NOW)
    assert d.title == "День рождения мамы" and d.missing == "time"
    d = extract("стрижка", NOW)
    assert d.missing == "date" and not d.found_anything
    assert extract("в 18:00 позвонить", NOW).missing is None
    assert looks_like_schedule("пн 9:00 бассейн\nср 15:00 маникюр")
    assert looks_like_schedule("в пятницу врач, а в субботу стрижка")
    assert not looks_like_schedule("25.10 18:30 Стоматолог")
    assert not looks_like_schedule("через 2 часа забрать посылку")


MSK = ZoneInfo("Europe/Moscow")
NOW_MSK = datetime(2026, 10, 15, 12, 0, tzinfo=MSK)  # четверг


@pytest.mark.parametrize(
    "text, title, kind, expected",
    [
        ("сдать эссе до пятницы", "Сдать эссе", "task", datetime(2026, 10, 16, 23, 59)),
        ("отправить отчёт к 25.10", "Отправить отчёт", "task", datetime(2026, 10, 25, 23, 59)),
        ("дедлайн 20.10 18:00 презентация", "Презентация", "task", datetime(2026, 10, 20, 18, 0)),
        ("к понедельнику подготовить слайды", "Подготовить слайды", "task", datetime(2026, 10, 19, 23, 59)),
        ("до среды оплатить курс", "Оплатить курс", "task", datetime(2026, 10, 21, 23, 59)),
        ("в пятницу 15:00 маникюр", "Маникюр", "event", datetime(2026, 10, 16, 15, 0)),
    ],
)
def test_tasks(text, title, kind, expected):
    from assistant.parser import extract, finalize

    d = extract(text, NOW_MSK)
    assert (d.title, d.kind) == (title, kind)
    assert finalize(d, NOW_MSK) == expected.replace(tzinfo=MSK)


def test_inbox_and_tags():
    from assistant.parser import extract, finalize

    d = extract("задача: разобрать почту", NOW_MSK)
    assert (d.title, d.kind, d.missing) == ("Разобрать почту", "task", None)
    assert finalize(d, NOW_MSK) is None
    d = extract("#сша #задача обновить договор", NOW_MSK)
    assert (d.title, d.kind, d.tags) == ("Обновить договор", "task", ["сша"])


@pytest.mark.parametrize(
    "text, zone, local",
    [
        ("созвон в 10:00 по Нью-Йорку", "America/New_York", datetime(2026, 10, 15, 17, 0)),
        ("звонок 9:30 PT", "America/Los_Angeles", datetime(2026, 10, 15, 19, 30)),
        ("завтра 11:00 по времени Лос-Анджелеса демо", "America/Los_Angeles", datetime(2026, 10, 16, 21, 0)),
        ("встреча 18:00 мск", None, datetime(2026, 10, 15, 18, 0)),  # свой пояс — не помечаем
    ],
)
def test_timezones(text, zone, local):
    from assistant.parser import extract, finalize

    d = extract(text, NOW_MSK)
    assert d.tz == zone
    assert finalize(d, NOW_MSK).astimezone(MSK).replace(tzinfo=None) == local


def test_city_without_po_is_not_timezone():
    from assistant.parser import extract

    d = extract("встреча в Москве завтра 10:00", NOW_MSK)
    assert d.tz is None and d.title == "Встреча в Москве"


def test_zone_resolve():
    from assistant import zones

    assert zones.resolve("Москва") == "Europe/Moscow"
    assert zones.resolve("нью-йорк") == "America/New_York"
    assert zones.resolve("PT") == "America/Los_Angeles"
    assert zones.resolve("Europe/Warsaw") == "Europe/Warsaw"
    assert zones.resolve("Хогвартс") is None


@pytest.mark.parametrize(
    "title, tags, zone, area",
    [
        ("Пара по маркетингу", [], None, "Учёба"),
        ("Сдать эссе", [], None, "Учёба"),
        ("Созвон с клиентом", [], "America/New_York", "Работа США"),
        ("Отчёт для офиса в Москве", [], None, "Работа РФ"),
        ("Маникюр", [], None, "Личное"),
        ("Что-то", ["сша"], None, "Работа США"),
        ("Купить продукты", [], None, None),
        ("Папка с документами", [], None, None),  # «пап» ≠ «папа»
    ],
)
def test_area_detection(title, tags, zone, area):
    from assistant.areas import DEFAULT_AREAS, detect

    assert detect(DEFAULT_AREAS, title, tags, zone) == area
