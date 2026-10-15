from datetime import datetime, timedelta, timezone

from assistant.storage import Storage

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)


def test_event_lifecycle(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    start = NOW + timedelta(hours=5)
    offsets = [timedelta(days=1), timedelta(hours=1), timedelta(0)]
    ev = s.add_event(1, "Стоматолог", start, offsets, NOW)

    # «за день» уже в прошлом — не создаётся
    assert s.due_reminders(NOW + timedelta(days=2)).__len__() == 2
    assert s.due_reminders(NOW) == []

    due = s.due_reminders(start - timedelta(hours=1))
    assert [d.event.title for d in due] == ["Стоматолог"]
    s.mark_sent(due[0].id)
    assert s.due_reminders(start - timedelta(minutes=30)) == []

    assert [e.id for e in s.upcoming(1, NOW)] == [ev.id]
    assert s.upcoming(2, NOW) == []  # чужой чат
    assert not s.delete_event(2, ev.id)
    assert s.delete_event(1, ev.id)
    assert s.due_reminders(start) == []  # напоминания удалились каскадом


def test_settings(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    assert s.get_setting(1, "digest") is None
    s.set_setting(1, "digest", "08:00")
    s.set_setting(1, "digest", "09:00")
    assert s.get_setting(1, "digest") == "09:00"
    assert s.chats() == [1]
