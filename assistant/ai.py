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

import anthropic

log = logging.getLogger(__name__)

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

SYSTEM = """Ты — помощник, который переносит планы пользователя в календарь.
На вход приходит сообщение с расписанием или фото записей (часто рукописных, \
на русском, иногда на польском или английском). Найди все события и задачи с привязкой ко времени.

Правила:
- date — дата в формате YYYY-MM-DD. Относительные даты («завтра», «в пятницу», «12-го») \
считай от текущей даты, которая указана в сообщении; выбирай ближайшую будущую дату. \
Если по записи дату определить нельзя — null. Не придумывай.
- time — время начала в формате HH:MM (24 часа). «В 3 дня» = 15:00, «полвосьмого» = 07:30. \
Если время не указано или неоднозначно — null. Не придумывай.
- title — коротко, что за событие, на языке записи; сохрани место, имя врача, адрес, \
если они есть. Без даты и времени в названии.
- for_whom — для кого событие, только из списка участников семьи; пустой список, \
если в записи не сказано, для кого оно (тогда оно для автора).
- Зачёркнутые пункты и то, что явно уже прошло или отменено, пропускай.
- В comment кратко (1 предложение) напиши, если что-то на фото не удалось прочитать; \
иначе пустая строка.
- Если событий нет — верни пустой список events."""


class AIError(Exception):
    pass


@dataclass
class AIDraft:
    title: str
    day: date | None
    at: time | None
    for_whom: list[str] = field(default_factory=list)


@dataclass
class AIResult:
    events: list[AIDraft]
    comment: str


def _schema(members: list[str]) -> dict:
    nullable = lambda: {"anyOf": [{"type": "string"}, {"type": "null"}]}  # noqa: E731
    who: dict = {"type": "string"}
    if members:
        who["enum"] = members
    return {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "date": nullable(),
                        "time": nullable(),
                        "for_whom": {"type": "array", "items": who},
                    },
                    "required": ["title", "date", "time", "for_whom"],
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
    ) -> AIResult:
        context = (
            f"Сейчас: {WEEKDAYS[now.weekday()]}, {now:%Y-%m-%d %H:%M} ({now.tzinfo}).\n"
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

        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM,
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": _schema(members)},
                },
                messages=[{"role": "user", "content": content}],
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
                title=(ev.get("title") or "").strip() or "Событие",
                day=_parse_date(ev.get("date")),
                at=_parse_time(ev.get("time")),
                for_whom=[w for w in ev.get("for_whom", []) if w in members],
            )
            for ev in data.get("events", [])
        ]
        return AIResult(events, (data.get("comment") or "").strip())
