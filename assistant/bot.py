"""Личный ассистент Катаржины в Telegram: упорядочивает дела и напоминает о них.

Встречи и задачи можно прислать текстом, целым расписанием или фото записей.
Каждая запись попадает в своё направление (работа РФ, работа США, учёба, личное),
время в другом часовом поясе («по Нью-Йорку») пересчитывается в её собственное.
Если чего-то не хватает — бот уточняет. Членов семьи можно пригласить, чтобы
ставить дела друг другу и получать общие напоминания.
"""

from __future__ import annotations

import html
import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import areas as areas_mod
from . import zones
from .areas import Area
from .parser import (
    Draft,
    ParseError,
    extract,
    finalize,
    format_offset,
    is_all_day,
    looks_like_schedule,
    parse_offsets,
    plural,
)
from .storage import Event, Storage, User

log = logging.getLogger("assistant")

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

HELP = """Я — твой личный ассистент 💐
Присылай мне всё, что летит со всех сторон, — я разложу по направлениям и напомню вовремя.

<b>Встречи</b>:
• <code>завтра в 10:00 созвон с Анной</code>
• <code>25.10 18:30 стоматолог</code>
• <code>созвон с клиентом в 10:00 по Нью-Йорку</code> — переведу в твоё время

<b>Задачи с дедлайном</b>:
• <code>сдать эссе до пятницы</code>
• <code>отправить отчёт к 25.10</code>
• <code>задача: разобрать почту</code> — без срока, во «входящие»

<b>Направления</b> определяю сама, но можно и явно: <code>#сша</code>, <code>#рф</code>, <code>#учеба</code>, <code>#личное</code>

<b>Расписание или фото</b> ежедневника 📸 — пришли целиком, я разберу и переспрошу, если чего-то не хватит.

<b>Команды</b>
/today — план на сегодня
/week — неделя по направлениям
/list — всё ближайшее (можно <code>/list сша</code>)
/tasks — задачи и дедлайны
/inbox — задачи без срока
/areas — направления
/tz — твой часовой пояс
/reminders, /digest, /weekly — когда напоминать и присылать сводки
/family, /name — семья
/cancel — отменить незаконченное добавление"""


class Config:
    def __init__(self) -> None:
        self.token = os.environ["BOT_TOKEN"]
        self.tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
        self.allowed = {
            int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
        }
        self.default_offsets = os.getenv("DEFAULT_REMINDERS", "1d,1h,0m")
        self.task_offsets = os.getenv("TASK_REMINDERS", "2d,1d,0d")
        self.task_time = datetime.strptime(os.getenv("TASK_REMIND_TIME", "10:00"), "%H:%M").time()
        self.default_digest = os.getenv("DIGEST_TIME", "08:00")
        self.default_weekly = os.getenv("WEEKLY_TIME", "19:00")
        self.db_path = os.getenv("DB_PATH", "data/assistant.db")
        self.ai_model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
        self.ai_enabled = bool(os.getenv("ANTHROPIC_API_KEY"))
        parse_offsets(self.default_offsets)  # проверяем формат при старте
        parse_task_days(self.task_offsets)


def parse_task_days(spec: str) -> list[int]:
    """'2d,1d,0d' -> [2, 1, 0] — за сколько дней до дедлайна напоминать о задаче."""
    days = []
    for part in spec.split(","):
        part = part.strip().lower().rstrip("dд")
        if part:
            days.append(int(part))
    return sorted(set(days), reverse=True)


# --- вспомогательное ------------------------------------------------------

def cfg(ctx: ContextTypes.DEFAULT_TYPE) -> Config:
    return ctx.application.bot_data["config"]


def db(ctx: ContextTypes.DEFAULT_TYPE) -> Storage:
    return ctx.application.bot_data["storage"]


def tz_of(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> ZoneInfo:
    name = db(ctx).get_setting(chat_id, "tz")
    return ZoneInfo(name) if name else cfg(ctx).tz


def now_for(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> datetime:
    return datetime.now(tz_of(ctx, chat_id)).replace(second=0, microsecond=0)


def user_areas(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list[Area]:
    raw = db(ctx).get_setting(chat_id, "areas")
    return areas_mod.from_json(json.loads(raw)) if raw else list(areas_mod.DEFAULT_AREAS)


def save_areas(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, items: list[Area]) -> None:
    db(ctx).set_setting(chat_id, "areas", json.dumps(areas_mod.to_json(items), ensure_ascii=False))


def rel_day(d: date, today: date) -> str:
    if d == today:
        return "сегодня"
    if d == today + timedelta(days=1):
        return "завтра"
    s = f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"
    return s + (f" {d.year}" if d.year != today.year else "")


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def fmt_when(ctx: ContextTypes.DEFAULT_TYPE, e: Event, viewer: int) -> str:
    """«сегодня в 17:00 (10:00 NY)», «до пт, 17 октября», «без срока»."""
    if e.starts_at is None:
        return "без срока"
    tz = tz_of(ctx, viewer)
    local = e.starts_at.astimezone(tz)
    day = rel_day(local.date(), datetime.now(tz).date())
    if e.is_task:
        s = f"до {day}" if e.all_day else f"до {day} {local:%H:%M}"
    else:
        s = f"{day} в {local:%H:%M}"
    if e.src_tz and e.src_tz != str(tz) and not e.all_day:
        other = e.starts_at.astimezone(ZoneInfo(e.src_tz))
        s += f" ({other:%H:%M} {zones.label(e.src_tz)})"
    return s


def fmt_left(delta: timedelta) -> str:
    minutes = round(delta.total_seconds() / 60)
    if minutes <= 0:
        return "Сейчас"
    if minutes < 60:
        return f"Через {minutes} мин"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"Через {hours} ч" + (f" {mins} мин" if mins else "")
    return f"Через {round(hours / 24)} дн"


def names(ctx: ContextTypes.DEFAULT_TYPE, chat_ids: list[int]) -> str:
    users = [db(ctx).get_user(c) for c in chat_ids]
    return ", ".join(u.name for u in users if u)


def fmt_item(ctx: ContextTypes.DEFAULT_TYPE, e: Event, viewer: int, show_area: bool = True) -> str:
    prefix = areas_mod.emoji_for(user_areas(ctx, viewer), e.area) + " " if show_area else ""
    mark = "☐ " if e.is_task else ""
    line = f"{prefix}{mark}<b>#{e.id}</b> {html.escape(e.title)} — {fmt_when(ctx, e, viewer)}"
    extra = []
    if e.creator_id != viewer:
        creator = db(ctx).get_user(e.creator_id)
        if creator:
            extra.append(f"👤 {html.escape(creator.name)}")
    if len(e.members) > 1:
        extra.append("👥 " + html.escape(names(ctx, e.members)))
    return line + (f" <i>({'; '.join(extra)})</i>" if extra else "")


def grouped(ctx: ContextTypes.DEFAULT_TYPE, events: list[Event], viewer: int) -> list[str]:
    """Строки списка, сгруппированные по направлениям (в порядке из /areas)."""
    my_areas = user_areas(ctx, viewer)
    order = [a.name for a in my_areas]
    groups: dict[str | None, list[Event]] = {}
    for e in events:
        key = a.name if (a := areas_mod.find(my_areas, e.area)) else e.area
        groups.setdefault(key, []).append(e)
    if list(groups) == [None]:
        return [fmt_item(ctx, e, viewer, show_area=False) for e in events]
    keys = sorted(groups, key=lambda k: (k is None, order.index(k) if k in order else len(order)))
    lines = []
    for k in keys:
        a = areas_mod.find(my_areas, k)
        header = a.label if a else (f"📌 {k}" if k else "▫️ Без направления")
        lines += ["", f"<b>{html.escape(header)}</b>"]
        lines += [fmt_item(ctx, e, viewer, show_area=False) for e in groups[k]]
    return lines


def offsets_for(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list[timedelta]:
    spec = db(ctx).get_setting(chat_id, "offsets") or cfg(ctx).default_offsets
    return parse_offsets(spec)


def reminder_times(ctx: ContextTypes.DEFAULT_TYPE, e: Event, chat_id: int) -> list[datetime]:
    """Когда напоминать участнику: встречи — по /reminders, задачи — утром за N дней до дедлайна."""
    if e.starts_at is None:
        return []
    now = datetime.now(timezone.utc)
    if not e.is_task:
        times = [e.starts_at - off for off in offsets_for(ctx, chat_id)]
    else:
        tz = tz_of(ctx, chat_id)
        deadline_day = e.starts_at.astimezone(tz).date()
        times = [datetime.combine(deadline_day - timedelta(days=d), cfg(ctx).task_time, tz)
                 for d in parse_task_days(cfg(ctx).task_offsets)]
        times = [t for t in times if t < e.starts_at]
        if not e.all_day:
            times.append(e.starts_at - timedelta(hours=1))
    return sorted({t for t in times if t > now})


def describe_reminders(ctx: ContextTypes.DEFAULT_TYPE, e: Event, chat_id: int) -> str:
    if e.starts_at is None:
        return "📥 Во «входящих» — напомню в воскресном обзоре, /inbox"
    times = reminder_times(ctx, e, chat_id)
    if not times:
        return "🔔 Это слишком скоро — напоминаний не будет"
    if not e.is_task:
        offs = [format_offset(o) for o in offsets_for(ctx, chat_id) if e.starts_at - o in times]
        return "🔔 Напомню: " + ", ".join(offs)
    tz = tz_of(ctx, chat_id)
    today = datetime.now(tz).date()

    def short(t: datetime) -> str:
        d = t.astimezone(tz)
        day = rel_day(d.date(), today) if d.date() - today < timedelta(days=2) else f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"
        return f"{day} в {d:%H:%M}"

    return "🔔 Напомню: " + " · ".join(short(t) for t in times)


def others_in_family(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list[User]:
    return [u for u in db(ctx).family_members(chat_id) if u.chat_id != chat_id]


def is_allowed(ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    allowed = cfg(ctx).allowed
    # Приглашённые через /family проходят, даже если их нет в ALLOWED_USER_IDS
    return not allowed or user_id in allowed or db(ctx).is_family_member(user_id)


def restricted(handler):
    """Пускаем только своих: ALLOWED_USER_IDS и тех, кого пригласили в семью."""

    @wraps(handler)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or not is_allowed(ctx, user.id):
            log.warning("Отказано в доступе: user_id=%s", user.id if user else None)
            if update.callback_query:
                await update.callback_query.answer("Нет доступа")
            elif update.effective_message:
                await update.effective_message.reply_text(
                    "Извини, я личный ассистент и работаю только для своих.\n"
                    f"Если тебя пригласили — открой ссылку-приглашение. Твой id: {user.id if user else '?'}"
                )
            return
        db(ctx).upsert_user(user.id, user.first_name or "Без имени")
        return await handler(update, ctx)

    return wrapper


async def notify(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, markup=None) -> None:
    try:
        await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:
        log.exception("Не удалось отправить сообщение в чат %s", chat_id)


# --- черновики и уточняющие вопросы ------------------------------------------
#
# Незаконченное добавление хранится в настройке "pending" как JSON:
#   {"drafts": [{title, day, at, source, kind, tz, area, who, ...}], "ask": индекс | null}
# "who" — chat_id участников, кроме самого автора; "plan_id" — задача из «входящих»,
# которой назначаем срок.

def draft_to_dict(d: Draft, who: list[int] | None = None, area: str | None = None) -> dict:
    if d.starts_at:
        local = d.starts_at.astimezone(ZoneInfo(d.tz)) if d.tz else d.starts_at
        day, at, source = local.date(), local.time(), "exact"
    else:
        day, at, source = d.day, d.at, d.source
    return {
        "title": d.title,
        "day": day.isoformat() if day else None,
        "at": at.strftime("%H:%M") if at else None,
        "source": source,
        "kind": d.kind,
        "tz": d.tz,
        "area": area,
        "who": who or [],
    }


def dict_to_draft(x: dict) -> Draft:
    return Draft(
        title=x["title"],
        day=date.fromisoformat(x["day"]) if x["day"] else None,
        at=datetime.strptime(x["at"], "%H:%M").time() if x["at"] else None,
        source=x.get("source"),
        kind=x.get("kind", "event"),
        tz=x.get("tz"),
    )


def missing_of(x: dict) -> str | None:
    if x.get("plan_id") and not x["day"] and not x["at"]:
        return "date"  # назначаем срок задаче из «входящих»
    if x.get("ask_day") and not x["day"]:
        return "date"  # на фото у встречи было только время — день уточняем
    return dict_to_draft(x).missing


def load_pending(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> dict | None:
    raw = db(ctx).get_setting(chat_id, "pending")
    return json.loads(raw) if raw else None


def save_pending(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, pending: dict | None) -> None:
    db(ctx).set_setting(chat_id, "pending", json.dumps(pending, ensure_ascii=False) if pending else None)


async def start_batch(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, msg: Message, drafts: list[dict], note: str = ""
) -> None:
    pending = {"drafts": drafts, "ask": None, "note": note}
    await advance(ctx, chat_id, msg, pending)


def preview_event(ctx: ContextTypes.DEFAULT_TYPE, x: dict, starts_at: datetime | None) -> Event:
    """Событие-«призрак» для показа черновика в итоговом списке."""
    d = dict_to_draft(x)
    return Event(0, 0, x["title"] or "Без названия", starts_at, [], d.kind, x.get("area"),
                 is_all_day(d), d.tz)


async def advance(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, msg: Message, pending: dict) -> None:
    """Задаёт следующий уточняющий вопрос или показывает итог на подтверждение."""
    now = now_for(ctx, chat_id)
    drafts = pending["drafts"]

    for i, x in enumerate(drafts):
        missing = missing_of(x)
        if missing is None:
            continue
        pending["ask"] = i
        save_pending(ctx, chat_id, pending)
        d = dict_to_draft(x)
        title = html.escape(d.title or "это")
        if missing == "time":
            text = f"🕒 Во сколько <b>{title}</b> ({fmt_day(d.day)})?\nНапиши время, например <code>18:30</code>"
            buttons = [[InlineKeyboardButton("Без точного времени (09:00)", callback_data="q:t:09:00")]]
        else:
            when = f" в {d.at:%H:%M}" if d.at else ""
            verb = "Какой срок у задачи" if x.get("plan_id") or d.kind == "task" else "Когда"
            text = (f"📅 {verb} <b>{title}</b>{when}?\n"
                    "Напиши дату, например <code>25.10</code>, <code>в пятницу</code> или <code>завтра 18:30</code>")
            buttons = [[InlineKeyboardButton("Сегодня", callback_data="q:d:0"),
                        InlineKeyboardButton("Завтра", callback_data="q:d:1")]]
            if not x.get("plan_id"):
                buttons.append([InlineKeyboardButton("📥 Задача без срока", callback_data="q:inbox")])
        buttons.append([InlineKeyboardButton("⏭ Пропустить это", callback_data="q:skip"),
                        InlineKeyboardButton("✖️ Отменить всё", callback_data="b:cancel")])
        await msg.reply_text(text, parse_mode=ParseMode.HTML,
                             reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Всё известно — проверяем, что не в прошлом, и показываем итог
    pending["ask"] = None
    ready, past = [], []
    for x in drafts:
        try:
            ready.append((x, finalize(dict_to_draft(x), now)))
        except ParseError:
            past.append(x)
    pending["drafts"] = [x for x, _ in ready]

    if not ready:
        save_pending(ctx, chat_id, None)
        text = "Все эти дела уже в прошлом — нечего добавлять 🙂" if past else "Нечего добавлять."
        await msg.reply_text(text)
        return

    save_pending(ctx, chat_id, pending)
    far = datetime.max.replace(tzinfo=timezone.utc)
    ready.sort(key=lambda r: r[1] or far)
    lines = ["📝 <b>Вот что я поняла:</b>"]
    preview = [preview_event(ctx, x, s) for x, s in ready]
    for line in grouped(ctx, preview, chat_id):
        lines.append(line.replace("<b>#0</b> ", ""))
    for x, _ in ready:
        if x["who"]:
            lines.append(f"<i>👥 «{html.escape(x['title'])}» — ещё для: {html.escape(names(ctx, x['who']))}</i>")
    if past:
        lines += ["", f"<i>Пропустила уже прошедшие: {len(past)}</i>"]
    if pending.get("note"):
        lines += ["", f"<i>{html.escape(pending['note'])}</i>"]
    await msg.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Сохранить ({len(ready)})", callback_data="b:save"),
            InlineKeyboardButton("✖️ Отмена", callback_data="b:cancel"),
        ]]),
    )


async def answer_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    chat_id = update.effective_chat.id
    x = pending["drafts"][pending["ask"]]
    try:
        reply = extract(update.message.text, now_for(ctx, chat_id))
    except ParseError as e:
        await update.message.reply_text(f"🤔 {e}. Попробуй ещё раз или нажми «Пропустить».")
        return
    if not reply.found_anything:
        await update.message.reply_text(
            "🤔 Не нашла здесь ни даты, ни времени. Напиши, например, <code>18:30</code> "
            "или <code>в пятницу</code>, либо нажми «Пропустить».\n/cancel — отменить всё",
            parse_mode=ParseMode.HTML,
        )
        return
    merged = draft_to_dict(reply)
    for key in ("day", "at"):
        if merged[key]:
            x[key] = merged[key]
    if merged["day"]:
        x["source"] = merged["source"]
    if reply.tz:
        x["tz"] = reply.tz
    if reply.kind == "task":
        x["kind"] = "task"
    if not x["title"] and reply.title:
        x["title"] = reply.title
    await advance(ctx, chat_id, update.message, pending)


async def save_batch(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, pending: dict) -> list[Event]:
    now = now_for(ctx, chat_id)
    me = db(ctx).get_user(chat_id)
    saved = []
    for x in pending["drafts"]:
        d = dict_to_draft(x)
        try:
            starts_at = finalize(d, now)
        except ParseError:
            continue
        if x.get("plan_id"):
            event = db(ctx).schedule(chat_id, x["plan_id"], starts_at, is_all_day(d), d.tz)
            if event:
                for member in event.members:
                    db(ctx).add_reminders(event.id, member, reminder_times(ctx, event, member))
                saved.append(event)
            continue
        event = db(ctx).add_event(chat_id, x["title"] or "Без названия", starts_at, now,
                                  kind=d.kind, area=x.get("area"), all_day=is_all_day(d), src_tz=d.tz)
        for member in [chat_id, *x["who"]]:
            db(ctx).add_member(event.id, member, reminder_times(ctx, event, member))
        event = db(ctx).get_event(event.id)
        for member in x["who"]:
            await notify(ctx, member, f"📌 {html.escape(me.name)} добавила для тебя: "
                                      f"{html.escape(event.title)} — {fmt_when(ctx, event, member)}")
        saved.append(event)
    save_pending(ctx, chat_id, None)
    db(ctx).set_setting(chat_id, "last_batch", json.dumps([e.id for e in saved]))
    return saved


def share_buttons(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, prefix: str) -> list[list[InlineKeyboardButton]]:
    """Кнопки «поделиться с семьёй». prefix: 'sh:<event_id>' или 'shb'."""
    others = others_in_family(ctx, chat_id)
    if not others:
        return []
    row = [InlineKeyboardButton(f"👤 + {u.name[:20]}", callback_data=f"{prefix}:{u.chat_id}")
           for u in others[:3]]
    if len(others) > 1:
        row.append(InlineKeyboardButton("👥 Всей семье", callback_data=f"{prefix}:all"))
    return [row]


def item_buttons(events: list[Event]) -> list[list[InlineKeyboardButton]]:
    rows = []
    for e in events[:15]:
        if e.is_task:
            rows.append([InlineKeyboardButton(f"✅ #{e.id} {e.title[:28]}", callback_data=f"dn:{e.id}"),
                         InlineKeyboardButton("🗑", callback_data=f"del:{e.id}")])
        else:
            rows.append([InlineKeyboardButton(f"🗑 #{e.id} {e.title[:30]}", callback_data=f"del:{e.id}")])
    return rows


# --- знакомство и семья ------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Приглашение по ссылке t.me/<бот>?start=join_<код> — до проверки доступа
    if ctx.args and ctx.args[0].startswith("join_"):
        await do_join(update, ctx, ctx.args[0][5:])
        return
    await start_known(update, ctx)


def init_user_settings(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    storage = db(ctx)
    if storage.get_setting(chat_id, "digest") is None:
        storage.set_setting(chat_id, "digest", cfg(ctx).default_digest)
        storage.set_setting(chat_id, "weekly", cfg(ctx).default_weekly)
        # первая сводка — завтра утром, а не сразу после знакомства
        today = now_for(ctx, chat_id).date().isoformat()
        storage.set_setting(chat_id, "digest_sent", today)
        storage.set_setting(chat_id, "weekly_sent", today)


def tz_line(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    tz = tz_of(ctx, chat_id)
    return (f"🕒 Твой часовой пояс: <b>{zones.label(str(tz))}</b> (сейчас {now_for(ctx, chat_id):%H:%M}). "
            "Если не так — напиши, например, <code>/tz Нью-Йорк</code> или <code>/tz Москва</code>.")


@restricted
async def start_known(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    init_user_settings(ctx, chat_id)
    name = db(ctx).get_user(chat_id).name
    await update.message.reply_text(
        f"Привет, {html.escape(name)}! 👋\n\n{HELP}\n\n{tz_line(ctx, chat_id)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Укажи код приглашения: /join КОД")
        return
    await do_join(update, ctx, ctx.args[0])


async def do_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    user = update.effective_user
    storage = db(ctx)
    storage.upsert_user(user.id, user.first_name or "Без имени")
    if not storage.join_family(user.id, code.strip()):
        await update.message.reply_text(
            "Это приглашение не подходит — возможно, им уже воспользовались. "
            "Попроси прислать новое (команда /family)."
        )
        return
    init_user_settings(ctx, update.effective_chat.id)
    members = storage.family_members(user.id)
    for m in members:
        if m.chat_id != user.id:
            await notify(ctx, m.chat_id, f"👋 {html.escape(user.first_name or 'Новый участник')} "
                                         "присоединился(-ась) к семье")
    await update.message.reply_text(
        f"🎉 Добро пожаловать в семью! Сейчас в ней: {html.escape(', '.join(m.name for m in members))}.\n\n"
        "Как тебя называть? Например: <code>/name Мама</code> — так другим будет понятнее, "
        f"для кого событие.\n\n{HELP}\n\n{tz_line(ctx, user.id)}",
        parse_mode=ParseMode.HTML,
    )


@restricted
async def cmd_family(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    storage = db(ctx)
    if ctx.args and ctx.args[0].lower() in ("leave", "выйти"):
        storage.leave_family(chat_id)
        await update.message.reply_text("Ты вышла из семьи. Общие события остались в списке.")
        return
    code = storage.new_invite(chat_id)
    members = storage.family_members(chat_id)
    link = f"https://t.me/{ctx.bot.username}?start=join_{code}"
    await update.message.reply_text(
        f"👨‍👩‍👧 <b>Семья:</b> {html.escape(', '.join(m.name for m in members))}\n\n"
        "Чтобы пригласить маму (или кого-то ещё), перешли ей эту ссылку — "
        f"она откроет бота и сразу попадёт в семью:\n{link}\n\n"
        f"Или пусть напишет боту: <code>/join {code}</code>\n"
        "<i>Приглашение одноразовое. Для следующего человека снова вызови /family.</i>\n\n"
        "Что дальше: в сообщениях можно писать, для кого дело "
        "(«маме к врачу в пт 10:00»), а под каждой записью будут кнопки «+ Мама».\n"
        "Выйти из семьи: <code>/family leave</code>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@restricted
async def cmd_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = " ".join(ctx.args).strip()[:30]
    if not name:
        current = db(ctx).get_user(update.effective_chat.id).name
        await update.message.reply_text(f"Сейчас тебя зовут «{current}». Изменить: /name Мама")
        return
    db(ctx).set_name(update.effective_chat.id, name)
    await update.message.reply_text(f"Готово, теперь ты «{name}» 🙂")


# --- настройки ---------------------------------------------------------------

@restricted
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


@restricted
async def cmd_tz(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if ctx.args:
        zone = zones.resolve(" ".join(ctx.args))
        if zone is None:
            await update.message.reply_text(
                "Не знаю такой город 🤔 Попробуй: /tz Москва, /tz Нью-Йорк, /tz Лос-Анджелес "
                "или название пояса вроде America/New_York"
            )
            return
        db(ctx).set_setting(chat_id, "tz", zone)
    await update.message.reply_text(tz_line(ctx, chat_id), parse_mode=ParseMode.HTML)


@restricted
async def cmd_areas(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    items = user_areas(ctx, chat_id)
    args = ctx.args
    if args and args[0].lower() in ("add", "добавить") and len(args) > 1:
        rest = args[1:]
        emoji = rest.pop(0) if not any(ch.isalnum() for ch in rest[0]) and len(rest) > 1 else "📌"
        name = " ".join(rest)[:30]
        if areas_mod.find(items, name):
            await update.message.reply_text("Такое направление уже есть")
            return
        items.append(Area(emoji, name, aliases=[areas_mod.norm(name)]))
        save_areas(ctx, chat_id, items)
    elif args and args[0].lower() in ("del", "удалить") and len(args) > 1:
        a = areas_mod.find(items, " ".join(args[1:]))
        if not a:
            await update.message.reply_text("Не нашла такое направление")
            return
        items = [x for x in items if x.name != a.name]
        save_areas(ctx, chat_id, items)
    elif args and args[0].lower() in ("reset", "сброс"):
        items = list(areas_mod.DEFAULT_AREAS)
        db(ctx).set_setting(chat_id, "areas", None)
    lines = ["🗂 <b>Твои направления</b>", ""]
    for a in items:
        tag = f"#{areas_mod.norm(a.aliases[0])}" if a.aliases else ""
        lines.append(f"{a.emoji} {html.escape(a.name)}  <code>{tag}</code>")
    lines += ["",
              "Направление я определяю сама по смыслу, а тег в сообщении (например <code>#сша</code>) "
              "задаёт его явно. Под каждой записью есть кнопка 🏷, чтобы поправить.",
              "",
              "Добавить: <code>/areas add 💼 Фриланс</code>",
              "Удалить: <code>/areas del Фриланс</code>",
              "Вернуть как было: <code>/areas reset</code>"]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    save_pending(ctx, update.effective_chat.id, None)
    await update.message.reply_text("Хорошо, ничего не добавляю.")


@restricted
async def cmd_reminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if ctx.args:
        spec = " ".join(ctx.args)
        try:
            offsets = parse_offsets(spec)
        except ParseError as e:
            await update.message.reply_text(str(e))
            return
        if not offsets:
            await update.message.reply_text("Нужен хотя бы один интервал, например: /reminders 1h")
            return
        db(ctx).set_setting(chat_id, "offsets", spec)
        prefix = "Готово! О новых встречах буду напоминать"
    else:
        offsets = offsets_for(ctx, chat_id)
        prefix = "О встречах напоминаю"
    days = parse_task_days(cfg(ctx).task_offsets)
    task_days = ", ".join("в день дедлайна" if d == 0 else f"за {d} {plural(d, 'день', 'дня', 'дней')}"
                          for d in days)
    await update.message.reply_text(
        f"🔔 {prefix}: {', '.join(format_offset(o) for o in offsets)}.\n"
        f"📝 О задачах — {task_days}, в {cfg(ctx).task_time:%H:%M}.\n\n"
        "Изменить для встреч: <code>/reminders 1d, 3h, 30m, 0m</code>\n"
        "(d — дни, h — часы, m — минуты, 0m — в момент начала)",
        parse_mode=ParseMode.HTML,
    )


async def time_setting(update: Update, ctx: ContextTypes.DEFAULT_TYPE, key: str, what: str, when: str) -> None:
    chat_id = update.effective_chat.id
    if ctx.args:
        arg = ctx.args[0].lower()
        if arg in ("off", "выкл", "нет"):
            db(ctx).set_setting(chat_id, key, "off")
            await update.message.reply_text(f"{what} выключен(а)")
            return
        try:
            t = datetime.strptime(arg, "%H:%M").time()
        except ValueError:
            await update.message.reply_text(f"Формат: /{key} 08:30 или /{key} off")
            return
        db(ctx).set_setting(chat_id, key, f"{t:%H:%M}")
        await update.message.reply_text(f"Готово: {what.lower()} — {when} в {t:%H:%M}")
        return
    current = db(ctx).get_setting(chat_id, key) or "off"
    state = "выключен(а)" if current == "off" else f"приходит {when} в {current}"
    await update.message.reply_text(f"{what} {state}.\nИзменить: /{key} 08:30, выключить: /{key} off")


@restricted
async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await time_setting(update, ctx, "digest", "☀️ Утренний план на день", "каждый день")


@restricted
async def cmd_weekly(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await time_setting(update, ctx, "weekly", "🗓 Обзор недели", "по воскресеньям")


# --- добавление ----------------------------------------------------------------

@restricted
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(ctx.args)
    if not text:
        await update.message.reply_text(
            "Напиши дело после команды, например:\n/add завтра 10:00 созвон"
        )
        return
    await handle_text(update, ctx, text)


@restricted
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pending = load_pending(ctx, update.effective_chat.id)
    if pending and pending.get("ask") is not None:
        await answer_question(update, ctx, pending)
        return
    await handle_text(update, ctx, update.message.text)


def detect_area(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, d: Draft) -> str | None:
    return areas_mod.detect(user_areas(ctx, chat_id), d.title, d.tags, d.tz)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = update.effective_chat.id
    now = now_for(ctx, chat_id)
    single_line = len([ln for ln in text.splitlines() if ln.strip()]) == 1

    try:
        draft = extract(text, now)
    except ParseError as e:
        draft, error = None, e
    else:
        error = None

    # Быстрый путь: одна понятная запись — сохраняем сразу, без нейросети
    simple = draft and single_line and not looks_like_schedule(text) and draft.title
    if simple and draft.missing is None and (draft.found_anything or draft.kind == "task"):
        try:
            starts_at = finalize(draft, now)
        except ParseError as e:
            await update.message.reply_text(f"🤔 {e}.")
            return
        await save_single(update, ctx, draft, starts_at)
        return

    # Расписание, свободный текст или неполная запись — разбираем через Claude
    if cfg(ctx).ai_enabled:
        await recognize(update, ctx, text=text)
        return

    # Без Claude: одна запись, которой не хватает даты/времени, — переспрашиваем
    if simple:
        await start_batch(ctx, chat_id, update.message,
                          [draft_to_dict(draft, area=detect_area(ctx, chat_id, draft))])
        return
    reason = str(error) if error else "Пришли, пожалуйста, по одному делу в сообщении"
    await update.message.reply_text(
        f"🤔 {reason}.\nПопробуй так: <code>завтра в 18:30 Стоматолог</code>\nВсе примеры — /help",
        parse_mode=ParseMode.HTML,
    )


async def save_single(update: Update, ctx: ContextTypes.DEFAULT_TYPE, d: Draft, starts_at: datetime | None) -> None:
    chat_id = update.effective_chat.id
    now = now_for(ctx, chat_id)
    event = db(ctx).add_event(chat_id, d.title, starts_at, now, kind=d.kind,
                              area=detect_area(ctx, chat_id, d), all_day=is_all_day(d), src_tz=d.tz)
    db(ctx).add_member(event.id, chat_id, reminder_times(ctx, event, chat_id))
    event = db(ctx).get_event(event.id)
    buttons = share_buttons(ctx, chat_id, f"sh:{event.id}")
    buttons.append([InlineKeyboardButton("🏷 Направление", callback_data=f"ar:{event.id}"),
                    InlineKeyboardButton("↩️ Отменить", callback_data=f"del:{event.id}")])
    what = "задачу" if event.is_task else ""
    await update.message.reply_text(
        f"✅ Записала{(' ' + what) if what else ''}: {fmt_item(ctx, event, chat_id)}\n"
        f"{describe_reminders(ctx, event, chat_id)}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@restricted
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not cfg(ctx).ai_enabled:
        await update.message.reply_text(
            "📸 Распознавание фото пока не подключено (нужен ANTHROPIC_API_KEY). "
            "Напиши дело текстом, например: завтра 10:00 созвон"
        )
        return
    msg = update.message
    if msg.photo:
        file = await msg.photo[-1].get_file()
        image_type = "image/jpeg"
    else:
        file = await msg.document.get_file()
        image_type = msg.document.mime_type or "image/jpeg"
    if image_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        await msg.reply_text("Этот формат картинки я не читаю — пришли, пожалуйста, обычное фото.")
        return
    image = bytes(await file.download_as_bytearray())
    await recognize(update, ctx, text=msg.caption, image=image, image_type=image_type)


async def recognize(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    text: str | None = None, image: bytes | None = None, image_type: str = "image/jpeg",
) -> None:
    from .ai import AIError  # импорт здесь, чтобы бот работал и без пакета anthropic

    chat_id = update.effective_chat.id
    extractor = ctx.application.bot_data["extractor"]
    family = db(ctx).family_members(chat_id)
    my_areas = user_areas(ctx, chat_id)
    by_name: dict[str, int] = {}
    for u in family:
        by_name.setdefault(u.name, u.chat_id)

    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    status = await update.message.reply_text("⏳ Читаю..." if image else "⏳ Разбираю...")
    try:
        result = await extractor.extract(
            now_for(ctx, chat_id), list(by_name) if len(family) > 1 else [],
            author=db(ctx).get_user(chat_id).name, text=text, image=image, image_type=image_type,
            areas=[a.name for a in my_areas],
        )
    except AIError as e:
        await status.edit_text(f"😔 {e}")
        return
    await status.delete()

    if not result.events:
        hint = f"\n<i>{html.escape(result.comment)}</i>" if result.comment else ""
        await update.message.reply_text(
            "🤔 Не нашла здесь дел. Попробуй так: <code>завтра в 18:30 Стоматолог</code>" + hint,
            parse_mode=ParseMode.HTML,
        )
        return

    drafts = []
    for ev in result.events:
        who = [by_name[w] for w in ev.for_whom if by_name.get(w) not in (None, chat_id)]
        d = Draft(ev.title, day=ev.day, at=ev.at, source="exact" if ev.day else None,
                  kind=ev.kind, tz=ev.tz)
        area = ev.area or detect_area(ctx, chat_id, d)
        drafts.append(draft_to_dict(d, sorted(set(who)), area) | {"ask_day": ev.day is None and ev.kind == "event"})
    await start_batch(ctx, chat_id, update.message, drafts, note=result.comment)


# --- списки ------------------------------------------------------------------

async def send_list(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, events: list[Event], title: str, empty: str,
    overdue: list[Event] | None = None, footer: str = "",
) -> None:
    chat_id = update.effective_chat.id
    overdue = overdue or []
    if not events and not overdue:
        await update.message.reply_text(empty + (f"\n\n{footer}" if footer else ""), parse_mode=ParseMode.HTML)
        return
    lines = [title]
    if overdue:
        lines += ["", "⚠️ <b>Просрочено</b>"] + [fmt_item(ctx, e, chat_id) for e in overdue]
    lines += grouped(ctx, events, chat_id)
    if footer:
        lines += ["", footer]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(item_buttons(overdue + events)),
    )


def inbox_footer(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    n = len(db(ctx).inbox(chat_id))
    return f"📥 Во «входящих» {n} {plural(n, 'задача', 'задачи', 'задач')} без срока — /inbox" if n else ""


def filter_area(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, events: list[Event]) -> tuple[list[Event], str]:
    if not ctx.args:
        return events, ""
    a = areas_mod.find(user_areas(ctx, chat_id), " ".join(ctx.args))
    if not a:
        return events, ""
    return [e for e in events if areas_mod.find([a], e.area)], f" · {a.label}"


@restricted
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now = now_for(ctx, chat_id)
    events, suffix = filter_area(ctx, chat_id, db(ctx).upcoming(chat_id, now))
    overdue, _ = filter_area(ctx, chat_id, db(ctx).overdue(chat_id, now))
    await send_list(update, ctx, events[:20], f"📅 <b>Ближайшее{html.escape(suffix)}</b>",
                    "Пока ничего не запланировано ✨", overdue, inbox_footer(ctx, chat_id))


@restricted
async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now = now_for(ctx, chat_id)
    end = datetime.combine(now.date() + timedelta(days=1), time(0), now.tzinfo)
    await send_list(update, ctx, db(ctx).upcoming(chat_id, now, end), "☀️ <b>Сегодня</b>",
                    "На сегодня больше ничего нет 🌿", db(ctx).overdue(chat_id, now))


@restricted
async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now = now_for(ctx, chat_id)
    events, suffix = filter_area(ctx, chat_id, db(ctx).upcoming(chat_id, now, now + timedelta(days=7)))
    await send_list(update, ctx, events, f"🗓 <b>Ближайшие 7 дней{html.escape(suffix)}</b>",
                    "На неделю ничего не запланировано ✨", db(ctx).overdue(chat_id, now),
                    inbox_footer(ctx, chat_id))


@restricted
async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now = now_for(ctx, chat_id)
    tasks = [e for e in db(ctx).upcoming(chat_id, now) if e.is_task]
    tasks, suffix = filter_area(ctx, chat_id, tasks)
    await send_list(update, ctx, tasks, f"📝 <b>Задачи и дедлайны{html.escape(suffix)}</b>",
                    "Задач со сроком нет 🎉", db(ctx).overdue(chat_id, now), inbox_footer(ctx, chat_id))


@restricted
async def cmd_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    items = db(ctx).inbox(chat_id)
    if not items:
        await update.message.reply_text(
            "📥 Во «входящих» пусто ✨\nСюда попадают задачи без срока, например: "
            "<code>задача: разобрать почту</code>", parse_mode=ParseMode.HTML)
        return
    lines = ["📥 <b>Входящие — задачи без срока</b>"] + grouped(ctx, items, chat_id)
    lines += ["", "Назначь срок кнопкой 📅 или отметь ✅, если уже сделано."]
    rows = [[InlineKeyboardButton(f"✅ {e.title[:24]}", callback_data=f"dn:{e.id}"),
             InlineKeyboardButton("📅", callback_data=f"pl:{e.id}"),
             InlineKeyboardButton("🗑", callback_data=f"del:{e.id}")] for e in items[:15]]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup(rows))


@restricted
async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Укажи номер задачи: /done 12 (номера видно в /tasks)")
        return
    event_id = int(ctx.args[0].lstrip("#"))
    if await done_and_notify(ctx, update.effective_chat.id, event_id):
        await update.message.reply_text(f"✅ #{event_id} выполнено — так держать! 💪")
    else:
        await update.message.reply_text(f"Не нашла невыполненную задачу #{event_id}")


@restricted
async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Укажи номер: /delete 12 (номера видно в /list)")
        return
    event_id = int(ctx.args[0].lstrip("#"))
    if await delete_and_notify(ctx, update.effective_chat.id, event_id):
        await update.message.reply_text(f"🗑 #{event_id} удалено")
    else:
        await update.message.reply_text(f"Не нашла #{event_id}")


async def delete_and_notify(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, event_id: int) -> bool:
    event = db(ctx).delete_event(chat_id, event_id)
    if event is None:
        return False
    me = db(ctx).get_user(chat_id)
    for member in event.members:
        if member != chat_id:
            await notify(ctx, member, f"🗑 {html.escape(me.name)} удалила: "
                                      f"{html.escape(event.title)} — {fmt_when(ctx, event, member)}")
    return True


async def done_and_notify(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, event_id: int) -> bool:
    event = db(ctx).mark_done(chat_id, event_id, datetime.now(timezone.utc))
    if event is None:
        return False
    me = db(ctx).get_user(chat_id)
    for member in event.members:
        if member != chat_id:
            await notify(ctx, member, f"✅ {html.escape(me.name)} выполнила: {html.escape(event.title)}")
    return True


# --- кнопки ---------------------------------------------------------------

@restricted
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    chat_id = update.effective_chat.id
    action, *rest = q.data.split(":", 1)
    arg = rest[0] if rest else ""

    if action == "del":
        ok = await delete_and_notify(ctx, chat_id, int(arg))
        await q.answer("Удалено" if ok else "Уже удалено")
        if ok:
            await q.message.reply_text(f"🗑 #{arg} удалено")

    elif action == "dn":
        ok = await done_and_notify(ctx, chat_id, int(arg))
        await q.answer("✅ Отлично!" if ok else "Уже отмечено")
        if ok:
            await q.message.reply_text(f"✅ #{arg} выполнено — так держать! 💪")

    elif action == "pl":
        event = db(ctx).get_event(int(arg))
        if not db(ctx).can_edit(chat_id, event):
            await q.answer("Задача уже удалена")
            return
        await q.answer()
        x = {"title": event.title, "day": None, "at": None, "source": None, "kind": "task",
             "tz": None, "area": event.area, "who": [], "plan_id": event.id}
        await start_batch(ctx, chat_id, q.message, [x])

    elif action == "snooze":
        event_id, minutes = map(int, arg.split(":"))
        at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        ok = db(ctx).snooze(chat_id, event_id, at)
        label = f"{minutes // 60} ч" if minutes >= 60 else f"{minutes} мин"
        await q.answer(f"Напомню через {label}" if ok else "Уже удалено")
        if ok:
            await q.edit_message_reply_markup(None)

    elif action == "done":
        await q.answer("👍")
        await q.edit_message_reply_markup(None)

    elif action == "ar":
        my_areas = user_areas(ctx, chat_id)
        rows = [[InlineKeyboardButton(a.label, callback_data=f"as:{arg}:{i}")] for i, a in enumerate(my_areas)]
        rows.append([InlineKeyboardButton("▫️ Без направления", callback_data=f"as:{arg}:-1")])
        await q.answer()
        await q.message.reply_text("🏷 Куда отнести?", reply_markup=InlineKeyboardMarkup(rows))

    elif action == "as":
        event_id, idx = map(int, arg.split(":"))
        my_areas = user_areas(ctx, chat_id)
        area = my_areas[idx].name if 0 <= idx < len(my_areas) else None
        ok = db(ctx).set_area(chat_id, event_id, area)
        await q.answer((f"Теперь: {my_areas[idx].label}" if area else "Без направления") if ok else "Уже удалено")
        if ok:
            await q.edit_message_reply_markup(None)

    elif action in ("sh", "shb"):
        if action == "sh":
            event_id, target = arg.split(":")
            event_ids = [int(event_id)]
        else:
            target = arg
            event_ids = json.loads(db(ctx).get_setting(chat_id, "last_batch") or "[]")
        await share(ctx, q, chat_id, event_ids, target)

    elif action == "q":
        await on_question_button(update, ctx, arg)

    elif action == "b":
        pending = load_pending(ctx, chat_id)
        if not pending:
            await q.answer("Это уже неактуально")
            return
        await q.edit_message_reply_markup(None)
        if arg == "cancel":
            save_pending(ctx, chat_id, None)
            await q.answer("Отменено")
            await q.message.reply_text("Хорошо, ничего не добавляю.")
            return
        if pending.get("ask") is not None:
            await q.answer("Сначала ответь на вопрос выше")
            return
        saved = await save_batch(ctx, chat_id, pending)
        await q.answer("Сохранено")
        shared = any(len(e.members) > 1 for e in saved)
        buttons = [] if shared else share_buttons(ctx, chat_id, "shb")
        await q.message.reply_text(
            f"✅ Сохранила: {len(saved)}. Напомню вовремя 🔔"
            + ("\nПоделиться всем этим:" if buttons else ""),
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )


async def on_question_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, arg: str) -> None:
    q = update.callback_query
    chat_id = update.effective_chat.id
    pending = load_pending(ctx, chat_id)
    if not pending or pending.get("ask") is None:
        await q.answer("Это уже неактуально")
        return
    x = pending["drafts"][pending["ask"]]
    kind, _, value = arg.partition(":")
    if kind == "skip":
        pending["drafts"].pop(pending["ask"])
    elif kind == "t":
        x["at"] = value
    elif kind == "d":
        x["day"] = (now_for(ctx, chat_id).date() + timedelta(days=int(value))).isoformat()
        x["source"] = "relative"
    elif kind == "inbox":
        x.update(kind="task", day=None, at=None, ask_day=False)
    await q.answer()
    await q.edit_message_reply_markup(None)
    await advance(ctx, chat_id, q.message, pending)


async def share(ctx: ContextTypes.DEFAULT_TYPE, q, chat_id: int, event_ids: list[int], target: str) -> None:
    family = {u.chat_id: u for u in others_in_family(ctx, chat_id)}
    targets = list(family) if target == "all" else [int(target)]
    if not targets or any(t not in family for t in targets):
        await q.answer("Этого человека нет в семье")
        return
    me = db(ctx).get_user(chat_id)
    added = 0
    for event_id in event_ids:
        event = db(ctx).get_event(event_id)
        if event is None or chat_id not in event.members:
            continue
        for t in targets:
            if db(ctx).add_member(event_id, t, reminder_times(ctx, event, t)):
                added += 1
                await notify(ctx, t, f"📌 {html.escape(me.name)} добавила для тебя: "
                                     f"{html.escape(event.title)} — {fmt_when(ctx, event, t)}")
    who = ", ".join(family[t].name for t in targets)
    await q.answer(f"Теперь это видит: {who}" if added else "Уже было общим")


# --- фоновые задачи -------------------------------------------------------

def deadline_phrase(ctx: ContextTypes.DEFAULT_TYPE, e: Event, chat_id: int) -> str:
    tz = tz_of(ctx, chat_id)
    now = datetime.now(tz)
    if not e.all_day and e.starts_at - now < timedelta(hours=12):
        return "Дедлайн " + fmt_left(e.starts_at - now).lower()
    days = (e.starts_at.astimezone(tz).date() - now.date()).days
    if days <= 0:
        return "Дедлайн сегодня"
    if days == 1:
        return "Дедлайн завтра"
    return f"Дедлайн через {days} {plural(days, 'день', 'дня', 'дней')}"


async def job_reminders(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage = db(ctx)
    now = datetime.now(timezone.utc)
    for r in storage.due_reminders(now):
        e = r.event
        left = e.starts_at - now
        if left < -timedelta(hours=1):
            # бот долго был выключен — о давно прошедшем не напоминаем
            storage.mark_sent(r.id)
            continue
        emoji = areas_mod.emoji_for(user_areas(ctx, r.chat_id), e.area)
        if e.is_task:
            text = (f"📝 <b>{deadline_phrase(ctx, e, r.chat_id)}:</b> {html.escape(e.title)}\n"
                    f"{emoji} {fmt_when(ctx, e, r.chat_id)}")
            buttons = [[
                InlineKeyboardButton("✅ Сделано", callback_data=f"dn:{e.id}"),
                InlineKeyboardButton("⏰ +3 ч", callback_data=f"snooze:{e.id}:180"),
                InlineKeyboardButton("👌 Ок", callback_data="done"),
            ]]
        else:
            text = (f"🔔 <b>{fmt_left(left)}:</b> {html.escape(e.title)}\n"
                    f"{emoji} {fmt_when(ctx, e, r.chat_id)}")
            buttons = [[
                InlineKeyboardButton("⏰ +10 мин", callback_data=f"snooze:{e.id}:10"),
                InlineKeyboardButton("⏰ +1 ч", callback_data=f"snooze:{e.id}:60"),
                InlineKeyboardButton("✅ Ок", callback_data="done"),
            ]]
        if e.creator_id != r.chat_id:
            creator = storage.get_user(e.creator_id)
            if creator:
                text += f"\n<i>👤 поставила {html.escape(creator.name)}</i>"
        try:
            await ctx.bot.send_message(r.chat_id, text, parse_mode=ParseMode.HTML,
                                       reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            log.exception("Не удалось отправить напоминание %s", r.id)
            continue
        storage.mark_sent(r.id)


def digest_text(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    storage = db(ctx)
    tz = tz_of(ctx, chat_id)
    now = datetime.now(tz)
    start = datetime.combine(now.date(), time(0), tz)
    today = storage.upcoming(chat_id, start, start + timedelta(days=1))
    overdue = storage.overdue(chat_id, start)
    soon = [e for e in storage.upcoming(chat_id, start + timedelta(days=1), start + timedelta(days=4))
            if e.is_task]
    if not (today or overdue or soon):
        text = "☀️ Доброе утро! На сегодня ничего не запланировано — отличный день для себя 🌸"
    else:
        lines = ["☀️ <b>Доброе утро! План на сегодня:</b>"]
        lines += grouped(ctx, today, chat_id) if today else ["", "Встреч и дедлайнов сегодня нет 🌿"]
        if overdue:
            lines += ["", "⚠️ <b>Просрочено</b>"] + [fmt_item(ctx, e, chat_id) for e in overdue]
        if soon:
            lines += ["", "⏳ <b>Скоро дедлайны</b>"] + [fmt_item(ctx, e, chat_id) for e in soon]
        text = "\n".join(lines)
    if footer := inbox_footer(ctx, chat_id):
        text += f"\n\n{footer}"
    return text


def weekly_text(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    storage = db(ctx)
    now = datetime.now(tz_of(ctx, chat_id))
    done = storage.done_since(chat_id, now - timedelta(days=7))
    overdue = storage.overdue(chat_id, now)
    upcoming = storage.upcoming(chat_id, now, now + timedelta(days=7))
    inbox = storage.inbox(chat_id)
    lines = ["🗓 <b>Обзор недели</b>", ""]
    if done:
        lines.append(f"✅ Сделано за неделю: <b>{len(done)}</b> — ты молодец! 💪")
        my_areas = user_areas(ctx, chat_id)
        counts: dict[str, int] = {}
        for e in done:
            counts[areas_mod.emoji_for(my_areas, e.area)] = counts.get(areas_mod.emoji_for(my_areas, e.area), 0) + 1
        lines.append("   " + "  ".join(f"{k} {v}" for k, v in counts.items()))
    else:
        lines.append("На этой неделе выполненных задач не отмечено — отмечай их кнопкой ✅")
    if overdue:
        lines += ["", "⚠️ <b>Хвосты — перенести или закрыть</b>"] + [fmt_item(ctx, e, chat_id) for e in overdue]
    lines += ["", "📅 <b>Что впереди на неделе</b>"]
    lines += grouped(ctx, upcoming, chat_id) if upcoming else ["Пока ничего — можно спокойно планировать ✨"]
    if inbox:
        lines += ["", f"📥 Во «входящих» {len(inbox)} {plural(len(inbox), 'задача', 'задачи', 'задач')} "
                      "без срока — загляни в /inbox и назначь сроки"]
    return "\n".join(lines)


async def job_digest(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage = db(ctx)
    for chat_id in storage.chats():
        now = datetime.now(tz_of(ctx, chat_id))
        today = now.date().isoformat()

        at = storage.get_setting(chat_id, "digest")
        if at and at != "off" and f"{now:%H:%M}" >= at and storage.get_setting(chat_id, "digest_sent") != today:
            storage.set_setting(chat_id, "digest_sent", today)
            await notify(ctx, chat_id, digest_text(ctx, chat_id))

        at = storage.get_setting(chat_id, "weekly")
        if (now.weekday() == 6 and at and at != "off" and f"{now:%H:%M}" >= at
                and storage.get_setting(chat_id, "weekly_sent") != today):
            storage.set_setting(chat_id, "weekly_sent", today)
            await notify(ctx, chat_id, weekly_text(ctx, chat_id))
    # заодно чистим прошедшие встречи и давно выполненные задачи
    storage.purge_past(datetime.now(timezone.utc) - timedelta(days=14))


# --- запуск ---------------------------------------------------------------

COMMANDS = [
    ("start", cmd_start), ("join", cmd_join), ("help", cmd_help), ("add", cmd_add),
    ("cancel", cmd_cancel), ("list", cmd_list), ("today", cmd_today), ("week", cmd_week),
    ("tasks", cmd_tasks), ("inbox", cmd_inbox), ("done", cmd_done), ("delete", cmd_delete),
    ("reminders", cmd_reminders), ("digest", cmd_digest), ("weekly", cmd_weekly),
    ("tz", cmd_tz), ("areas", cmd_areas), ("family", cmd_family), ("name", cmd_name),
]


def build_app(config: Config) -> Application:
    os.makedirs(os.path.dirname(config.db_path) or ".", exist_ok=True)
    app = Application.builder().token(config.token).build()
    app.bot_data["config"] = config
    app.bot_data["storage"] = Storage(config.db_path)
    app.bot_data["extractor"] = None
    if config.ai_enabled:
        from .ai import Extractor
        app.bot_data["extractor"] = Extractor(config.ai_model)

    for name, handler in COMMANDS:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(job_reminders, interval=30, first=5)
    app.job_queue.run_repeating(job_digest, interval=60, first=10)
    return app


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config()
    if not config.allowed:
        log.warning("ALLOWED_USER_IDS не задан — бот ответит любому пользователю!")
    if not config.ai_enabled:
        log.warning("ANTHROPIC_API_KEY не задан — фото и расписания распознаваться не будут")
    build_app(config).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
