"""Направления жизни (работа РФ, работа США, учёба, личное) и их автоопределение."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass
class Area:
    emoji: str
    name: str
    aliases: list[str] = field(default_factory=list)  # для тегов: #сша, #учеба
    words: list[str] = field(default_factory=list)    # регулярки-подсказки для автоопределения
    zones: list[str] = field(default_factory=list)    # префиксы поясов: America/ -> работа США

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


DEFAULT_AREAS = [
    Area("🇷🇺", "Работа РФ",
         aliases=["рф", "россия", "работа_рф", "ру", "ru"],
         words=[r"росси", r"рф(?!\w)", r"москв", r"мск(?!\w)", r"руб(?:л|\.)", r"₽"],
         zones=["Europe/Moscow"]),
    Area("🇺🇸", "Работа США",
         aliases=["сша", "usa", "us", "америка", "работа_сша"],
         words=[r"сша", r"америк", r"usa", r"нью[- ]?йорк", r"штат", r"доллар", r"\$"],
         zones=["America/"]),
    Area("🎓", "Учёба",
         aliases=["учеба", "учёба", "study", "универ"],
         words=[r"уч[её]б", r"пар[аыу](?!\w)", r"лекци", r"семинар", r"экзамен", r"зач[её]т", r"сесси[яи]",
                r"домашк", r"дз(?!\w)", r"эссе", r"курсов", r"диплом", r"универ", r"препод", r"лаб(?:[аыу]|ораторн)",
                r"контрольн", r"реферат", r"курс[аеуы]?(?!\w)"]),
    Area("🏠", "Личное",
         aliases=["личное", "дом", "personal", "лично"],
         words=[r"врач", r"стомат", r"маникюр", r"педикюр", r"стриж", r"парикмах", r"спорт",
                r"трениров", r"йог", r"бассейн", r"мам", r"пап[аеуы](?!\w)", r"бабушк", r"день рождения", r"др(?!\w)",
                r"подруг", r"свидан", r"массаж", r"косметолог", r"зал(?!\w)"]),
]


def norm(s: str) -> str:
    return s.lower().replace("ё", "е").replace(" ", "_").strip("#")


def from_json(items: list[dict]) -> list[Area]:
    return [Area(**x) for x in items]


def to_json(areas: list[Area]) -> list[dict]:
    return [asdict(a) for a in areas]


def find(areas: list[Area], name: str | None) -> Area | None:
    """Ищет направление по названию или тегу."""
    if not name:
        return None
    key = norm(name)
    for a in areas:
        if key == norm(a.name) or key in (norm(x) for x in a.aliases):
            return a
    return None


def detect(areas: list[Area], title: str, tags: list[str], zone: str | None) -> str | None:
    """Определяет направление: сначала явный тег, потом пояс, потом слова в названии."""
    for tag in tags:
        if a := find(areas, tag):
            return a.name
    if zone:
        for a in areas:
            if any(zone.startswith(z) for z in a.zones):
                return a.name
    text = title.lower()
    best, best_hits = None, 0
    for a in areas:
        hits = sum(1 for w in a.words if re.search(r"(?<!\w)" + w, text))
        if hits > best_hits:
            best, best_hits = a.name, hits
    return best


def emoji_for(areas: list[Area], name: str | None) -> str:
    a = find(areas, name)
    return a.emoji if a else ("📌" if name else "▫️")
