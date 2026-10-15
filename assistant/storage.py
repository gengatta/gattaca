"""SQLite-хранилище: пользователи и семья, события и задачи, напоминания, настройки.

Все моменты времени хранятся в UTC (ISO-строки), в часовой пояс пользователя
переводятся только при показе.

Событие (или задача) принадлежит создателю, а напоминания получают его участники
(event_members) — так мама может поставить событие Катаржине и наоборот.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS families (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invite_code TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS users (
    chat_id   INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL,
    family_id INTEGER REFERENCES families(id)
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER NOT NULL,
    title      TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'event',  -- event | task
    area       TEXT,                              -- направление: «Работа США», «Учёба»…
    starts_at  TEXT,                              -- NULL у задач без срока
    all_day    INTEGER NOT NULL DEFAULT 0,        -- дедлайн «до конца дня», без времени
    src_tz     TEXT,                              -- пояс, в котором время было указано
    done_at    TEXT,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS event_members (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    chat_id  INTEGER NOT NULL,
    PRIMARY KEY (event_id, chat_id)
);
CREATE TABLE IF NOT EXISTS reminders (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    chat_id   INTEGER NOT NULL,
    remind_at TEXT    NOT NULL,
    sent      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, remind_at);
CREATE TABLE IF NOT EXISTS settings (
    chat_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    PRIMARY KEY (chat_id, key)
);
"""


@dataclass
class User:
    chat_id: int
    name: str
    family_id: int | None


@dataclass
class Event:
    id: int
    creator_id: int
    title: str
    starts_at: datetime | None  # UTC; None — задача без срока
    members: list[int] = field(default_factory=list)
    kind: str = "event"
    area: str | None = None
    all_day: bool = False
    src_tz: str | None = None
    done_at: datetime | None = None

    @property
    def is_task(self) -> bool:
        return self.kind == "task"


@dataclass
class DueReminder:
    id: int
    chat_id: int
    event: Event


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        has_events = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        if has_events and version < SCHEMA_VERSION:
            raise RuntimeError(
                f"База {path} от старой версии бота. Удалите файл — бот создаст новую."
            )
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # --- пользователи и семья --------------------------------------------

    def upsert_user(self, chat_id: int, name: str) -> User:
        """Регистрирует пользователя; имя не перезаписываем, если его задали через /name."""
        with self.db:
            self.db.execute(
                "INSERT INTO users (chat_id, name) VALUES (?, ?) ON CONFLICT(chat_id) DO NOTHING",
                (chat_id, name),
            )
        return self.get_user(chat_id)

    def get_user(self, chat_id: int) -> User | None:
        row = self.db.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        return User(row["chat_id"], row["name"], row["family_id"]) if row else None

    def set_name(self, chat_id: int, name: str) -> None:
        with self.db:
            self.db.execute("UPDATE users SET name = ? WHERE chat_id = ?", (name, chat_id))

    def family_members(self, chat_id: int) -> list[User]:
        """Все члены семьи пользователя, включая его самого (или только он, если семьи нет)."""
        user = self.get_user(chat_id)
        if user is None:
            return []
        if user.family_id is None:
            return [user]
        rows = self.db.execute(
            "SELECT * FROM users WHERE family_id = ? ORDER BY chat_id", (user.family_id,)
        )
        return [User(r["chat_id"], r["name"], r["family_id"]) for r in rows]

    def new_invite(self, chat_id: int) -> str:
        """Создаёт семью (если её ещё нет) и новый одноразовый код приглашения."""
        code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        user = self.get_user(chat_id)
        with self.db:
            if user.family_id is None:
                cur = self.db.execute("INSERT INTO families (invite_code) VALUES (?)", (code,))
                self.db.execute(
                    "UPDATE users SET family_id = ? WHERE chat_id = ?", (cur.lastrowid, chat_id)
                )
            else:
                self.db.execute(
                    "UPDATE families SET invite_code = ? WHERE id = ?", (code, user.family_id)
                )
        return code

    def join_family(self, chat_id: int, code: str) -> bool:
        row = self.db.execute("SELECT id FROM families WHERE invite_code = ?", (code,)).fetchone()
        if not row:
            return False
        with self.db:
            self.db.execute(
                "UPDATE users SET family_id = ? WHERE chat_id = ?", (row["id"], chat_id)
            )
            # код одноразовый
            self.db.execute("UPDATE families SET invite_code = NULL WHERE id = ?", (row["id"],))
        return True

    def leave_family(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE users SET family_id = NULL WHERE chat_id = ?", (chat_id,))

    def is_family_member(self, chat_id: int) -> bool:
        user = self.get_user(chat_id)
        return bool(user and user.family_id is not None)

    # --- события и задачи ------------------------------------------------

    def add_event(
        self,
        creator_id: int,
        title: str,
        starts_at: datetime | None,
        now: datetime,
        kind: str = "event",
        area: str | None = None,
        all_day: bool = False,
        src_tz: str | None = None,
    ) -> Event:
        with self.db:
            cur = self.db.execute(
                """INSERT INTO events (creator_id, title, kind, area, starts_at, all_day, src_tz, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (creator_id, title, kind, area, _iso(starts_at) if starts_at else None,
                 int(all_day), src_tz, _iso(now)),
            )
        return self.get_event(cur.lastrowid)

    def add_member(self, event_id: int, chat_id: int, remind_at: list[datetime]) -> bool:
        """Добавляет участника и его напоминания. False, если он уже участник."""
        event = self.get_event(event_id)
        if event is None or chat_id in event.members:
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO event_members (event_id, chat_id) VALUES (?, ?)", (event_id, chat_id)
            )
            self._insert_reminders(event_id, chat_id, remind_at)
        return True

    def add_reminders(self, event_id: int, chat_id: int, remind_at: list[datetime]) -> None:
        with self.db:
            self._insert_reminders(event_id, chat_id, remind_at)

    def _insert_reminders(self, event_id: int, chat_id: int, remind_at: list[datetime]) -> None:
        self.db.executemany(
            "INSERT INTO reminders (event_id, chat_id, remind_at) VALUES (?, ?, ?)",
            [(event_id, chat_id, _iso(t)) for t in remind_at],
        )

    def get_event(self, event_id: int) -> Event | None:
        row = self.db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def can_edit(self, chat_id: int, event: Event | None) -> bool:
        return event is not None and (chat_id in event.members or chat_id == event.creator_id)

    def delete_event(self, chat_id: int, event_id: int) -> Event | None:
        """Удаляет событие, если пользователь его участник или создатель. Возвращает удалённое."""
        event = self.get_event(event_id)
        if not self.can_edit(chat_id, event):
            return None
        with self.db:
            self.db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        return event

    def mark_done(self, chat_id: int, event_id: int, now: datetime) -> Event | None:
        """Отмечает задачу выполненной и снимает оставшиеся напоминания."""
        event = self.get_event(event_id)
        if not self.can_edit(chat_id, event) or event.done_at:
            return None
        with self.db:
            self.db.execute("UPDATE events SET done_at = ? WHERE id = ?", (_iso(now), event_id))
            self.db.execute("DELETE FROM reminders WHERE event_id = ? AND sent = 0", (event_id,))
        return self.get_event(event_id)

    def set_area(self, chat_id: int, event_id: int, area: str | None) -> bool:
        if not self.can_edit(chat_id, self.get_event(event_id)):
            return False
        with self.db:
            self.db.execute("UPDATE events SET area = ? WHERE id = ?", (area, event_id))
        return True

    def schedule(
        self, chat_id: int, event_id: int, starts_at: datetime, all_day: bool, src_tz: str | None
    ) -> Event | None:
        """Назначает срок задаче из «входящих» (старые напоминания снимаются)."""
        event = self.get_event(event_id)
        if not self.can_edit(chat_id, event):
            return None
        with self.db:
            self.db.execute(
                "UPDATE events SET starts_at = ?, all_day = ?, src_tz = ? WHERE id = ?",
                (_iso(starts_at), int(all_day), src_tz, event_id),
            )
            self.db.execute("DELETE FROM reminders WHERE event_id = ? AND sent = 0", (event_id,))
        return self.get_event(event_id)

    def _select(self, chat_id: int, where: str, args: list, order: str = "e.starts_at") -> list[Event]:
        sql = f"""SELECT e.* FROM events e JOIN event_members m ON m.event_id = e.id
                  WHERE m.chat_id = ? AND {where} ORDER BY {order}"""
        return [self._event(r) for r in self.db.execute(sql, [chat_id, *args]).fetchall()]

    def upcoming(self, chat_id: int, since: datetime, until: datetime | None = None) -> list[Event]:
        """Невыполненные события и задачи со сроком в [since, until)."""
        where, args = "e.done_at IS NULL AND e.starts_at >= ?", [_iso(since)]
        if until is not None:
            where += " AND e.starts_at < ?"
            args.append(_iso(until))
        return self._select(chat_id, where, args)

    def overdue(self, chat_id: int, now: datetime) -> list[Event]:
        """Невыполненные задачи, дедлайн которых уже прошёл."""
        return self._select(
            chat_id, "e.kind = 'task' AND e.done_at IS NULL AND e.starts_at < ?", [_iso(now)]
        )

    def inbox(self, chat_id: int) -> list[Event]:
        """Задачи без срока."""
        return self._select(
            chat_id, "e.kind = 'task' AND e.done_at IS NULL AND e.starts_at IS NULL", [], "e.id"
        )

    def done_since(self, chat_id: int, since: datetime) -> list[Event]:
        return self._select(chat_id, "e.done_at >= ?", [_iso(since)], "e.done_at")

    def purge_past(self, before: datetime) -> int:
        """Чистит прошедшие встречи и давно выполненные задачи; невыполненные задачи не трогает."""
        with self.db:
            cur = self.db.execute(
                """DELETE FROM events WHERE (kind = 'event' AND starts_at < ?)
                   OR (done_at IS NOT NULL AND done_at < ?)""",
                (_iso(before), _iso(before)),
            )
        return cur.rowcount

    # --- напоминания -----------------------------------------------------

    def due_reminders(self, now: datetime) -> list[DueReminder]:
        rows = self.db.execute(
            """SELECT r.id AS rid, r.chat_id AS rchat, e.* FROM reminders r
               JOIN events e ON e.id = r.event_id
               WHERE r.sent = 0 AND r.remind_at <= ? AND e.done_at IS NULL
               ORDER BY r.remind_at""",
            (_iso(now),),
        ).fetchall()
        return [DueReminder(r["rid"], r["rchat"], self._event(r)) for r in rows]

    def mark_sent(self, reminder_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))

    def snooze(self, chat_id: int, event_id: int, remind_at: datetime) -> bool:
        event = self.get_event(event_id)
        if event is None or chat_id not in event.members:
            return False
        self.add_reminders(event_id, chat_id, [remind_at])
        return True

    # --- настройки -------------------------------------------------------

    def get_setting(self, chat_id: int, key: str) -> str | None:
        row = self.db.execute(
            "SELECT value FROM settings WHERE chat_id = ? AND key = ?", (chat_id, key)
        ).fetchone()
        return row["value"] if row else None

    def set_setting(self, chat_id: int, key: str, value: str | None) -> None:
        with self.db:
            if value is None:
                self.db.execute(
                    "DELETE FROM settings WHERE chat_id = ? AND key = ?", (chat_id, key)
                )
            else:
                self.db.execute(
                    "INSERT INTO settings (chat_id, key, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value",
                    (chat_id, key, value),
                )

    def chats(self) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT chat_id FROM users")]

    def _event(self, row: sqlite3.Row) -> Event:
        members = [
            r[0]
            for r in self.db.execute(
                "SELECT chat_id FROM event_members WHERE event_id = ? ORDER BY chat_id",
                (row["id"],),
            )
        ]
        parse = lambda s: datetime.fromisoformat(s) if s else None  # noqa: E731
        return Event(
            id=row["id"], creator_id=row["creator_id"], title=row["title"],
            starts_at=parse(row["starts_at"]), members=members, kind=row["kind"],
            area=row["area"], all_day=bool(row["all_day"]), src_tz=row["src_tz"],
            done_at=parse(row["done_at"]),
        )
