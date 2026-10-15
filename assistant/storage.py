"""SQLite-хранилище: пользователи и семья, события, напоминания, настройки.

Все моменты времени хранятся в UTC (ISO-строки), в часовой пояс пользователя
переводятся только при показе.

Событие принадлежит создателю, а напоминания получают его участники
(event_members) — так мама может поставить событие Катаржине и наоборот.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    starts_at  TEXT    NOT NULL,
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
    starts_at: datetime  # UTC
    members: list[int] = field(default_factory=list)


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
        self.db.executescript(SCHEMA)

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

    # --- события ---------------------------------------------------------

    def add_event(self, creator_id: int, title: str, starts_at: datetime, now: datetime) -> Event:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO events (creator_id, title, starts_at, created_at) VALUES (?, ?, ?, ?)",
                (creator_id, title, _iso(starts_at), _iso(now)),
            )
        return Event(cur.lastrowid, creator_id, title, starts_at.astimezone(timezone.utc))

    def add_member(
        self, event_id: int, chat_id: int, offsets: list[timedelta], now: datetime
    ) -> bool:
        """Добавляет участника и его напоминания. False, если он уже участник."""
        event = self.get_event(event_id)
        if event is None or chat_id in event.members:
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO event_members (event_id, chat_id) VALUES (?, ?)", (event_id, chat_id)
            )
            for off in offsets:
                remind_at = event.starts_at - off
                if remind_at > now:  # прошедшие напоминания не создаём
                    self.db.execute(
                        "INSERT INTO reminders (event_id, chat_id, remind_at) VALUES (?, ?, ?)",
                        (event_id, chat_id, _iso(remind_at)),
                    )
        return True

    def get_event(self, event_id: int) -> Event | None:
        row = self.db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def delete_event(self, chat_id: int, event_id: int) -> Event | None:
        """Удаляет событие, если пользователь его участник или создатель. Возвращает удалённое."""
        event = self.get_event(event_id)
        if event is None or (chat_id not in event.members and chat_id != event.creator_id):
            return None
        with self.db:
            self.db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        return event

    def upcoming(self, chat_id: int, since: datetime, until: datetime | None = None) -> list[Event]:
        sql = """SELECT e.* FROM events e JOIN event_members m ON m.event_id = e.id
                 WHERE m.chat_id = ? AND e.starts_at >= ?"""
        args: list = [chat_id, _iso(since)]
        if until is not None:
            sql += " AND e.starts_at < ?"
            args.append(_iso(until))
        sql += " ORDER BY e.starts_at"
        return [self._event(r) for r in self.db.execute(sql, args).fetchall()]

    def purge_past(self, before: datetime) -> int:
        with self.db:
            cur = self.db.execute("DELETE FROM events WHERE starts_at < ?", (_iso(before),))
        return cur.rowcount

    # --- напоминания -----------------------------------------------------

    def due_reminders(self, now: datetime) -> list[DueReminder]:
        rows = self.db.execute(
            """SELECT r.id AS rid, r.chat_id AS rchat, e.* FROM reminders r
               JOIN events e ON e.id = r.event_id
               WHERE r.sent = 0 AND r.remind_at <= ? ORDER BY r.remind_at""",
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
        with self.db:
            self.db.execute(
                "INSERT INTO reminders (event_id, chat_id, remind_at) VALUES (?, ?, ?)",
                (event_id, chat_id, _iso(remind_at)),
            )
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
        return Event(
            row["id"], row["creator_id"], row["title"],
            datetime.fromisoformat(row["starts_at"]), members,
        )
