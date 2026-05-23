#!/usr/bin/env python3
"""
TaskBot — совместный таск-менеджер для пары (Тима и Маша)
Хранение: SQLite | Уведомления: APScheduler | Фреймворк: python-telegram-bot v20
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ─── Настройки ───────────────────────────────────────────────────────────────

import os

BOT_TOKEN = os.environ["BOT_TOKEN"].strip().strip('"').strip("'")

# Telegram user_id каждого партнёра — заполни после /start
USERS = {
    "Тима": 6196513270,
    "Маша": 478976611,
}

TZ = ZoneInfo("Europe/Madrid")  # Часовой пояс (Валенсия)
DB_PATH = Path("tasks.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── Состояния ConversationHandler ───────────────────────────────────────────

(
    STATE_CATEGORY,
    STATE_TEXT,
    STATE_ASSIGNEE,
    STATE_DEADLINE_CHOICE,
    STATE_DEADLINE_INPUT,
) = range(5)

CATEGORIES = [
    ("🛒", "Покупки"),
    ("🏠", "Дом"),
    ("💳", "Оплаты"),
    ("🏥", "Здоровье"),
    ("🎉", "Досуг"),
    ("📝", "Другое"),
]

# ─── База данных ──────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                creator     TEXT    NOT NULL,
                assignee    TEXT    NOT NULL,
                category    TEXT    NOT NULL,
                text        TEXT    NOT NULL,
                deadline    TEXT,           -- ISO datetime строка или NULL
                done        INTEGER DEFAULT 0,
                created_at  TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def add_task(creator: str, assignee: str, category: str, text: str, deadline: str | None) -> int:
    with db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (creator, assignee, category, text, deadline) VALUES (?,?,?,?,?)",
            (creator, assignee, category, text, deadline),
        )
        conn.commit()
        return cur.lastrowid


def get_tasks_for(assignee: str) -> list[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE assignee=? AND done=0 ORDER BY deadline IS NULL, deadline ASC",
            (assignee,),
        ).fetchall()


def get_task(task_id: int) -> sqlite3.Row | None:
    with db_connect() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def complete_task(task_id: int):
    with db_connect() as conn:
        conn.execute("UPDATE tasks SET done=1 WHERE id=?", (task_id,))
        conn.commit()


def delete_task(task_id: int):
    with db_connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()


def get_overdue_and_upcoming() -> list[sqlite3.Row]:
    """Все невыполненные задачи с дедлайном для проверки уведомлений."""
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE done=0 AND deadline IS NOT NULL"
        ).fetchall()

# ─── Вспомогательные функции ─────────────────────────────────────────────────

def who_am_i(user_id: int) -> str | None:
    for name, uid in USERS.items():
        if uid == user_id:
            return name
    return None


def other_person(name: str) -> str:
    return "Маша" if name == "Тима" else "Тима"


def fmt_deadline(dl: str | None) -> str:
    if not dl:
        return "без дедлайна"
    dt = datetime.fromisoformat(dl).astimezone(TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


def deadline_status(dl: str | None) -> str:
    if not dl:
        return ""
    now = datetime.now(TZ)
    dt = datetime.fromisoformat(dl).astimezone(TZ)
    diff = dt - now
    if diff.total_seconds() < 0:
        return "🔴 ПРОСРОЧЕНО"
    elif diff.days == 0:
        hours = int(diff.total_seconds() // 3600)
        return f"🟡 сегодня (~{hours}ч)"
    elif diff.days <= 2:
        return f"🟠 через {diff.days} дн."
    else:
        return f"🟢 через {diff.days} дн."


def task_line(t: sqlite3.Row) -> str:
    cat_emoji = next((e for e, n in CATEGORIES if n == t["category"]), "📝")
    status = deadline_status(t["deadline"])
    dl = fmt_deadline(t["deadline"])
    return (
        f"{cat_emoji} <b>{t['text']}</b>\n"
        f"   📅 {dl}  {status}\n"
        f"   👤 от {t['creator']}  •  #id{t['id']}"
    )


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Новая задача", callback_data="new_task"),
            InlineKeyboardButton("📋 Мои задачи",   callback_data="my_tasks"),
        ],
        [
            InlineKeyboardButton("🗑 Удалить свою задачу", callback_data="delete_mine"),
        ],
    ])

# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = who_am_i(user_id)

    if name:
        await update.message.reply_text(
            f"Привет, <b>{name}</b>! 👋\nЧто будем делать?",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
    else:
        await update.message.reply_text(
            f"Твой Telegram ID: <code>{user_id}</code>\n"
            "Добавь его в переменную <code>USERS</code> в боте и перезапусти.",
            parse_mode="HTML",
        )

# ─── Главное меню (callback) ──────────────────────────────────────────────────

async def cb_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = who_am_i(q.from_user.id)
    if not name:
        await q.edit_message_text("Ты не зарегистрирован в боте.")
        return
    await q.edit_message_text(
        f"Привет, <b>{name}</b>! Что будем делать?",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )

# ─── Создание задачи ──────────────────────────────────────────────────────────

async def cb_new_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = who_am_i(q.from_user.id)
    if not name:
        await q.edit_message_text("Ты не зарегистрирован.")
        return ConversationHandler.END

    ctx.user_data.clear()
    ctx.user_data["creator"] = name

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{e} {n}", callback_data=f"cat_{n}") for e, n in CATEGORIES[:3]],
        [InlineKeyboardButton(f"{e} {n}", callback_data=f"cat_{n}") for e, n in CATEGORIES[3:]],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")],
    ])
    await q.edit_message_text("Выбери категорию задачи:", reply_markup=kb)
    return STATE_CATEGORY


async def cb_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cat = q.data.split("_", 1)[1]
    ctx.user_data["category"] = cat
    await q.edit_message_text(
        f"Категория: <b>{cat}</b>\n\nНапиши текст задачи:",
        parse_mode="HTML",
    )
    return STATE_TEXT


async def msg_task_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["text"] = update.message.text.strip()
    name = ctx.user_data["creator"]
    other = other_person(name)

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"👤 {name} (я)",  callback_data=f"assign_{name}"),
            InlineKeyboardButton(f"👤 {other}",       callback_data=f"assign_{other}"),
        ],
        [InlineKeyboardButton("⬅️ Отмена", callback_data="cancel")],
    ])
    await update.message.reply_text(
        f"Задача: <b>{ctx.user_data['text']}</b>\n\nКому назначить?",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return STATE_ASSIGNEE


async def cb_assignee(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    assignee = q.data.split("_", 1)[1]
    ctx.user_data["assignee"] = assignee

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Указать дедлайн", callback_data="dl_yes"),
            InlineKeyboardButton("⏩ Без дедлайна",    callback_data="dl_no"),
        ],
        [InlineKeyboardButton("⬅️ Отмена", callback_data="cancel")],
    ])
    await q.edit_message_text(
        f"Назначено: <b>{assignee}</b>\n\nУстановить дедлайн?",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return STATE_DEADLINE_CHOICE


async def cb_deadline_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["deadline"] = None
    return await _save_task(q, ctx)


async def cb_deadline_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "Введи дедлайн в формате:\n<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\nПример: <code>25.05.2026 18:00</code>",
        parse_mode="HTML",
    )
    return STATE_DEADLINE_INPUT


async def msg_deadline_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        dt = datetime.strptime(raw, "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
        ctx.user_data["deadline"] = dt.isoformat()
    except ValueError:
        await update.message.reply_text(
            "Неверный формат. Попробуй ещё раз:\n<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>",
            parse_mode="HTML",
        )
        return STATE_DEADLINE_INPUT

    return await _save_task_msg(update.message, ctx)


async def _save_task(q, ctx):
    d = ctx.user_data
    task_id = add_task(d["creator"], d["assignee"], d["category"], d["text"], d["deadline"])

    # Уведомить исполнителя если это не создатель
    if d["creator"] != d["assignee"]:
        assignee_id = USERS.get(d["assignee"])
        if assignee_id:
            dl_str = fmt_deadline(d["deadline"])
            cat_emoji = next((e for e, n in CATEGORIES if n == d["category"]), "📝")
            await ctx.bot.send_message(
                chat_id=assignee_id,
                text=(
                    f"📌 <b>Новая задача от {d['creator']}!</b>\n\n"
                    f"{cat_emoji} <b>{d['text']}</b>\n"
                    f"📅 Дедлайн: {dl_str}\n\n"
                    f"Открой список задач: /start"
                ),
                parse_mode="HTML",
            )

    await q.edit_message_text(
        f"✅ Задача #{task_id} создана!\n\n"
        f"<b>{d['text']}</b>\n"
        f"👤 Для: {d['assignee']} • 📅 {fmt_deadline(d['deadline'])}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]),
    )
    return ConversationHandler.END


async def _save_task_msg(msg, ctx):
    d = ctx.user_data
    task_id = add_task(d["creator"], d["assignee"], d["category"], d["text"], d["deadline"])

    if d["creator"] != d["assignee"]:
        assignee_id = USERS.get(d["assignee"])
        if assignee_id:
            dl_str = fmt_deadline(d["deadline"])
            cat_emoji = next((e for e, n in CATEGORIES if n == d["category"]), "📝")
            await ctx.bot.send_message(
                chat_id=assignee_id,
                text=(
                    f"📌 <b>Новая задача от {d['creator']}!</b>\n\n"
                    f"{cat_emoji} <b>{d['text']}</b>\n"
                    f"📅 Дедлайн: {dl_str}\n\n"
                    f"Открой список: /start"
                ),
                parse_mode="HTML",
            )

    await msg.reply_text(
        f"✅ Задача #{task_id} создана!\n\n"
        f"<b>{d['text']}</b>\n"
        f"👤 Для: {d['assignee']} • 📅 {fmt_deadline(d['deadline'])}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]),
    )
    return ConversationHandler.END

# ─── Мои задачи ───────────────────────────────────────────────────────────────

async def cb_my_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = who_am_i(q.from_user.id)
    if not name:
        await q.edit_message_text("Ты не зарегистрирован.")
        return

    tasks = get_tasks_for(name)
    if not tasks:
        await q.edit_message_text(
            f"У тебя нет активных задач, <b>{name}</b>! 🎉",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
            ]),
        )
        return

    # Показываем задачи с чекбоксами
    ctx.user_data["pending_complete"] = set()
    rows = []
    for t in tasks:
        rows.append([
            InlineKeyboardButton(
                f"☐ #{t['id']}",
                callback_data=f"toggle_{t['id']}",
            )
        ])
    rows.append([
        InlineKeyboardButton("✅ Подтвердить выполнение", callback_data="confirm_complete"),
        InlineKeyboardButton("🏠 Меню", callback_data="main_menu"),
    ])

    lines = "\n\n".join(task_line(t) for t in tasks)
    await q.edit_message_text(
        f"📋 <b>Задачи для {name}:</b>\n\n{lines}\n\n"
        "Отметь выполненные ☑ и нажми «Подтвердить»:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_toggle_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    task_id = int(q.data.split("_")[1])
    name = who_am_i(q.from_user.id)

    pending = ctx.user_data.get("pending_complete", set())
    if task_id in pending:
        pending.discard(task_id)
    else:
        pending.add(task_id)
    ctx.user_data["pending_complete"] = pending

    tasks = get_tasks_for(name)
    rows = []
    for t in tasks:
        checked = "☑" if t["id"] in pending else "☐"
        rows.append([
            InlineKeyboardButton(
                f"{checked} #{t['id']} {t['text'][:30]}",
                callback_data=f"toggle_{t['id']}",
            )
        ])
    rows.append([
        InlineKeyboardButton("✅ Подтвердить выполнение", callback_data="confirm_complete"),
        InlineKeyboardButton("🏠 Меню", callback_data="main_menu"),
    ])

    lines = "\n\n".join(task_line(t) for t in tasks)
    await q.edit_message_text(
        f"📋 <b>Задачи для {name}:</b>\n\n{lines}\n\n"
        "Отметь выполненные ☑ и нажми «Подтвердить»:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_confirm_complete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = who_am_i(q.from_user.id)
    pending = ctx.user_data.get("pending_complete", set())

    if not pending:
        await q.answer("Ничего не отмечено!", show_alert=True)
        return

    completed = []
    for task_id in pending:
        t = get_task(task_id)
        if t and t["assignee"] == name:
            complete_task(task_id)
            completed.append(t)

            # Уведомить создателя
            creator_id = USERS.get(t["creator"])
            if creator_id and t["creator"] != name:
                await ctx.bot.send_message(
                    chat_id=creator_id,
                    text=(
                        f"✅ <b>{name}</b> выполнил(а) задачу!\n\n"
                        f"<b>{t['text']}</b>"
                    ),
                    parse_mode="HTML",
                )

    ctx.user_data["pending_complete"] = set()
    names_done = "\n".join(f"• {t['text']}" for t in completed)
    await q.edit_message_text(
        f"🎉 Выполнено {len(completed)} задач:\n{names_done}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]),
    )

# ─── Удаление своих задач ─────────────────────────────────────────────────────

async def cb_delete_mine(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = who_am_i(q.from_user.id)
    if not name:
        return

    with db_connect() as conn:
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE creator=? AND done=0 ORDER BY created_at DESC",
            (name,),
        ).fetchall()

    if not tasks:
        await q.edit_message_text(
            "У тебя нет активных задач, которые можно удалить.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
            ]),
        )
        return

    rows = []
    for t in tasks:
        label = f"🗑 #{t['id']} → {t['assignee']}: {t['text'][:35]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"del_{t['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")])

    await q.edit_message_text(
        "Выбери задачу для удаления:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_del_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    task_id = int(q.data.split("_")[1])
    name = who_am_i(q.from_user.id)

    t = get_task(task_id)
    if t and t["creator"] == name:
        delete_task(task_id)
        msg = f"🗑 Задача <b>«{t['text']}»</b> удалена."
    else:
        msg = "Нельзя удалить эту задачу."

    await q.edit_message_text(
        msg,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]),
    )

# ─── Отмена ConversationHandler ───────────────────────────────────────────────

async def cb_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.clear()
    await q.edit_message_text(
        "Отменено.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]),
    )
    return ConversationHandler.END

# ─── Уведомления ─────────────────────────────────────────────────────────────

async def send_notifications(bot):
    """Вызывается планировщиком каждые 15 минут."""
    now = datetime.now(TZ)
    tasks = get_overdue_and_upcoming()

    for t in tasks:
        dt = datetime.fromisoformat(t["deadline"]).astimezone(TZ)
        diff = dt - now
        total_sec = diff.total_seconds()

        send = False
        tag = ""

        if total_sec < 0:
            # Просрочено — каждые 15 минут (scheduler уже каждые 15 мин)
            send = True
            tag = f"🔴 <b>ПРОСРОЧЕНО!</b>\nДедлайн был: {fmt_deadline(t['deadline'])}"
        elif diff.days == 0:
            # День дедлайна — каждый час (15 * 4 = 60 мин)
            mins_since_midnight = now.hour * 60 + now.minute
            if mins_since_midnight % 60 < 15:
                send = True
                hours_left = max(0, int(total_sec // 3600))
                tag = f"🟡 Дедлайн <b>сегодня</b>! Осталось ~{hours_left}ч\n📅 {fmt_deadline(t['deadline'])}"
        elif diff.days <= 3:
            # За 1-3 дня — 2 раза в день (утро 9:00 и вечер 20:00 ±15 мин)
            h, m = now.hour, now.minute
            morning = (h == 9 and m < 15)
            evening = (h == 20 and m < 15)
            if morning or evening:
                send = True
                tag = f"🟠 Дедлайн через <b>{diff.days} дн.</b>\n📅 {fmt_deadline(t['deadline'])}"

        if send:
            assignee_id = USERS.get(t["assignee"])
            if assignee_id:
                cat_emoji = next((e for e, n in CATEGORIES if n == t["category"]), "📝")
                try:
                    await bot.send_message(
                        chat_id=assignee_id,
                        text=(
                            f"⏰ <b>Напоминание о задаче</b>\n\n"
                            f"{cat_emoji} <b>{t['text']}</b>\n"
                            f"👤 от {t['creator']}\n\n"
                            f"{tag}"
                        ),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    log.warning(f"Не удалось отправить уведомление: {e}")

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler для создания задачи
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_new_task, pattern="^new_task$")],
        states={
            STATE_CATEGORY: [
                CallbackQueryHandler(cb_category, pattern="^cat_"),
                CallbackQueryHandler(cb_cancel,   pattern="^cancel$"),
            ],
            STATE_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_task_text),
            ],
            STATE_ASSIGNEE: [
                CallbackQueryHandler(cb_assignee, pattern="^assign_"),
                CallbackQueryHandler(cb_cancel,   pattern="^cancel$"),
            ],
            STATE_DEADLINE_CHOICE: [
                CallbackQueryHandler(cb_deadline_yes, pattern="^dl_yes$"),
                CallbackQueryHandler(cb_deadline_no,  pattern="^dl_no$"),
                CallbackQueryHandler(cb_cancel,       pattern="^cancel$"),
            ],
            STATE_DEADLINE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_deadline_input),
            ],
        },
        fallbacks=[CallbackQueryHandler(cb_cancel, pattern="^cancel$")],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_main_menu,        pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(cb_my_tasks,         pattern="^my_tasks$"))
    app.add_handler(CallbackQueryHandler(cb_toggle_task,      pattern="^toggle_"))
    app.add_handler(CallbackQueryHandler(cb_confirm_complete, pattern="^confirm_complete$"))
    app.add_handler(CallbackQueryHandler(cb_delete_mine,      pattern="^delete_mine$"))
    app.add_handler(CallbackQueryHandler(cb_del_task,         pattern="^del_"))

    # Планировщик уведомлений
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        send_notifications,
        trigger="interval",
        minutes=15,
        args=[app.bot],
    )
    scheduler.start()

    log.info("TaskBot запущен ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
