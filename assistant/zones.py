"""Часовые пояса: «созвон в 10:00 по Нью-Йорку», «/tz Москва»."""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Русские названия городов во всех ходовых падежах (по Москве, в Москве, Москва)
CITIES: list[tuple[str, str]] = [
    (r"москв[аеуы]|мск", "Europe/Moscow"),
    (r"питер[ау]?|спб|санкт[- ]петербург[ау]?", "Europe/Moscow"),
    (r"калининград[ау]?", "Europe/Kaliningrad"),
    (r"самар[аеуы]", "Europe/Samara"),
    (r"екатеринбург[ау]?|екб", "Asia/Yekaterinburg"),
    (r"новосибирск[ау]?", "Asia/Novosibirsk"),
    (r"владивосток[ау]?", "Asia/Vladivostok"),
    (r"нью[- ]?йорк[ау]?|восточному(?: времени)?|бостон[ау]?|майами|вашингтон[ау]?|флорид[аеуы]",
     "America/New_York"),
    (r"чикаго|техас[ау]?|хьюстон[ау]?|далл?ас[ау]?", "America/Chicago"),
    (r"денвер[ау]?", "America/Denver"),
    (r"лос[- ]?анджелес[ау]?|калифорни[июяей]|сан[- ]?франциско|сиэтл[ау]?|тихоокеанскому(?: времени)?",
     "America/Los_Angeles"),
    (r"варшав[аеуы]|польш[аеи]", "Europe/Warsaw"),
    (r"лондон[ау]?", "Europe/London"),
    (r"берлин[ау]?", "Europe/Berlin"),
    (r"париж[ау]?", "Europe/Paris"),
    (r"киев[ау]?", "Europe/Kyiv"),
    (r"минск[ау]?", "Europe/Minsk"),
    (r"стамбул[ау]?", "Europe/Istanbul"),
    (r"тбилиси", "Asia/Tbilisi"),
    (r"ереван[ау]?", "Asia/Yerevan"),
    (r"алмат[аыеу]", "Asia/Almaty"),
    (r"дуба[ейю]", "Asia/Dubai"),
]

# Латинские сокращения — только заглавными, чтобы не путать с обычными словами
CODES: dict[str, str] = {
    "MSK": "Europe/Moscow",
    "NY": "America/New_York", "NYC": "America/New_York",
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "LA": "America/Los_Angeles", "SF": "America/Los_Angeles",
    "UK": "Europe/London", "CET": "Europe/Berlin",
}

LABELS = {
    "Europe/Moscow": "Мск",
    "America/New_York": "NY",
    "America/Chicago": "Чикаго",
    "America/Denver": "Денвер",
    "America/Los_Angeles": "LA",
    "Europe/Warsaw": "Варшава",
    "Europe/London": "Лондон",
}

_B, _E = r"(?<![\w])", r"(?![\w])"
_CITY_ALT = "|".join(f"(?:{p})" for p, _ in CITIES)
# «по Москве», «по времени Нью-Йорка», «(Мск)», «мск»
RE_CITY = re.compile(
    _B + r"(?:по\s+(?:времени\s+)?|time\s+)?(" + _CITY_ALT + r")(?:\s+времени)?" + _E,
    re.IGNORECASE,
)
RE_CODE = re.compile(
    _B + r"(?:по\s+)?(" + "|".join(sorted(CODES, key=len, reverse=True)) + r")(?:\s+time)?" + _E
)


def _city_zone(word: str) -> str | None:
    for pattern, zone in CITIES:
        if re.fullmatch(pattern, word, re.IGNORECASE):
            return zone
    return None


def find_zone(text: str) -> tuple[str, re.Match] | None:
    """Ищет в тексте указание часового пояса. Возвращает (IANA-имя, совпадение)."""
    # Город считается указанием пояса, только если рядом есть «по» или время/скобки —
    # иначе «встреча в Москве» превратилась бы в смену пояса. Поэтому для городов
    # требуем «по …», а «мск» допускаем и без него.
    for m in RE_CITY.finditer(text):
        word = m.group(1)
        explicit = m.group(0).lower().startswith(("по ", "time ")) or word.lower() == "мск"
        explicit = explicit or "времени" in m.group(0).lower()
        if explicit and (zone := _city_zone(word)):
            return zone, m
    if m := RE_CODE.search(text):
        return CODES[m.group(1)], m
    return None


def resolve(name: str) -> str | None:
    """Название города, сокращение или IANA-имя -> IANA-имя пояса (для команды /tz)."""
    name = name.strip()
    if not name:
        return None
    if name.upper() in CODES:
        return CODES[name.upper()]
    if zone := _city_zone(name.lower()):
        return zone
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return None


def label(zone: str) -> str:
    return LABELS.get(zone) or zone.rsplit("/", 1)[-1].replace("_", " ")
