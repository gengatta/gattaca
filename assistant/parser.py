"""Разбор событий и задач из обычного текста на русском — без нейросети.

Примеры, которые понимает парсер:
    25.10 18:30 Стоматолог
    завтра в 10:00 созвон с Анной
    в пятницу 15:00 маникюр
    через 2 часа забрать посылку
    созвон в 10:00 по Нью-Йорку            (время в другом поясе)
    сдать эссе до пятницы                  (задача с дедлайном)
    #учеба отчёт по лабе к 25.10           (тег направления)
    задача: разобрать почту                (задача без срока — во «входящие»)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import zones


class ParseError(ValueError):
    """Текст не удалось превратить в событие."""


@dataclass
class ParsedEvent:
    title: str
    starts_at: datetime  # aware, в часовом поясе пользователя


# Дни недели во всех нужных падежах: «в пятницу», «до пятницы», «к пятнице»
WEEKDAYS = {
    0: r"понедельник[ауе]?|пн",
    1: r"вторник[ауе]?|вт",
    2: r"сред[ауые]|ср",
    3: r"четверг[ауе]?|чт",
    4: r"пятниц[ауые]|пт",
    5: r"суббот[ауые]|сб",
    6: r"воскресень[еяю]|вс",
}

RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}

UNITS = {
    "мин": timedelta(minutes=1),
    "час": timedelta(hours=1),
    "дн": timedelta(days=1),
    "ден": timedelta(days=1),
    "день": timedelta(days=1),
    "недел": timedelta(weeks=1),
}

# \b в Python работает с кириллицей, но «в» и «через» нужно отделять явно
_B = r"(?<![\w])"
_E = r"(?![\w])"
# «до пятницы», «к 25.10», «дедлайн 18:00» — признак задачи с дедлайном
_DL = r"(?:(до|к|дедлайн:?|deadline:?|срок:?)\s+)?"

RE_IN = re.compile(
    _B + r"через\s+(?:(\d+)\s+)?(минут[уы]?|мин|час(?:а|ов)?|дн(?:я|ей)|день|недел[юиь])" + _E,
    re.IGNORECASE,
)
RE_DATE = re.compile(_B + _DL + r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?" + _E, re.IGNORECASE)
RE_ISO = re.compile(_B + _DL + r"(\d{4})-(\d{2})-(\d{2})" + _E, re.IGNORECASE)
RE_REL = re.compile(_B + _DL + r"(" + "|".join(RELATIVE_DAYS) + r")" + _E, re.IGNORECASE)
RE_WEEKDAY = re.compile(
    _B + r"(?:(до|к|дедлайн:?|deadline:?|срок:?)\s+|во?\s+)?(" + "|".join(WEEKDAYS.values()) + r")" + _E,
    re.IGNORECASE,
)
RE_TIME = re.compile(_B + r"(?:(до|к)\s+|в\s+)?([01]?\d|2[0-3])[:](\d{2})" + _E, re.IGNORECASE)
RE_TAG = re.compile(r"(?<![\w#])#([\w]+)")
RE_TASK_PREFIX = re.compile(r"^\s*(?:задача|задачу|todo|task|сделать|дедлайн|deadline)\s*[:\-—]\s*", re.IGNORECASE)
TASK_TAGS = {"задача", "задачи", "todo", "task", "дедлайн"}


@dataclass
class Draft:
    """Черновик события/задачи: дата или время могут быть ещё неизвестны."""

    title: str
    day: date | None = None
    at: time | None = None
    source: str | None = None  # откуда взялась дата: exact / no_year / relative / weekday
    starts_at: datetime | None = None  # уже готовый момент («через 2 часа»)
    kind: str = "event"  # event — встреча в конкретное время, task — задача с дедлайном или без
    tz: str | None = None  # пояс, в котором указано время («по Нью-Йорку»), None — свой
    tags: list[str] = field(default_factory=list)

    @property
    def found_anything(self) -> bool:
        return bool(self.starts_at or self.day or self.at)

    @property
    def missing(self) -> str | None:
        """Чего не хватает, чтобы сохранить: 'date', 'time' или None.

        Задаче хватает одной даты (дедлайн — конец дня), а задача совсем без срока
        уходит во «входящие». Встрече нужны и день, и время (только время — значит,
        сегодня или завтра).
        """
        if self.kind == "task" or self.starts_at or self.at:
            return None
        if self.day:
            return "time"
        return "date"


def extract(text: str, now: datetime) -> Draft:
    """Достаёт дату/время/пояс/теги из текста; остальное — название (может быть пустым).

    `now` должен быть aware datetime в часовом поясе пользователя.
    """
    rest = text.strip()
    draft = Draft("")

    # 0. Теги и явные пометки задачи
    draft.tags = [t.lower() for t in RE_TAG.findall(rest)]
    rest = RE_TAG.sub(" ", rest)
    if any(t in TASK_TAGS for t in draft.tags):
        draft.kind = "task"
        draft.tags = [t for t in draft.tags if t not in TASK_TAGS]
    if m := RE_TASK_PREFIX.match(rest):
        draft.kind = "task"
        rest = rest[m.end():]

    # 1. Часовой пояс: «по Нью-Йорку» — дальше считаем время в нём
    if found := zones.find_zone(rest):
        zone, m = found
        if zone != str(now.tzinfo):
            draft.tz = zone
            now = now.astimezone(ZoneInfo(zone))
        rest = _cut(rest, m)

    # 2. «через N часов» — сразу готовый момент
    if m := RE_IN.search(rest):
        amount = int(m.group(1) or 1)
        draft.starts_at = (now + amount * _unit(m.group(2))).replace(second=0, microsecond=0)
        draft.title = _clean(_cut(rest, m))
        return draft

    # 3. Дата
    if m := RE_ISO.search(rest):
        draft.day = _safe_date(int(m.group(2)), int(m.group(3)), int(m.group(4)))
        draft.source = "exact"
    elif m := RE_DATE.search(rest):
        d, mo, y = int(m.group(2)), int(m.group(3)), m.group(4)
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
            draft.source = "exact"
        else:
            year = now.year
            draft.source = "no_year"
        draft.day = _safe_date(year, mo, d)
    elif m := RE_REL.search(rest):
        draft.day = now.date() + timedelta(days=RELATIVE_DAYS[m.group(2).lower()])
        draft.source = "relative"
    elif m := RE_WEEKDAY.search(rest):
        word = m.group(2).lower()
        target = next(i for i, pat in WEEKDAYS.items() if re.fullmatch(pat, word))
        draft.day = now.date() + timedelta(days=(target - now.weekday()) % 7)
        draft.source = "weekday"
    if m:
        if m.group(1):
            draft.kind = "task"
        rest = _cut(rest, m)

    # 4. Время
    if m := RE_TIME.search(rest):
        draft.at = time(int(m.group(2)), int(m.group(3)))
        if m.group(1):
            draft.kind = "task"
        rest = _cut(rest, m)

    draft.title = _clean(rest)
    return draft


def looks_like_schedule(text: str) -> bool:
    """Несколько дат или несколько времён в одном тексте — это расписание, а не одно событие."""
    dates = len(RE_ISO.findall(text)) or len(RE_DATE.findall(text))
    dates += sum(len(r.findall(text)) for r in (RE_REL, RE_WEEKDAY, RE_IN))
    return dates > 1 or len(RE_TIME.findall(text)) > 1


DEADLINE_TIME = time(23, 59)


def finalize(draft: Draft, now: datetime, default_time: time | None = None) -> datetime | None:
    """Превращает черновик в конкретный момент (aware).

    None — задача без срока («входящие»). ParseError, если данных не хватает
    или время уже прошло.
    """
    if draft.starts_at:
        return draft.starts_at
    if draft.tz:
        now = now.astimezone(ZoneInfo(draft.tz))
    tz = now.tzinfo
    day, at = draft.day, draft.at
    if draft.kind == "task":
        if day is None and at is None:
            return None
        at = at or DEADLINE_TIME  # дедлайн без времени — до конца дня
    else:
        at = at or default_time
        if at is None:
            raise ParseError("Не указано время")
    if day is None:
        if draft.at is None:
            raise ParseError("Не нашла дату или время")
        # Только время: сегодня, а если уже прошло — завтра
        day = now.date()
        if datetime.combine(day, at, tz) <= now:
            day += timedelta(days=1)

    starts_at = datetime.combine(day, at, tz)
    if starts_at <= now:
        if draft.source == "no_year":
            # «25.01» в октябре — значит, в следующем году
            starts_at = _safe_datetime(starts_at, now.year + 1)
        elif draft.source == "weekday":
            # «в пятницу 9:00», а сегодня пятница и 9:00 уже прошло — следующая неделя
            starts_at += timedelta(weeks=1)
        else:
            raise ParseError("Это время уже прошло")
    return starts_at


def is_all_day(draft: Draft) -> bool:
    """Задача с дедлайном «до такого-то дня» без конкретного времени."""
    return draft.kind == "task" and draft.at is None and draft.starts_at is None


def parse_event(text: str, now: datetime, default_time: time = time(9, 0)) -> ParsedEvent:
    """Одно событие из текста целиком; без времени ставит default_time."""
    if not text.strip():
        raise ParseError("Пустое сообщение")
    draft = extract(text, now)
    if not draft.found_anything:
        raise ParseError("Не нашла дату или время")
    starts_at = finalize(draft, now, default_time)
    if not draft.title:
        raise ParseError("Не указано, что за событие")
    return ParsedEvent(draft.title, starts_at)


def _cut(text: str, m: re.Match) -> str:
    return text[: m.start()] + " " + text[m.end():]


def _unit(word: str) -> timedelta:
    word = word.lower()
    for prefix, delta in UNITS.items():
        if word.startswith(prefix):
            return delta
    raise ParseError(f"Не понимаю единицу времени «{word}»")


def _safe_date(y: int, m: int, d: int) -> date:
    try:
        return date(y, m, d)
    except ValueError as e:
        raise ParseError(f"Некорректная дата: {d:02}.{m:02}.{y}") from e


def _safe_datetime(dt: datetime, year: int) -> datetime:
    try:
        return dt.replace(year=year)
    except ValueError as e:  # 29.02
        raise ParseError("Такой даты в следующем году нет") from e


def _clean(rest: str) -> str:
    title = re.sub(r"\s+", " ", rest).strip(" ,.-—:()")
    return title[:1].upper() + title[1:]


def parse_offsets(spec: str) -> list[timedelta]:
    """'1d,2h,30m,0m' -> [timedelta(days=1), ...]. Используется для настроек напоминаний."""
    result = []
    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*([dhmдчм])", part)
        if not m:
            raise ParseError(f"Не понимаю интервал «{part}» (пример: 1d, 2h, 30m)")
        n, u = int(m.group(1)), m.group(2)
        result.append(
            {"d": timedelta(days=n), "д": timedelta(days=n),
             "h": timedelta(hours=n), "ч": timedelta(hours=n),
             "m": timedelta(minutes=n), "м": timedelta(minutes=n)}[u]
        )
    return sorted(set(result), reverse=True)


def format_offset(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes == 0:
        return "в момент начала"
    if minutes % 1440 == 0:
        n = minutes // 1440
        return f"за {n} {plural(n, 'день', 'дня', 'дней')}"
    if minutes % 60 == 0:
        n = minutes // 60
        return f"за {n} {plural(n, 'час', 'часа', 'часов')}"
    return f"за {minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
