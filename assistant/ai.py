"""Распознавание расписаний через Claude: свободный текст и фото рукописных заметок.

Модель возвращает список черновиков; если в записи нет даты или времени,
поле остаётся пустым — бот потом уточнит у пользователя, а не выдумывает.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anthropic

log = logging.getLogger(__name__)

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

SYSTEM = """Ты — помощник, который переносит планы пользователя в календарь и список задач.
На вход приходит сообщение с расписанием, списком дел или фото записей (часто рукописных, \
на русском, иногда на английском). Найди все встречи и задачи.

Правила:
- kind — "event", если это встреча/занятие/звонок в конкретное время; "task", если это дело, \
которое надо сделать (сдать, отправить, подготовить, купить), в том числе к сроку («до пятницы»).
- date — дата в формате YYYY-MM-DD (для задачи — дедлайн). Относительные даты («завтра», \
«в пятницу», «12-го») считай от текущей даты из сообщения; выбирай ближайшую будущую. \
Если дату определить нельзя — null. Не придумывай.
- time — время в формате HH:MM (24 часа). «В 3 дня» = 15:00, «полвосьмого» = 07:30. \
Если время не указано или неоднозначно — null. Не придумывай.
- tz — IANA-пояс, если время явно указано по другому городу («10:00 по Нью-Йорку», «9am PT», \
«по Москве»): America/New_York, America/Los_Angeles, Europe/Moscow и т.п. Иначе null.
- title — коротко, что за дело, на языке записи; сохрани место, имя, адрес, \
если они есть. Без даты и времени в названии.
- area — направление из списка направлений пользователя, к которому относится запись \
(по смыслу: работа в России, работа в США, учёба, личное). null, если непонятно.
- for_whom — для кого запись, только из списка участников семьи; пустой список, \
если не сказано, для кого (тогда для автора).
- Зачёркнутые пункты и то, что явно уже прошло или отменено, пропускай.
- В comment кратко (1 предложение) напиши, если что-то на фото не удалось прочитать; \
иначе пустая строка.
- Если ничего нет — верни пустой список events."""


class AIError(Exception):
    pass


@dataclass
class AIDraft:
    title: str
    day: date | None
    at: time | None
    for_whom: list[str] = field(default_factory=list)
    kind: str = "event"
    tz: str | None = None
    area: str | None = None


@dataclass
class AIResult:
    events: list[AIDraft]
    comment: str


def _schema(members: list[str], areas: list[str]) -> dict:
    nullable = lambda: {"anyOf": [{"type": "string"}, {"type": "null"}]}  # noqa: E731
    who: dict = {"type": "string"}
    if members:
        who["enum"] = members
    area: dict = nullable()
    if areas:
        area = {"anyOf": [{"type": "string", "enum": areas}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "kind": {"type": "string", "enum": ["event", "task"]},
                        "date": nullable(),
                        "time": nullable(),
                        "tz": nullable(),
                        "area": area,
                        "for_whom": {"type": "array", "items": who},
                    },
                    "required": ["title", "kind", "date", "time", "tz", "area", "for_whom"],
                    "additionalProperties": False,
                },
            },
            "comment": {"type": "string"},
        },
        "required": ["events", "comment"],
        "additionalProperties": False,
    }


def _parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _parse_time(s: str | None) -> time | None:
    try:
        return datetime.strptime(s, "%H:%M").time() if s else None
    except ValueError:
        return None


def _parse_tz(s: str | None) -> str | None:
    try:
        return str(ZoneInfo(s)) if s else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


class Extractor:
    def __init__(self, model: str):
        self.client = anthropic.AsyncAnthropic()
        self.model = model

    async def extract(
        self,
        now: datetime,
        members: list[str],
        author: str = "",
        text: str | None = None,
        image: bytes | None = None,
        image_type: str = "image/jpeg",
        areas: list[str] | None = None,
    ) -> AIResult:
        areas = areas or []
        context = (
            f"Сейчас: {WEEKDAYS[now.weekday()]}, {now:%Y-%m-%d %H:%M}, "
            f"часовой пояс пользователя {now.tzinfo}.\n"
            f"Направления пользователя: {', '.join(areas) if areas else 'не заданы'}.\n"
            f"Участники семьи: {', '.join(members) if members else 'только автор'}."
        )
        if members and author:
            context += f"\nАвтор сообщения: {author} («мне», «у меня» — это {author})."
        content: list[dict] = []
        if image is not None:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image_type,
                    "data": base64.standard_b64encode(image).decode(),
                },
            })
            context += "\nНа фото — записи пользователя."
        if text:
            context += f"\n\nСообщение пользователя:\n{text}"
        content.append({"type": "text", "text": context})

        request = dict(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": _schema(members, areas)},
            },
            messages=[{"role": "user", "content": content}],
        )
        try:
            if "haiku" in self.model:
                # У Haiku нет серверного запасного варианта при отказе — обычный запрос
                response = await self.client.messages.create(**request)
            else:
                response = await self.client.beta.messages.create(
                    **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                )
        except anthropic.APIStatusError as e:
            log.exception("Claude API error")
            raise AIError(f"Ошибка сервиса распознавания ({e.status_code})") from e
        except anthropic.APIConnectionError as e:
            raise AIError("Сервис распознавания недоступен, попробуй ещё раз позже") from e

        if response.stop_reason == "refusal":
            raise AIError("Не получилось разобрать это сообщение")
        if response.stop_reason == "max_tokens":
            raise AIError("Слишком длинное расписание — пришли его частями")

        raw = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AIError("Не получилось разобрать ответ, попробуй ещё раз") from e

        events = [
            AIDraft(
                title=(ev.get("title") or "").strip() or "Без названия",
                day=_parse_date(ev.get("date")),
                at=_parse_time(ev.get("time")),
                for_whom=[w for w in ev.get("for_whom", []) if w in members],
                kind="task" if ev.get("kind") == "task" else "event",
                tz=_parse_tz(ev.get("tz")),
                area=ev.get("area") if ev.get("area") in areas else None,
            )
            for ev in data.get("events", [])
        ]
        return AIResult(events, (data.get("comment") or "").strip())
