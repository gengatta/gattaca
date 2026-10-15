"""Личный ассистент Катаржины в Telegram: хранит события и напоминает о них."""

from __future__ import annotations

import html
import logging
import os
from datetime import datetime, time, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .parser import ParseError, format_offset, parse_event, parse_offsets
from .storage import Event, Storage

log = logging.getLogger("assistant")

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

HELP = """Я — твой личный ассистент 💐
Напиши мне событие обычным текстом, и я напомню о нём заранее.

<b>Как добавить событие</b> — просто сообщением:
• <code>25.10 18:30 Стоматолог</code>
• <code>завтра в 10:00 созвон с Анной</code>
• <code>в пятницу 15:00 маникюр</code>
• <code>через 2 часа забрать посылку</code>
• <code>Йога 01.11.2026 в 19:00</code>

<b>Команды</b>
/list — ближайшие события
/today — что сегодня
/week — события на 7 дней
/delete <i>номер</i> — удалить событие
/reminders — когда напоминать (например <code>/reminders 1d, 2h, 15m</code>)
/digest — утренняя сводка (например <code>/digest 08:30</code> или <code>/digest off</code>)
/help — эта подсказка"""


class Config:
    def __init__(self) -> None:
        self.token = os.environ["BOT_TOKEN"]
        self.tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))
        self.allowed = {
            int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
        }
        self.default_offsets = os.getenv("DEFAULT_REMINDERS", "1d,1h,0m")
        self.default_digest = os.getenv("DIGEST_TIME", "08:00")
        self.db_path = os.getenv("DB_PATH", "data/assistant.db")
        parse_offsets(self.default_offsets)  # проверяем формат при старте


# --- вспомогательное ------------------------------------------------------

def cfg(ctx: ContextTypes.DEFAULT_TYPE) -> Config:
    return ctx.application.bot_data["config"]


def db(ctx: ContextTypes.DEFAULT_TYPE) -> Storage:
    return ctx.application.bot_data["storage"]


def now_local(ctx: ContextTypes.DEFAULT_TYPE) -> datetime:
    return datetime.now(cfg(ctx).tz).replace(microsecond=0)


def fmt_when(dt: datetime, tz: ZoneInfo, today: datetime | None = None) -> str:
    local = dt.astimezone(tz)
    today = (today or datetime.now(tz)).date()
    if local.date() == today:
        day = "сегодня"
    elif local.date() == today + timedelta(days=1):
        day = "завтра"
    else:
        day = f"{WEEKDAYS[local.weekday()]}, {local.day} {MONTHS[local.month - 1]}"
        if local.year != today.year:
            day += f" {local.year}"
    return f"{day} в {local:%H:%M}"


def fmt_left(delta: timedelta) -> str:
    minutes = round(delta.total_seconds() / 60)
    if minutes <= 0:
        return "Сейчас"
    if minutes < 60:
        return f"Через {minutes} мин"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"Через {hours} ч" + (f" {mins} мин" if mins else "")
    days = round(hours / 24)
    return f"Через {days} дн"


def fmt_event(e: Event, tz: ZoneInfo) -> str:
    return f"<b>#{e.id}</b> {html.escape(e.title)} — {fmt_when(e.starts_at, tz)}"


def offsets_for(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list[timedelta]:
    spec = db(ctx).get_setting(chat_id, "offsets") or cfg(ctx).default_offsets
    return parse_offsets(spec)


def restricted(handler):
    """Пускаем только пользователей из ALLOWED_USER_IDS (если список задан)."""

    @wraps(handler)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        allowed = cfg(ctx).allowed
        user = update.effective_user
        if allowed and (user is None or user.id not in allowed):
            log.warning("Отказано в доступе: user_id=%s", user.id if user else None)
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"Извини, я личный ассистент и работаю только для своей хозяйки.\n"
                    f"Твой id: {user.id if user else '?'}"
                )
            return
        return await handler(update, ctx)

    return wrapper


# --- команды --------------------------------------------------------------

@restricted
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if db(ctx).get_setting(chat_id, "digest") is None:
        db(ctx).set_setting(chat_id, "digest", cfg(ctx).default_digest)
        # первая сводка — завтра утром, а не сразу после знакомства
        db(ctx).set_setting(chat_id, "digest_sent", now_local(ctx).date().isoformat())
    name = update.effective_user.first_name or "Катаржина"
    await update.message.reply_text(
        f"Привет, {html.escape(name)}! 👋\n\n{HELP}", parse_mode=ParseMode.HTML
    )


@restricted
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


@restricted
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(ctx.args)
    if not text:
        await update.message.reply_text(
            "Напиши событие после команды, например:\n/add завтра 10:00 созвон"
        )
        return
    await add_event(update, ctx, text)


@restricted
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await add_event(update, ctx, update.message.text)


async def add_event(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = update.effective_chat.id
    now = now_local(ctx)
    try:
        parsed = parse_event(text, now)
    except ParseError as e:
        await update.message.reply_text(
            f"🤔 {e}.\nПопробуй так: <code>завтра в 18:30 Стоматолог</code>\nВсе примеры — /help",
            parse_mode=ParseMode.HTML,
        )
        return

    offsets = offsets_for(ctx, chat_id)
    event = db(ctx).add_event(chat_id, parsed.title, parsed.starts_at, offsets, now)
    planned = [format_offset(o) for o in offsets if parsed.starts_at - o > now]
    reminders = ", ".join(planned) if planned else "событие слишком скоро — напоминаний не будет"
    await update.message.reply_text(
        f"✅ Записала: {fmt_event(event, cfg(ctx).tz)}\n🔔 Напомню: {reminders}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩️ Отменить", callback_data=f"del:{event.id}")]]
        ),
    )


async def send_list(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, events: list[Event], title: str, empty: str
) -> None:
    tz = cfg(ctx).tz
    if not events:
        await update.message.reply_text(empty)
        return
    lines = [title, ""] + [fmt_event(e, tz) for e in events]
    buttons = [
        [InlineKeyboardButton(f"🗑 #{e.id} {e.title[:30]}", callback_data=f"del:{e.id}")]
        for e in events
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons)
    )


@restricted
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    events = db(ctx).upcoming(update.effective_chat.id, now_local(ctx))[:15]
    await send_list(update, ctx, events, "📅 <b>Ближайшие события</b>",
                    "Пока ничего не запланировано ✨")


@restricted
async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = now_local(ctx)
    end = datetime.combine(now.date() + timedelta(days=1), time(0), now.tzinfo)
    events = db(ctx).upcoming(update.effective_chat.id, now, end)
    await send_list(update, ctx, events, "☀️ <b>Сегодня</b>", "На сегодня больше ничего нет 🌿")


@restricted
async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = now_local(ctx)
    events = db(ctx).upcoming(update.effective_chat.id, now, now + timedelta(days=7))
    await send_list(update, ctx, events, "🗓 <b>На ближайшие 7 дней</b>",
                    "На неделю ничего не запланировано ✨")


@restricted
async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Укажи номер события: /delete 12 (номера видно в /list)")
        return
    event_id = int(ctx.args[0].lstrip("#"))
    if db(ctx).delete_event(update.effective_chat.id, event_id):
        await update.message.reply_text(f"🗑 Событие #{event_id} удалено")
    else:
        await update.message.reply_text(f"Не нашла событие #{event_id}")


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
        prefix = "Готово! Для новых событий буду напоминать"
    else:
        offsets = offsets_for(ctx, chat_id)
        prefix = "Сейчас напоминаю"
    await update.message.reply_text(
        f"🔔 {prefix}: {', '.join(format_offset(o) for o in offsets)}.\n\n"
        "Изменить: <code>/reminders 1d, 3h, 30m, 0m</code>\n"
        "(d — дни, h — часы, m — минуты, 0m — в момент начала)",
        parse_mode=ParseMode.HTML,
    )


@restricted
async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if ctx.args:
        arg = ctx.args[0].lower()
        if arg in ("off", "выкл", "нет"):
            db(ctx).set_setting(chat_id, "digest", "off")
            await update.message.reply_text("Утренняя сводка выключена")
            return
        try:
            t = datetime.strptime(arg, "%H:%M").time()
        except ValueError:
            await update.message.reply_text("Формат: /digest 08:30 или /digest off")
            return
        db(ctx).set_setting(chat_id, "digest", f"{t:%H:%M}")
        await update.message.reply_text(f"☀️ Буду присылать сводку на день в {t:%H:%M}")
        return
    current = db(ctx).get_setting(chat_id, "digest") or "off"
    state = "выключена" if current == "off" else f"приходит в {current}"
    await update.message.reply_text(
        f"Утренняя сводка {state}.\nИзменить: /digest 08:30, выключить: /digest off"
    )


# --- кнопки ---------------------------------------------------------------

@restricted
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    chat_id = update.effective_chat.id
    action, *rest = q.data.split(":")

    if action == "del":
        event_id = int(rest[0])
        ok = db(ctx).delete_event(chat_id, event_id)
        await q.answer("Удалено" if ok else "Уже удалено")
        if ok:
            await q.message.reply_text(f"🗑 Событие #{event_id} удалено")
    elif action == "snooze":
        event_id, minutes = int(rest[0]), int(rest[1])
        at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        ok = db(ctx).add_reminder(chat_id, event_id, at)
        await q.answer(f"Напомню через {minutes} мин" if ok else "Событие уже удалено")
        if ok:
            await q.edit_message_reply_markup(None)
    elif action == "done":
        await q.answer("👍")
        await q.edit_message_reply_markup(None)


# --- фоновые задачи -------------------------------------------------------

async def job_reminders(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage, tz = db(ctx), cfg(ctx).tz
    now = datetime.now(timezone.utc)
    for r in storage.due_reminders(now):
        e = r.event
        left = e.starts_at - now
        if left < -timedelta(hours=1):
            # бот долго был выключен — о давно прошедшем не напоминаем
            storage.mark_sent(r.id)
            continue
        text = (f"🔔 <b>{fmt_left(left)}:</b> {html.escape(e.title)}\n"
                f"🕒 {fmt_when(e.starts_at, tz)}")
        buttons = [[
            InlineKeyboardButton("⏰ +10 мин", callback_data=f"snooze:{e.id}:10"),
            InlineKeyboardButton("⏰ +1 ч", callback_data=f"snooze:{e.id}:60"),
            InlineKeyboardButton("✅ Ок", callback_data="done"),
        ]]
        try:
            await ctx.bot.send_message(e.chat_id, text, parse_mode=ParseMode.HTML,
                                       reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            log.exception("Не удалось отправить напоминание %s", r.id)
            continue
        storage.mark_sent(r.id)


async def job_digest(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage, tz = db(ctx), cfg(ctx).tz
    now = datetime.now(tz)
    today = now.date().isoformat()
    for chat_id in storage.chats():
        at = storage.get_setting(chat_id, "digest")
        if not at or at == "off" or f"{now:%H:%M}" < at:
            continue
        if storage.get_setting(chat_id, "digest_sent") == today:
            continue
        storage.set_setting(chat_id, "digest_sent", today)
        start = datetime.combine(now.date(), time(0), tz)
        events = storage.upcoming(chat_id, start, start + timedelta(days=1))
        if events:
            lines = [f"• {e.starts_at.astimezone(tz):%H:%M} — {html.escape(e.title)}"
                     for e in events]
            text = "☀️ <b>Доброе утро! Сегодня в планах:</b>\n\n" + "\n".join(lines)
        else:
            text = "☀️ Доброе утро! На сегодня ничего не запланировано — отличный день для себя 🌸"
        try:
            await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            log.exception("Не удалось отправить сводку в чат %s", chat_id)
    # заодно чистим события, прошедшие больше недели назад
    storage.purge_past(datetime.now(timezone.utc) - timedelta(days=7))


# --- запуск ---------------------------------------------------------------

def build_app(config: Config) -> Application:
    os.makedirs(os.path.dirname(config.db_path) or ".", exist_ok=True)
    app = Application.builder().token(config.token).build()
    app.bot_data["config"] = config
    app.bot_data["storage"] = Storage(config.db_path)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CallbackQueryHandler(on_button))
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
    build_app(config).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
