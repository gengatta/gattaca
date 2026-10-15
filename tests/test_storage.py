from datetime import datetime, timedelta, timezone

from assistant.storage import Storage

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
OFFSETS = [timedelta(days=1), timedelta(hours=1), timedelta(0)]


def test_event_lifecycle(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    s.upsert_user(1, "Катаржина")
    start = NOW + timedelta(hours=5)
    ev = s.add_event(1, "Стоматолог", start, NOW)
    assert s.add_member(ev.id, 1, OFFSETS, NOW)
    assert not s.add_member(ev.id, 1, OFFSETS, NOW)  # повторно не добавляется

    # «за день» уже в прошлом — не создаётся
    assert len(s.due_reminders(NOW + timedelta(days=2))) == 2
    assert s.due_reminders(NOW) == []

    due = s.due_reminders(start - timedelta(hours=1))
    assert [(d.chat_id, d.event.title) for d in due] == [(1, "Стоматолог")]
    s.mark_sent(due[0].id)
    assert s.due_reminders(start - timedelta(minutes=30)) == []

    assert [e.id for e in s.upcoming(1, NOW)] == [ev.id]
    assert s.upcoming(2, NOW) == []  # чужой чат
    assert s.delete_event(2, ev.id) is None
    assert s.delete_event(1, ev.id).title == "Стоматолог"
    assert s.due_reminders(start) == []  # напоминания удалились каскадом


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

    ev = s.add_event(2, "Врач", NOW + timedelta(days=3), NOW)
    s.add_member(ev.id, 2, OFFSETS, NOW)
    s.add_member(ev.id, 1, [timedelta(hours=2)], NOW)
    due = s.due_reminders(NOW + timedelta(days=4))
    assert sorted(d.chat_id for d in due) == [1, 2, 2, 2]
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
