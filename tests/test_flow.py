"""Сквозной сценарий бота на подставных объектах Telegram (без сети)."""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from assistant import bot
from assistant.ai import AIDraft, AIResult
from assistant.storage import Storage

TZ = ZoneInfo("Europe/Warsaw")
KASIA, MOM, STRANGER = 1, 2, 3


class FakeMessage:
    def __init__(self, log, chat_id, text=None):
        self.log, self.chat_id, self.text = log, chat_id, text
        self.caption, self.photo, self.markup = None, None, None

    async def reply_text(self, text, reply_markup=None, **kw):
        m = FakeMessage(self.log, self.chat_id, text)
        m.markup = reply_markup
        self.log.append((self.chat_id, text, reply_markup))
        return m

    async def edit_text(self, text, **kw):
        self.log.append((self.chat_id, text, None))

    async def delete(self):
        pass


class FakeQuery:
    def __init__(self, data, message):
        self.data, self.message = data, message

    async def answer(self, text=None):
        pass

    async def edit_message_reply_markup(self, markup):
        pass


class FakeBot:
    username = "kasia_bot"

    def __init__(self, log):
        self.log = log

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.log.append((chat_id, text, reply_markup))

    async def send_chat_action(self, *a):
        pass


class FakeExtractor:
    def __init__(self):
        self.result = AIResult([], "")
        self.calls = []

    async def extract(self, now, members, **kw):
        self.calls.append((members, kw))
        return self.result


class Harness:
    def __init__(self, tmp_path, ai=False):
        config = object.__new__(bot.Config)
        config.tz = TZ
        config.allowed = {KASIA}
        config.default_offsets = "1d,1h,0m"
        config.default_digest = "08:00"
        config.default_weekly = "19:00"
        config.task_offsets = "2d,1d,0d"
        config.task_time = time(10, 0)
        config.ai_enabled = ai
        self.log = []
        self.extractor = FakeExtractor()
        self.app = SimpleNamespace(bot_data={
            "config": config,
            "storage": Storage(tmp_path / "db.sqlite"),
            "extractor": self.extractor,
        })
        self.bot = FakeBot(self.log)
        self.last = None

    @property
    def db(self) -> Storage:
        return self.app.bot_data["storage"]

    def ctx(self, args=()):
        return SimpleNamespace(application=self.app, bot=self.bot, args=list(args))

    def update(self, user, text=None, query=None):
        msg = FakeMessage(self.log, user, text)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user, first_name={1: "Катаржина", 2: "Ольга", 3: "X"}[user]),
            effective_chat=SimpleNamespace(id=user),
            effective_message=msg, message=msg,
            callback_query=FakeQuery(query, msg) if query else None,
        )

    def run(self, handler, user, text=None, args=(), query=None):
        before = len(self.log)
        asyncio.run(handler(self.update(user, text, query), self.ctx(args)))
        out = self.log[before:]
        return out

    def text(self, user, text):
        return self.run(bot.on_text, user, text)

    def press(self, user, data):
        return self.run(bot.on_button, user, query=data)


def buttons(entry):
    markup = entry[2]
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


@pytest.fixture
def h(tmp_path):
    h = Harness(tmp_path)
    h.run(bot.cmd_start, KASIA)
    return h


def invite_mom(h):
    out = h.run(bot.cmd_family, KASIA)
    link = next(t for _, t, _ in out if "start=join_" in t)
    code = link.split("start=join_")[1].split()[0]
    h.run(bot.cmd_start, MOM, args=[f"join_{code}"])
    h.run(bot.cmd_name, MOM, args=["Мама"])


def test_stranger_is_blocked(h):
    out = h.text(STRANGER, "завтра 10:00 врач")
    assert "только для своих" in out[0][1]


def test_fast_path_and_share(h):
    invite_mom(h)
    out = h.text(KASIA, "25.10 18:30 Стоматолог")
    assert "Записала" in out[0][1]
    event_id = int(out[0][1].split("#")[1].split("<")[0])
    assert f"sh:{event_id}:{MOM}" in buttons(out[0])

    out = h.press(KASIA, f"sh:{event_id}:{MOM}")
    assert any(chat == MOM and "Катаржина добавила для тебя" in t for chat, t, _ in out)
    assert h.db.get_event(event_id).members == [KASIA, MOM]

    # мама может удалить общее событие — Катаржине придёт уведомление
    out = h.run(bot.cmd_delete, MOM, args=[str(event_id)])
    assert any(chat == KASIA and "Мама удалила" in t for chat, t, _ in out)


def test_missing_time_is_asked(h):
    out = h.text(KASIA, "послезавтра день рождения мамы")
    assert "Во сколько" in out[0][1]

    out = h.text(KASIA, "ну где-то вечером")
    assert "Не нашла" in out[0][1]

    out = h.text(KASIA, "19:00")
    assert "Вот что я поняла" in out[0][1] and "День рождения мамы" in out[0][1]
    h.press(KASIA, "b:save")
    events = h.db.upcoming(KASIA, datetime.now(timezone.utc))
    assert [e.title for e in events] == ["День рождения мамы"]
    assert events[0].starts_at.astimezone(TZ).time() == time(19, 0)


def test_photo_schedule_with_questions(tmp_path):
    h = Harness(tmp_path, ai=True)
    h.run(bot.cmd_start, KASIA)
    invite_mom(h)
    in_3_days = datetime.now(TZ).date() + timedelta(days=3)
    h.extractor.result = AIResult([
        AIDraft("Маникюр", None, time(15, 0)),
        AIDraft("Врач", in_3_days, time(10, 0), ["Мама"]),
        AIDraft("Старое", date(2020, 1, 1), time(10, 0)),
    ], "Одна строка размыта")

    # многострочный текст уходит в распознавание
    out = h.text(KASIA, "маникюр в 15\nмаме к врачу")
    assert h.extractor.calls[0][0] == ["Катаржина", "Мама"]
    assert any("Когда <b>Маникюр</b>" in t for _, t, _ in out)

    out = h.press(KASIA, "q:d:1")  # завтра
    summary = out[-1][1]
    assert "Маникюр" in summary and "Врач" in summary and "Старое" not in summary
    assert "Пропустила уже прошедшие: 1" in summary and "размыта" in summary

    out = h.press(KASIA, "b:save")
    assert any(chat == MOM and "добавила для тебя: Врач" in t for chat, t, _ in out)
    assert [e.title for e in h.db.upcoming(MOM, datetime.now(timezone.utc))] == ["Врач"]
    assert len(h.db.upcoming(KASIA, datetime.now(timezone.utc))) == 2

    # «Всей семье»/«+ Мама» для всей пачки
    h.press(KASIA, f"shb:{MOM}")
    assert len(h.db.upcoming(MOM, datetime.now(timezone.utc))) == 2


def test_reminders_go_to_every_member(h):
    invite_mom(h)
    now = datetime.now(timezone.utc)
    ev = h.db.add_event(KASIA, "Ужин", now + timedelta(minutes=30), now - timedelta(hours=2))
    h.db.add_member(ev.id, KASIA, [now - timedelta(minutes=30)])
    h.db.add_member(ev.id, MOM, [now - timedelta(minutes=30)])
    before = len(h.log)
    asyncio.run(bot.job_reminders(h.ctx()))
    sent = h.log[before:]
    assert sorted(c for c, _, _ in sent) == [KASIA, MOM]
    assert any(c == MOM and "поставила Катаржина" in t for c, t, _ in sent)


def first_id(text):
    return int(text.split("<b>#")[1].split("<")[0])


def test_task_with_deadline_and_done(h):
    day = datetime.now(TZ).date() + timedelta(days=3)
    out = h.text(KASIA, f"сдать эссе к {day:%d.%m}")
    assert "Записала задачу" in out[0][1] and "🎓" in out[0][1]
    assert "Напомню:" in out[0][1]
    task_id = first_id(out[0][1])

    # три напоминания, все в 10:00 по её времени
    due = h.db.due_reminders(datetime.now(timezone.utc) + timedelta(days=10))
    assert len(due) == 3
    rows = h.db.db.execute("SELECT remind_at FROM reminders WHERE event_id = ?", (task_id,)).fetchall()
    assert {datetime.fromisoformat(r[0]).astimezone(TZ).time() for r in rows} == {time(10, 0)}

    out = h.run(bot.cmd_tasks, KASIA)
    assert "Сдать эссе" in out[0][1] and f"dn:{task_id}" in buttons(out[0])

    h.press(KASIA, f"dn:{task_id}")
    assert h.db.get_event(task_id).done_at is not None
    out = h.run(bot.cmd_tasks, KASIA)
    assert "Задач со сроком нет" in out[0][1]
    assert "Сделано за неделю: <b>1</b>" in bot.weekly_text(h.ctx(), KASIA)


def test_inbox_and_planning(h):
    out = h.text(KASIA, "задача: разобрать почту")
    assert "Во «входящих»" in out[0][1]
    task_id = first_id(out[0][1])

    out = h.run(bot.cmd_inbox, KASIA)
    assert "Разобрать почту" in out[0][1] and f"pl:{task_id}" in buttons(out[0])

    out = h.press(KASIA, f"pl:{task_id}")
    assert "Какой срок" in out[-1][1]
    out = h.text(KASIA, "завтра")
    assert "Вот что я поняла" in out[-1][1]
    h.press(KASIA, "b:save")
    assert h.db.inbox(KASIA) == []
    assert h.db.get_event(task_id).starts_at is not None
    assert "#" in h.run(bot.cmd_tasks, KASIA)[0][1]


def test_no_date_goes_to_inbox_by_button(h):
    out = h.text(KASIA, "купить подарок маме")
    assert "q:inbox" in buttons(out[0])
    out = h.press(KASIA, "q:inbox")
    assert "без срока" in out[-1][1]
    h.press(KASIA, "b:save")
    assert [e.title for e in h.db.inbox(KASIA)] == ["Купить подарок маме"]


def test_timezones_and_area_button(h):
    out = h.run(bot.cmd_tz, KASIA, args=["Москва"])
    assert "Мск" in out[0][1]
    out = h.text(KASIA, "созвон с клиентом в 10:00 по Нью-Йорку")
    text = out[0][1]
    assert "(10:00 NY)" in text and "🇺🇸" in text
    event_id = first_id(text)
    local = h.db.get_event(event_id).starts_at.astimezone(ZoneInfo("Europe/Moscow"))
    assert local.hour == 17  # 10:00 NY = 17:00 Мск (в октябре)

    out = h.press(KASIA, f"ar:{event_id}")
    assert f"as:{event_id}:2" in buttons(out[-1])
    h.press(KASIA, f"as:{event_id}:2")
    assert h.db.get_event(event_id).area == "Учёба"


def test_areas_command(h):
    h.run(bot.cmd_areas, KASIA, args=["add", "💼", "Фриланс"])
    out = h.run(bot.cmd_areas, KASIA)
    assert "💼 Фриланс" in out[0][1]
    out = h.text(KASIA, "#фриланс завтра 12:00 созвон с заказчиком")
    assert "💼" in out[0][1]
    h.run(bot.cmd_areas, KASIA, args=["del", "Фриланс"])
    assert "💼 Фриланс  <code>" not in h.run(bot.cmd_areas, KASIA)[0][1]


def test_digest_groups_by_area(h):
    h.text(KASIA, "сегодня 23:58 пара по маркетингу")
    h.text(KASIA, "сегодня 23:59 маникюр")
    text = bot.digest_text(h.ctx(), KASIA)
    assert "<b>🎓 Учёба</b>" in text and "<b>🏠 Личное</b>" in text
