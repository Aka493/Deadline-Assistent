import sqlite3
import requests
import logging
from datetime import date, datetime as dt

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, ConversationHandler, filters
)

# ================== LOGGING ==================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# Убираем спам от httpx, apscheduler и telegram
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ================== TOKENS ==================
TELEGRAM_TOKEN = "8418805264:AAE6YC3_qOXDVFcC51ka-_3WeIvxOGYQmgo"
OPENROUTER_API_KEY = "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"

# ================== STATES ==================
(
    ADD_SUBJECT, ADD_DEADLINE, ADD_DIFFICULTY,
    AI_TEXT,
    DELETE_INDEX,
    EDIT_INDEX, EDIT_SUBJECT, EDIT_DEADLINE, EDIT_DIFFICULTY,
    FILTER_SUBJECT
) = range(10)

# ================== DATABASE ==================
conn = sqlite3.connect("assignments.db", check_same_thread=False)
cursor = conn.cursor()


def init_db():
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            subject TEXT,
            deadline DATE,
            difficulty INTEGER,
            risk INTEGER,
            notified INTEGER DEFAULT 0
        )
    """)
    conn.commit()


def add_assignment(user_id: int, subject: str, deadline: str, difficulty: int, risk: int):
    cursor.execute("""
        INSERT INTO assignments (user_id, subject, deadline, difficulty, risk)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, subject, deadline, difficulty, risk))
    conn.commit()


def get_assignments(user_id: int):
    cursor.execute("""
        SELECT id, subject, deadline, difficulty, risk
        FROM assignments
        WHERE user_id = ?
        ORDER BY deadline
    """, (user_id,))
    return cursor.fetchall()


def delete_assignment(user_id: int, index: int) -> bool:
    tasks = get_assignments(user_id)
    if not 1 <= index <= len(tasks):
        return False

    task_id = tasks[index - 1][0]
    cursor.execute("DELETE FROM assignments WHERE id = ?", (task_id,))
    conn.commit()
    return True


def update_assignment(task_id: int, subject: str, deadline: str, difficulty: int, risk: int):
    cursor.execute("""
        UPDATE assignments
        SET subject=?, deadline=?, difficulty=?, risk=?
        WHERE id = ?
    """, (subject, deadline, difficulty, risk, task_id))
    conn.commit()


# ================== AI ==================
def ai_request(prompt: str) -> str:
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "mistralai/mistral-7b-instruct",
                "messages": [
                    {"role": "system", "content": "Ты учебный ассистент студента."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 250
            },
            timeout=30
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()

    except Exception as e:
        logger.error(f"AI request error: {e}")
        return "⚠️ ИИ временно недоступен. Попробуй позже."


def calc_risk(deadline: date, difficulty: int) -> int:
    days_left = (deadline - date.today()).days

    if days_left < 0:
        return 5
    if days_left < difficulty:
        return 5
    if days_left < difficulty * 2:
        return 3
    return 1


def task_priority(task) -> int:
    deadline = dt.fromisoformat(task[2]).date()
    days_left = (deadline - date.today()).days
    diff = task[3]
    risk = task[4]
    return diff * 2 + risk * 3 - days_left


# ================== KEYBOARD ==================
main_keyboard = ReplyKeyboardMarkup(
    [
        ["➕ Добавить задание"],
        ["📋 Мои задания", "📊 Статистика"],
        ["📌 Приоритет дня", "📅 План на неделю"],
        ["🤖 ИИ-совет"],
        ["✏️ Редактировать задание", "🗑 Удалить задание"],
        ["🔍 Фильтр по предмету"]
    ],
    resize_keyboard=True
)


# ================== HELPERS ==================
def format_tasks(tasks) -> str:
    text = "📋 Твои задания:\n\n"
    for i, (_, s, d, diff, r) in enumerate(tasks, 1):
        text += f"{i}. 📘 {s}\n   📅 {d} | ⭐{diff} | ⚠️{r}\n\n"
    return text.strip()


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.", reply_markup=main_keyboard)
    return ConversationHandler.END


# ================== START ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я Deadline Assistant 🤖",
        reply_markup=main_keyboard
    )


# ================== ADD TASK ==================
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📘 Введите название предмета:")
    return ADD_SUBJECT


async def add_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subject = update.message.text.strip()

    if not subject:
        await update.message.reply_text("❌ Предмет не может быть пустым. Введите ещё раз:")
        return ADD_SUBJECT

    context.user_data["subject"] = subject
    await update.message.reply_text("📅 Введите дедлайн (YYYY-MM-DD):")
    return ADD_DEADLINE


async def add_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        deadline = dt.strptime(update.message.text.strip(), "%Y-%m-%d").date()

        if deadline < date.today():
            await update.message.reply_text("❌ Дедлайн не может быть в прошлом. Введите дату снова:")
            return ADD_DEADLINE

        context.user_data["deadline"] = deadline
        await update.message.reply_text("⚙️ Сложность (1–5):")
        return ADD_DIFFICULTY

    except ValueError:
        await update.message.reply_text("❌ Формат должен быть YYYY-MM-DD")
        return ADD_DEADLINE


async def add_difficulty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text("❌ Введите число от 1 до 5")
        return ADD_DIFFICULTY

    difficulty = int(text)
    if not 1 <= difficulty <= 5:
        await update.message.reply_text("❌ Введите число от 1 до 5")
        return ADD_DIFFICULTY

    deadline = context.user_data["deadline"]
    risk = calc_risk(deadline, difficulty)

    add_assignment(
        update.effective_user.id,
        context.user_data["subject"],
        deadline.isoformat(),
        difficulty,
        risk
    )

    await update.message.reply_text("✅ Задание добавлено", reply_markup=main_keyboard)
    return ConversationHandler.END


# ================== LIST ==================
async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_assignments(update.effective_user.id)

    if not tasks:
        await update.message.reply_text("📭 Нет заданий", reply_markup=main_keyboard)
        return

    await update.message.reply_text(format_tasks(tasks), reply_markup=main_keyboard)


# ================== PRIORITY DAY ==================
async def priority_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_assignments(update.effective_user.id)

    if not tasks:
        await update.message.reply_text("📭 Нет заданий", reply_markup=main_keyboard)
        return

    task = max(tasks, key=task_priority)
    _, subject, deadline, diff, risk = task

    prompt = f"""
Главное задание на сегодня:
{subject}
Дедлайн: {deadline}
Сложность: {diff}/5
Риск: {risk}/5

Составь план на сегодня (3 шага).
"""
    await update.message.reply_text("📌 Приоритет дня\n\n" + ai_request(prompt), reply_markup=main_keyboard)


# ================== WEEK PLAN ==================
async def week_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_assignments(update.effective_user.id)

    if not tasks:
        await update.message.reply_text("📭 Нет заданий", reply_markup=main_keyboard)
        return

    text = "Задания:\n"
    for _, s, d, diff, r in tasks:
        text += f"- {s}, дедлайн {d}, сложность {diff}, риск {r}\n"

    prompt = f"""
На основе заданий составь план на неделю (Пн–Вс),
учитывая дедлайны и сложность.

{text}
"""
    await update.message.reply_text("📅 План на неделю\n\n" + ai_request(prompt), reply_markup=main_keyboard)


# ================== AI CHAT ==================
async def ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Напиши вопрос:")
    return AI_TEXT


async def ai_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text.strip()

    if not question:
        await update.message.reply_text("❌ Вопрос не может быть пустым.")
        return AI_TEXT

    await update.message.reply_text(ai_request(question), reply_markup=main_keyboard)
    return ConversationHandler.END


# ================== DELETE ==================
async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await list_tasks(update, context)
    await update.message.reply_text("🗑 Введи номер задания:")
    return DELETE_INDEX


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        index = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введите номер")
        return DELETE_INDEX

    if delete_assignment(update.effective_user.id, index):
        await update.message.reply_text("✅ Удалено", reply_markup=main_keyboard)
    else:
        await update.message.reply_text("❌ Ошибка: неверный номер", reply_markup=main_keyboard)

    return ConversationHandler.END


# ================== EDIT ==================
async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await list_tasks(update, context)
    await update.message.reply_text("✏️ Введи номер задания для редактирования:")
    return EDIT_INDEX


async def edit_index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        index = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введите номер")
        return EDIT_INDEX

    tasks = get_assignments(update.effective_user.id)
    if not 1 <= index <= len(tasks):
        await update.message.reply_text("❌ Неверный номер задания", reply_markup=main_keyboard)
        return ConversationHandler.END

    context.user_data["edit_task_id"] = tasks[index - 1][0]
    await update.message.reply_text("✏️ Новый предмет:")
    return EDIT_SUBJECT


async def edit_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subject = update.message.text.strip()

    if not subject:
        await update.message.reply_text("❌ Предмет не может быть пустым. Введите ещё раз:")
        return EDIT_SUBJECT

    context.user_data["subject"] = subject
    await update.message.reply_text("📅 Новый дедлайн (YYYY-MM-DD):")
    return EDIT_DEADLINE


async def edit_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        deadline = dt.strptime(update.message.text.strip(), "%Y-%m-%d").date()

        if deadline < date.today():
            await update.message.reply_text("❌ Дедлайн не может быть в прошлом. Введите дату снова:")
            return EDIT_DEADLINE

        context.user_data["deadline"] = deadline
        await update.message.reply_text("⚙️ Новая сложность (1–5):")
        return EDIT_DIFFICULTY

    except ValueError:
        await update.message.reply_text("❌ Формат YYYY-MM-DD")
        return EDIT_DEADLINE


async def edit_difficulty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text("❌ Введите число от 1 до 5")
        return EDIT_DIFFICULTY

    difficulty = int(text)
    if not 1 <= difficulty <= 5:
        await update.message.reply_text("❌ Введите число от 1 до 5")
        return EDIT_DIFFICULTY

    deadline = context.user_data["deadline"]
    risk = calc_risk(deadline, difficulty)

    update_assignment(
        context.user_data["edit_task_id"],
        context.user_data["subject"],
        deadline.isoformat(),
        difficulty,
        risk
    )

    await update.message.reply_text("✅ Задание обновлено", reply_markup=main_keyboard)
    return ConversationHandler.END


# ================== FILTER ==================
async def filter_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите предмет для фильтрации:")
    return FILTER_SUBJECT


async def show_filtered_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subject = update.message.text.strip()

    if not subject:
        await update.message.reply_text("❌ Предмет не может быть пустым", reply_markup=main_keyboard)
        return ConversationHandler.END

    tasks = get_assignments(update.effective_user.id)
    filtered = [t for t in tasks if t[1].lower() == subject.lower()]

    if not filtered:
        await update.message.reply_text("📭 Нет заданий с таким предметом", reply_markup=main_keyboard)
    else:
        text = f"📋 Задания по предмету: {subject}\n\n"
        for i, (_, s, d, diff, r) in enumerate(filtered, 1):
            text += f"{i}. 📘 {s}\n   📅 {d} | ⭐{diff} | ⚠️{r}\n\n"
        await update.message.reply_text(text.strip(), reply_markup=main_keyboard)

    return ConversationHandler.END


# ================== STATISTICS ==================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_assignments(update.effective_user.id)

    if not tasks:
        await update.message.reply_text("📭 Нет заданий", reply_markup=main_keyboard)
        return

    total = len(tasks)
    avg_diff = sum(t[3] for t in tasks) / total
    avg_risk = sum(t[4] for t in tasks) / total

    text = (
        f"📊 Статистика заданий:\n"
        f"- Всего: {total}\n"
        f"- Средняя сложность: {avg_diff:.2f}\n"
        f"- Средний риск: {avg_risk:.2f}"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard)


# ================== REMINDERS (JobQueue) ==================
async def send_daily_reminders(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT DISTINCT user_id FROM assignments")
    users = [row[0] for row in cursor.fetchall()]

    today = date.today()

    for user_id in users:
        tasks = get_assignments(user_id)
        reminders = [t for t in tasks if 0 <= (dt.fromisoformat(t[2]).date() - today).days <= 1]

        if reminders:
            text = "⏰ Напоминание о ближайших заданиях:\n\n"
            for _, s, d, diff, risk in reminders:
                days_left = (dt.fromisoformat(d).date() - today).days
                text += f"- 📘 {s}\n  📅 {d} (осталось {days_left} дн.) | ⭐{diff} | ⚠️{risk}\n\n"

            await context.bot.send_message(chat_id=user_id, text=text.strip())


# ================== MAIN ==================
def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Старт
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))

    # Добавление
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить задание$"), add_start)],
        states={
            ADD_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_subject)],
            ADD_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_deadline)],
            ADD_DIFFICULTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_difficulty)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    # Редактирование
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✏️ Редактировать задание$"), edit_start)],
        states={
            EDIT_INDEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_index)],
            EDIT_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_subject)],
            EDIT_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_deadline)],
            EDIT_DIFFICULTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_difficulty)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    # Удаление
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗑 Удалить задание$"), delete_start)],
        states={DELETE_INDEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_confirm)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    # ИИ
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🤖 ИИ-совет$"), ai_start)],
        states={AI_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_answer)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    # Фильтр по предмету
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔍 Фильтр по предмету$"), filter_subject)],
        states={FILTER_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, show_filtered_subject)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    # Прочее
    app.add_handler(MessageHandler(filters.Regex("^📋 Мои задания$"), list_tasks))
    app.add_handler(MessageHandler(filters.Regex("^📊 Статистика$"), stats))
    app.add_handler(MessageHandler(filters.Regex("^📌 Приоритет дня$"), priority_day))
    app.add_handler(MessageHandler(filters.Regex("^📅 План на неделю$"), week_plan))

    # Напоминания
    app.job_queue.run_repeating(send_daily_reminders, interval=24 * 60 * 60, first=10)

    print("✅ Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
