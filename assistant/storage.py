"""SQLite-хранилище событий и напоминаний.

Все моменты времени хранятся в UTC (ISO-строки), в часовой пояс пользователя
переводятся только при показе.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    title      TEXT    NOT NULL,
    starts_at  TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
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
class Event:
    id: int
    chat_id: int
    title: str
    starts_at: datetime  # UTC


@dataclass
class DueReminder:
    id: int
    event: Event


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Storage:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    # --- события ---------------------------------------------------------

    def add_event(
        self, chat_id: int, title: str, starts_at: datetime, offsets: list[timedelta], now: datetime
    ) -> Event:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO events (chat_id, title, starts_at, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, title, _iso(starts_at), _iso(now)),
            )
            event_id = cur.lastrowid
            for off in offsets:
                remind_at = starts_at - off
                if remind_at > now:  # прошедшие напоминания не создаём
                    self.db.execute(
                        "INSERT INTO reminders (event_id, remind_at) VALUES (?, ?)",
                        (event_id, _iso(remind_at)),
                    )
        return Event(event_id, chat_id, title, starts_at.astimezone(timezone.utc))

    def get_event(self, chat_id: int, event_id: int) -> Event | None:
        row = self.db.execute(
            "SELECT * FROM events WHERE id = ? AND chat_id = ?", (event_id, chat_id)
        ).fetchone()
        return self._event(row) if row else None

    def delete_event(self, chat_id: int, event_id: int) -> bool:
        with self.db:
            cur = self.db.execute(
                "DELETE FROM events WHERE id = ? AND chat_id = ?", (event_id, chat_id)
            )
        return cur.rowcount > 0

    def upcoming(self, chat_id: int, since: datetime, until: datetime | None = None) -> list[Event]:
        sql = "SELECT * FROM events WHERE chat_id = ? AND starts_at >= ?"
        args: list = [chat_id, _iso(since)]
        if until is not None:
            sql += " AND starts_at < ?"
            args.append(_iso(until))
        sql += " ORDER BY starts_at"
        return [self._event(r) for r in self.db.execute(sql, args)]

    def purge_past(self, before: datetime) -> int:
        with self.db:
            cur = self.db.execute("DELETE FROM events WHERE starts_at < ?", (_iso(before),))
        return cur.rowcount

    # --- напоминания -----------------------------------------------------

    def due_reminders(self, now: datetime) -> list[DueReminder]:
        rows = self.db.execute(
            """SELECT r.id AS rid, e.* FROM reminders r JOIN events e ON e.id = r.event_id
               WHERE r.sent = 0 AND r.remind_at <= ? ORDER BY r.remind_at""",
            (_iso(now),),
        ).fetchall()
        return [DueReminder(r["rid"], self._event(r)) for r in rows]

    def mark_sent(self, reminder_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))

    def add_reminder(self, chat_id: int, event_id: int, remind_at: datetime) -> bool:
        if not self.get_event(chat_id, event_id):
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO reminders (event_id, remind_at) VALUES (?, ?)",
                (event_id, _iso(remind_at)),
            )
        return True

    # --- настройки -------------------------------------------------------

    def get_setting(self, chat_id: int, key: str) -> str | None:
        row = self.db.execute(
            "SELECT value FROM settings WHERE chat_id = ? AND key = ?", (chat_id, key)
        ).fetchone()
        return row["value"] if row else None

    def set_setting(self, chat_id: int, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO settings (chat_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value",
                (chat_id, key, value),
            )

    def chats(self) -> list[int]:
        rows = self.db.execute(
            "SELECT DISTINCT chat_id FROM settings UNION SELECT DISTINCT chat_id FROM events"
        )
        return [r[0] for r in rows]

    @staticmethod
    def _event(row: sqlite3.Row) -> Event:
        return Event(row["id"], row["chat_id"], row["title"], _dt(row["starts_at"]))
