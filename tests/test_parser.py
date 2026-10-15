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
