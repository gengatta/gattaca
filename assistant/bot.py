"""Личный ассистент Катаржины в Telegram: хранит события и напоминает о них.

Событие можно прислать текстом, целым расписанием или фото записей. Если в записи
не хватает даты или времени — бот уточняет. Членов семьи можно пригласить, чтобы
ставить события друг другу и получать общие напоминания.
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

from .parser import Draft, ParseError, extract, looks_like_schedule, finalize, format_offset, parse_offsets
from .storage import Event, Storage, User

log = logging.getLogger("assistant")

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

HELP = """Я — твой личный ассистент 💐
Пришли мне событие, расписание или фото своих записей — я всё запишу и напомню заранее.

<b>Одно событие</b> — просто сообщением:
• <code>25.10 18:30 Стоматолог</code>
• <code>завтра в 10:00 созвон с Анной</code>
• <code>через 2 часа забрать посылку</code>

<b>Расписание</b> — несколько строк или обычным текстом:
<code>пн 9:00 бассейн
ср в полчетвёртого маникюр
в субботу у мамы день рождения</code>

<b>Фото</b> — сфотографируй ежедневник или листок с планами 📸
Если где-то не хватит даты или времени, я переспрошу.

<b>Команды</b>
/list — ближайшие события
/today — что сегодня
/week — события на 7 дней
/delete <i>номер</i> — удалить событие
/reminders — когда напоминать (например <code>/reminders 1d, 2h, 15m</code>)
/digest — утренняя сводка (например <code>/digest 08:30</code> или <code>/digest off</code>)
/family — семья: пригласить маму, ставить события друг другу
/name — как тебя называть в семье (например <code>/name Мама</code>)
/cancel — отменить незаконченное добавление"""


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
        self.ai_model = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
        self.ai_enabled = bool(os.getenv("ANTHROPIC_API_KEY"))
        parse_offsets(self.default_offsets)  # проверяем формат при старте


# --- вспомогательное ------------------------------------------------------

def cfg(ctx: ContextTypes.DEFAULT_TYPE) -> Config:
    return ctx.application.bot_data["config"]


def db(ctx: ContextTypes.DEFAULT_TYPE) -> Storage:
    return ctx.application.bot_data["storage"]


def now_local(ctx: ContextTypes.DEFAULT_TYPE) -> datetime:
    return datetime.now(cfg(ctx).tz).replace(second=0, microsecond=0)


def fmt_when(dt: datetime, tz: ZoneInfo, today: date | None = None) -> str:
    local = dt.astimezone(tz)
    today = today or datetime.now(tz).date()
    if local.date() == today:
        day = "сегодня"
    elif local.date() == today + timedelta(days=1):
        day = "завтра"
    else:
        day = f"{WEEKDAYS[local.weekday()]}, {local.day} {MONTHS[local.month - 1]}"
        if local.year != today.year:
            day += f" {local.year}"
    return f"{day} в {local:%H:%M}"


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


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


def fmt_event(ctx: ContextTypes.DEFAULT_TYPE, e: Event, viewer: int) -> str:
    line = f"<b>#{e.id}</b> {html.escape(e.title)} — {fmt_when(e.starts_at, cfg(ctx).tz)}"
    extra = []
    if e.creator_id != viewer:
        creator = db(ctx).get_user(e.creator_id)
        if creator:
            extra.append(f"👤 {html.escape(creator.name)}")
    if len(e.members) > 1:
        extra.append("👥 " + html.escape(names(ctx, e.members)))
    return line + (f" <i>({'; '.join(extra)})</i>" if extra else "")


def offsets_for(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list[timedelta]:
    spec = db(ctx).get_setting(chat_id, "offsets") or cfg(ctx).default_offsets
    return parse_offsets(spec)


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


# --- черновики и уточняющие вопросы ------------------------------------------
#
# Незаконченное добавление хранится в настройке "pending" как JSON:
#   {"drafts": [{"title", "day", "at", "source", "who"}...], "ask": индекс | null}
# "who" — chat_id участников, кроме самого автора.

def draft_to_dict(d: Draft, who: list[int] | None = None) -> dict:
    if d.starts_at:
        day, at, source = d.starts_at.date(), d.starts_at.time(), "exact"
    else:
        day, at, source = d.day, d.at, d.source
    return {
        "title": d.title,
        "day": day.isoformat() if day else None,
        "at": at.strftime("%H:%M") if at else None,
        "source": source,
        "who": who or [],
    }


def dict_to_draft(x: dict) -> Draft:
    return Draft(
        title=x["title"],
        day=date.fromisoformat(x["day"]) if x["day"] else None,
        at=datetime.strptime(x["at"], "%H:%M").time() if x["at"] else None,
        source=x.get("source"),
    )


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


async def advance(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, msg: Message, pending: dict) -> None:
    """Задаёт следующий уточняющий вопрос или показывает итог на подтверждение."""
    now = now_local(ctx)
    drafts = pending["drafts"]

    for i, x in enumerate(drafts):
        d = dict_to_draft(x)
        missing = d.missing
        if missing is None and x.get("ask_day") and not x["day"]:
            missing = "date"  # на фото/в расписании было только время — день уточняем
        if missing is None:
            continue
        pending["ask"] = i
        save_pending(ctx, chat_id, pending)
        title = html.escape(d.title or "событие")
        if missing == "time":
            text = f"🕒 Во сколько <b>{title}</b> ({fmt_day(d.day)})?\nНапиши время, например <code>18:30</code>"
            buttons = [[InlineKeyboardButton("Без точного времени (09:00)", callback_data="q:t:09:00")]]
        else:
            when = f" в {d.at:%H:%M}" if d.at else ""
            text = (f"📅 Когда <b>{title}</b>{when}?\n"
                    "Напиши дату, например <code>25.10</code>, <code>в пятницу</code> или <code>завтра 18:30</code>")
            buttons = [[InlineKeyboardButton("Сегодня", callback_data="q:d:0"),
                        InlineKeyboardButton("Завтра", callback_data="q:d:1")]]
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
        text = "Все эти события уже прошли — нечего добавлять 🙂" if past else "Нечего добавлять."
        await msg.reply_text(text)
        return

    save_pending(ctx, chat_id, pending)
    lines = ["📝 <b>Вот что я поняла:</b>", ""]
    for x, starts_at in sorted(ready, key=lambda r: r[1]):
        line = f"• {html.escape(x['title'] or 'Событие')} — {fmt_when(starts_at, cfg(ctx).tz)}"
        if x["who"]:
            line += f" <i>(👥 {html.escape(names(ctx, [chat_id, *x['who']]))})</i>"
        lines.append(line)
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
        reply = extract(update.message.text, now_local(ctx))
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
    if not x["title"] and reply.title:
        x["title"] = reply.title
    await advance(ctx, chat_id, update.message, pending)


async def save_batch(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, pending: dict) -> list[Event]:
    now = now_local(ctx)
    me = db(ctx).get_user(chat_id)
    saved = []
    for x in pending["drafts"]:
        try:
            starts_at = finalize(dict_to_draft(x), now)
        except ParseError:
            continue
        event = db(ctx).add_event(chat_id, x["title"] or "Событие", starts_at, now)
        for member in [chat_id, *x["who"]]:
            db(ctx).add_member(event.id, member, offsets_for(ctx, member), now)
        for member in x["who"]:
            await notify(ctx, member, f"📌 {html.escape(me.name)} добавила для тебя: "
                                      f"{html.escape(event.title)} — {fmt_when(event.starts_at, cfg(ctx).tz)}")
        saved.append(db(ctx).get_event(event.id))
    save_pending(ctx, chat_id, None)
    db(ctx).set_setting(chat_id, "last_batch", json.dumps([e.id for e in saved]))
    return saved


async def notify(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    try:
        await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Не удалось отправить уведомление в чат %s", chat_id)


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


# --- команды --------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Приглашение по ссылке t.me/<бот>?start=join_<код> — до проверки доступа
    if ctx.args and ctx.args[0].startswith("join_"):
        await do_join(update, ctx, ctx.args[0][5:])
        return
    await start_known(update, ctx)


@restricted
async def start_known(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if db(ctx).get_setting(chat_id, "digest") is None:
        db(ctx).set_setting(chat_id, "digest", cfg(ctx).default_digest)
        # первая сводка — завтра утром, а не сразу после знакомства
        db(ctx).set_setting(chat_id, "digest_sent", now_local(ctx).date().isoformat())
    name = db(ctx).get_user(chat_id).name
    await update.message.reply_text(
        f"Привет, {html.escape(name)}! 👋\n\n{HELP}", parse_mode=ParseMode.HTML
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
    chat_id = update.effective_chat.id
    if storage.get_setting(chat_id, "digest") is None:
        storage.set_setting(chat_id, "digest", cfg(ctx).default_digest)
        storage.set_setting(chat_id, "digest_sent", now_local(ctx).date().isoformat())
    members = storage.family_members(user.id)
    for m in members:
        if m.chat_id != user.id:
            await notify(ctx, m.chat_id, f"👋 {html.escape(user.first_name or 'Новый участник')} "
                                         "присоединился(-ась) к семье")
    await update.message.reply_text(
        f"🎉 Добро пожаловать в семью! Сейчас в ней: {html.escape(', '.join(m.name for m in members))}.\n\n"
        "Как тебя называть? Например: <code>/name Мама</code> — так другим будет понятнее, "
        f"для кого событие.\n\n{HELP}",
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
        "Что дальше: в сообщениях можно писать, для кого событие "
        "(«маме к врачу в пт 10:00»), а под каждым событием будут кнопки «+ Мама».\n"
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


@restricted
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


@restricted
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    save_pending(ctx, update.effective_chat.id, None)
    await update.message.reply_text("Хорошо, ничего не добавляю.")


@restricted
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(ctx.args)
    if not text:
        await update.message.reply_text(
            "Напиши событие после команды, например:\n/add завтра 10:00 созвон"
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


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = update.effective_chat.id
    now = now_local(ctx)
    single_line = len([ln for ln in text.splitlines() if ln.strip()]) == 1

    try:
        draft = extract(text, now)
    except ParseError as e:
        draft, error = None, e
    else:
        error = None

    # Быстрый путь: одно понятное событие — сохраняем сразу, без нейросети
    if draft and single_line and not looks_like_schedule(text) and draft.title and draft.missing is None:
        try:
            starts_at = finalize(draft, now)
        except ParseError as e:
            await update.message.reply_text(f"🤔 {e}.")
            return
        await save_single(update, ctx, draft.title, starts_at)
        return

    # Расписание, свободный текст или неполная запись — разбираем через Claude
    if cfg(ctx).ai_enabled:
        await recognize(update, ctx, text=text)
        return

    # Без Claude: одно событие, которому не хватает даты/времени, — переспрашиваем
    if draft and single_line and draft.title and draft.found_anything:
        await start_batch(ctx, chat_id, update.message, [draft_to_dict(draft)])
        return
    reason = str(error) if error else (
        "Пришли, пожалуйста, по одному событию в сообщении" if not single_line
        else "Не нашла дату или время")
    await update.message.reply_text(
        f"🤔 {reason}.\nПопробуй так: <code>завтра в 18:30 Стоматолог</code>\nВсе примеры — /help",
        parse_mode=ParseMode.HTML,
    )


async def save_single(update: Update, ctx: ContextTypes.DEFAULT_TYPE, title: str, starts_at: datetime) -> None:
    chat_id = update.effective_chat.id
    now = now_local(ctx)
    offsets = offsets_for(ctx, chat_id)
    event = db(ctx).add_event(chat_id, title, starts_at, now)
    db(ctx).add_member(event.id, chat_id, offsets, now)
    planned = [format_offset(o) for o in offsets if starts_at - o > now]
    reminders = ", ".join(planned) if planned else "событие слишком скоро — напоминаний не будет"
    buttons = share_buttons(ctx, chat_id, f"sh:{event.id}")
    buttons.append([InlineKeyboardButton("↩️ Отменить", callback_data=f"del:{event.id}")])
    await update.message.reply_text(
        f"✅ Записала: {fmt_event(ctx, event, chat_id)}\n🔔 Напомню: {reminders}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@restricted
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not cfg(ctx).ai_enabled:
        await update.message.reply_text(
            "📸 Распознавание фото пока не подключено (нужен ANTHROPIC_API_KEY). "
            "Напиши событие текстом, например: завтра 10:00 созвон"
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
    by_name: dict[str, int] = {}
    for u in family:
        by_name.setdefault(u.name, u.chat_id)

    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    status = await update.message.reply_text("⏳ Читаю..." if image else "⏳ Разбираю...")
    try:
        result = await extractor.extract(
            now_local(ctx), list(by_name) if len(family) > 1 else [],
            author=db(ctx).get_user(chat_id).name, text=text, image=image, image_type=image_type,
        )
    except AIError as e:
        await status.edit_text(f"😔 {e}")
        return
    await status.delete()

    if not result.events:
        hint = f"\n<i>{html.escape(result.comment)}</i>" if result.comment else ""
        await update.message.reply_text(
            "🤔 Не нашла здесь событий. Попробуй так: <code>завтра в 18:30 Стоматолог</code>" + hint,
            parse_mode=ParseMode.HTML,
        )
        return

    drafts = []
    for ev in result.events:
        who = [by_name[w] for w in ev.for_whom if by_name.get(w) not in (None, chat_id)]
        d = Draft(ev.title, day=ev.day, at=ev.at, source="exact" if ev.day else None)
        drafts.append(draft_to_dict(d, sorted(set(who))) | {"ask_day": ev.day is None})
    await start_batch(ctx, chat_id, update.message, drafts, note=result.comment)


async def send_list(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, events: list[Event], title: str, empty: str
) -> None:
    chat_id = update.effective_chat.id
    if not events:
        await update.message.reply_text(empty)
        return
    lines = [title, ""] + [fmt_event(ctx, e, chat_id) for e in events]
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
    if await delete_and_notify(ctx, update.effective_chat.id, event_id):
        await update.message.reply_text(f"🗑 Событие #{event_id} удалено")
    else:
        await update.message.reply_text(f"Не нашла событие #{event_id}")


async def delete_and_notify(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, event_id: int) -> bool:
    event = db(ctx).delete_event(chat_id, event_id)
    if event is None:
        return False
    me = db(ctx).get_user(chat_id)
    for member in event.members:
        if member != chat_id:
            await notify(ctx, member, f"🗑 {html.escape(me.name)} удалила событие: "
                                      f"{html.escape(event.title)} — {fmt_when(event.starts_at, cfg(ctx).tz)}")
    return True


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
    action, *rest = q.data.split(":", 1)
    arg = rest[0] if rest else ""

    if action == "del":
        ok = await delete_and_notify(ctx, chat_id, int(arg))
        await q.answer("Удалено" if ok else "Уже удалено")
        if ok:
            await q.message.reply_text(f"🗑 Событие #{arg} удалено")

    elif action == "snooze":
        event_id, minutes = map(int, arg.split(":"))
        at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        ok = db(ctx).snooze(chat_id, event_id, at)
        await q.answer(f"Напомню через {minutes} мин" if ok else "Событие уже удалено")
        if ok:
            await q.edit_message_reply_markup(None)

    elif action == "done":
        await q.answer("👍")
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
            f"✅ Сохранила {len(saved)} — напомню заранее 🔔"
            + ("\nПоделиться всеми этими событиями:" if buttons else ""),
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
        x["day"] = (now_local(ctx).date() + timedelta(days=int(value))).isoformat()
        x["source"] = "relative"
    await q.answer()
    await q.edit_message_reply_markup(None)
    await advance(ctx, chat_id, q.message, pending)


async def share(ctx: ContextTypes.DEFAULT_TYPE, q, chat_id: int, event_ids: list[int], target: str) -> None:
    family = {u.chat_id: u for u in others_in_family(ctx, chat_id)}
    targets = list(family) if target == "all" else [int(target)]
    if not targets or any(t not in family for t in targets):
        await q.answer("Этого человека нет в семье")
        return
    now = now_local(ctx)
    me = db(ctx).get_user(chat_id)
    added = 0
    for event_id in event_ids:
        event = db(ctx).get_event(event_id)
        if event is None or chat_id not in event.members:
            continue
        for t in targets:
            if db(ctx).add_member(event_id, t, offsets_for(ctx, t), now):
                added += 1
                await notify(ctx, t, f"📌 {html.escape(me.name)} добавила для тебя: "
                                     f"{html.escape(event.title)} — {fmt_when(event.starts_at, cfg(ctx).tz)}")
    who = ", ".join(family[t].name for t in targets)
    await q.answer(f"Теперь это видит: {who}" if added else "Уже было общим")


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
        if e.creator_id != r.chat_id:
            creator = storage.get_user(e.creator_id)
            if creator:
                text += f"\n<i>👤 поставила {html.escape(creator.name)}</i>"
        buttons = [[
            InlineKeyboardButton("⏰ +10 мин", callback_data=f"snooze:{e.id}:10"),
            InlineKeyboardButton("⏰ +1 ч", callback_data=f"snooze:{e.id}:60"),
            InlineKeyboardButton("✅ Ок", callback_data="done"),
        ]]
        try:
            await ctx.bot.send_message(r.chat_id, text, parse_mode=ParseMode.HTML,
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
        await notify(ctx, chat_id, text)
    # заодно чистим события, прошедшие больше недели назад
    storage.purge_past(datetime.now(timezone.utc) - timedelta(days=7))


# --- запуск ---------------------------------------------------------------

def build_app(config: Config) -> Application:
    os.makedirs(os.path.dirname(config.db_path) or ".", exist_ok=True)
    app = Application.builder().token(config.token).build()
    app.bot_data["config"] = config
    app.bot_data["storage"] = Storage(config.db_path)
    app.bot_data["extractor"] = None
    if config.ai_enabled:
        from .ai import Extractor
        app.bot_data["extractor"] = Extractor(config.ai_model)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("family", cmd_family))
    app.add_handler(CommandHandler("name", cmd_name))
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
