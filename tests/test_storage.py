from datetime import datetime, timedelta, timezone

import pytest

from assistant.storage import Storage

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)


def test_event_lifecycle(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    s.upsert_user(1, "Катаржина")
    start = NOW + timedelta(hours=5)
    ev = s.add_event(1, "Стоматолог", start, NOW, area="Личное")
    assert s.add_member(ev.id, 1, [start - timedelta(hours=1), start])
    assert not s.add_member(ev.id, 1, [])  # повторно не добавляется

    assert s.due_reminders(NOW) == []
    due = s.due_reminders(start - timedelta(hours=1))
    assert [(d.chat_id, d.event.title, d.event.area) for d in due] == [(1, "Стоматолог", "Личное")]
    s.mark_sent(due[0].id)
    assert s.due_reminders(start - timedelta(minutes=30)) == []

    assert [e.id for e in s.upcoming(1, NOW)] == [ev.id]
    assert s.upcoming(2, NOW) == []  # чужой чат
    assert s.delete_event(2, ev.id) is None
    assert s.delete_event(1, ev.id).title == "Стоматолог"
    assert s.due_reminders(start) == []  # напоминания удалились каскадом


def test_tasks_inbox_done(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    s.upsert_user(1, "Катаржина")
    deadline = NOW + timedelta(days=2)
    task = s.add_event(1, "Эссе", deadline, NOW, kind="task", all_day=True)
    s.add_member(task.id, 1, [NOW + timedelta(days=1)])
    inbox = s.add_event(1, "Разобрать почту", None, NOW, kind="task")
    s.add_member(inbox.id, 1, [])
    late = s.add_event(1, "Отчёт", NOW - timedelta(days=1), NOW - timedelta(days=3), kind="task")
    s.add_member(late.id, 1, [])

    assert [e.title for e in s.inbox(1)] == ["Разобрать почту"]
    assert [e.title for e in s.overdue(1, NOW)] == ["Отчёт"]
    assert [e.title for e in s.upcoming(1, NOW)] == ["Эссе"]
    assert s.get_event(task.id).all_day

    assert s.mark_done(1, task.id, NOW).done_at == NOW
    assert s.mark_done(1, task.id, NOW) is None  # повторно нельзя
    assert s.due_reminders(NOW + timedelta(days=3)) == []  # напоминания сняты
    assert s.upcoming(1, NOW) == []
    assert [e.title for e in s.done_since(1, NOW - timedelta(days=7))] == ["Эссе"]

    planned = s.schedule(1, inbox.id, NOW + timedelta(days=5), True, None)
    assert planned.starts_at == NOW + timedelta(days=5) and s.inbox(1) == []

    assert s.set_area(1, late.id, "Учёба") and s.get_event(late.id).area == "Учёба"

    # чистка: старые выполненные удаляются, невыполненные просроченные — нет
    s.purge_past(NOW + timedelta(days=30))
    assert s.get_event(task.id) is None and s.get_event(late.id) is not None


def test_family_and_shared_reminders(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    s.upsert_user(1, "Катаржина")
    s.upsert_user(2, "Ольга")
    s.upsert_user(3, "Чужой")
    assert not s.is_family_member(2)

    code = s.new_invite(1)
    assert s.join_family(2, code)
    assert not s.join_family(3, code)  # код одноразовый
    s.set_name(2, "Мама")
    assert [u.name for u in s.family_members(1)] == ["Катаржина", "Мама"]
    assert s.is_family_member(2) and not s.is_family_member(3)

    start = NOW + timedelta(days=3)
    ev = s.add_event(2, "Врач", start, NOW)
    s.add_member(ev.id, 2, [start - timedelta(days=1), start])
    s.add_member(ev.id, 1, [start - timedelta(hours=2)])
    assert sorted(d.chat_id for d in s.due_reminders(start)) == [1, 2, 2]
    assert [e.members for e in s.upcoming(1, NOW)] == [[1, 2]]

    assert s.snooze(1, ev.id, NOW)
    assert not s.snooze(3, ev.id, NOW)  # не участник
    assert s.delete_event(3, ev.id) is None
    assert s.delete_event(1, ev.id) is not None

    s.leave_family(2)
    assert [u.name for u in s.family_members(1)] == ["Катаржина"]


def test_settings(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    assert s.get_setting(1, "digest") is None
    s.set_setting(1, "digest", "08:00")
    s.set_setting(1, "digest", "09:00")
    assert s.get_setting(1, "digest") == "09:00"
    s.set_setting(1, "digest", None)
    assert s.get_setting(1, "digest") is None


def test_reopen_and_old_db(tmp_path):
    Storage(tmp_path / "db.sqlite")
    Storage(tmp_path / "db.sqlite")  # повторное открытие новой базы — ок
    import sqlite3
    old = sqlite3.connect(tmp_path / "old.sqlite")
    old.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
    old.commit()
    with pytest.raises(RuntimeError, match="старой версии"):
        Storage(tmp_path / "old.sqlite")
