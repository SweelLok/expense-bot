import os
import csv
import logging
from datetime import datetime, timedelta
from io import StringIO

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes,
)

from db import Database

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN not set in .env")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Monkey-patch Bot для логирования исходящих сообщений ---
from telegram import Bot

_original_send_message = Bot.send_message
_original_send_document = Bot.send_document

# Хранилище chat_id -> user_id (обновляется при входящих сообщениях)
_CHAT_USER_MAP: dict[int, int] = {}


async def _patched_send_message(self, chat_id, text, **kwargs):
    msg = await _original_send_message(self, chat_id, text, **kwargs)
    try:
        uid = _CHAT_USER_MAP.get(chat_id)
        if uid:
            await db.log_message(uid, chat_id, msg.message_id, 1)
    except Exception:
        pass
    return msg


async def _patched_send_document(self, document, chat_id, **kwargs):
    msg = await _original_send_document(self, document, chat_id, **kwargs)
    try:
        uid = _CHAT_USER_MAP.get(chat_id)
        if uid:
            await db.log_message(uid, chat_id, msg.message_id, 1)
    except Exception:
        pass
    return msg


Bot.send_message = _patched_send_message
Bot.send_document = _patched_send_document

# Все состояния в одном ConversationHandler
(
    IDLE,
    ADD_AMOUNT, ADD_CATEGORY, ADD_DATE_YEAR, ADD_DATE_MONTH, ADD_DATE_DAY, ADD_DESC,
    EDIT_DATE_YEAR, EDIT_DATE_MONTH, EDIT_DATE_DAY, EDIT_SELECT, EDIT_FIELD, EDIT_VALUE, EDIT_NEW_DATE_YEAR, EDIT_NEW_DATE_MONTH, EDIT_NEW_DATE_DAY,
    DEL_DATE_YEAR, DEL_DATE_MONTH, DEL_DATE_DAY, DEL_SELECT,
    ADDSUB_NAME, ADDSUB_AMOUNT, ADDSUB_PERIOD,
    ADDCAT_NAME,
    SETLIMIT_CATEGORY, SETLIMIT_AMOUNT,
    BALANCE_INPUT,
) = range(27)

DEFAULT_CATEGORIES = ["еда", "бытовое", "подписки", "транспорт", "развлечения", "прочее"]

exchange_rates: dict[str, float] = {}
last_rate_update: datetime | None = None

db = Database()

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("💶 Баланс"), KeyboardButton("➕ Добавить трату")],
        [KeyboardButton("📊 Статистика"), KeyboardButton("📋 Подписки")],
        [KeyboardButton("✏️ Редактировать"), KeyboardButton("🗑 Удалить")],
        [KeyboardButton("⚙️ Настройки"), KeyboardButton("❌ Отмена")],
    ],
    resize_keyboard=True,
)


async def get_exchange_rates() -> dict[str, float]:
    global exchange_rates, last_rate_update
    if last_rate_update and (datetime.now() - last_rate_update).seconds < 3600:
        return exchange_rates
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://open.er-api.com/v6/latest/EUR")
            data = resp.json()
            exchange_rates = data.get("rates", {})
            # Оставляем только нужные валюты
            exchange_rates = {k: v for k, v in exchange_rates.items() if k in ("USD", "UAH")}
            last_rate_update = datetime.now()
            return exchange_rates
    except Exception as e:
        logger.error(f"Failed to get exchange rates: {e}")
        return exchange_rates


async def to_eur(amount: float, currency: str) -> float:
    if currency == "EUR":
        return amount
    rates = await get_exchange_rates()
    rate = rates.get(currency)
    if rate:
        return amount / rate
    return amount


def eur(amount: float) -> str:
    return f"{amount:.2f}€"


def get_week_range() -> tuple[str, str]:
    today = datetime.now()
    week_ago = today - timedelta(days=7)
    return week_ago.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def get_month_range() -> tuple[str, str]:
    today = datetime.now()
    month_ago = today - timedelta(days=30)
    return month_ago.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def get_date_range(period: str) -> tuple[str, str]:
    today = datetime.now()
    if period == "week":
        start = today - timedelta(days=7)
    elif period == "month":
        start = today - timedelta(days=30)
    elif period == "year":
        start = today - timedelta(days=365)
    else:
        start = datetime(2000, 1, 1)
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


async def categories_keyboard(user_id: int) -> InlineKeyboardMarkup:
    cats = await db.get_categories(user_id)
    buttons = []
    row = []
    for c in cats:
        row.append(InlineKeyboardButton(c["name"], callback_data=f"cat_{c['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def stats_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📅 За неделю", callback_data="stats_week")],
        [InlineKeyboardButton("📆 За месяц", callback_data="stats_month")],
        [InlineKeyboardButton("📆 За год", callback_data="stats_year")],
        [InlineKeyboardButton("📋 Все траты", callback_data="stats_all")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="stats_back")],
    ]
    return InlineKeyboardMarkup(keyboard)


def settings_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("💰 Изменить баланс", callback_data="setbalance")],
        [InlineKeyboardButton("📋 Добавить категорию", callback_data="addcat")],
        [InlineKeyboardButton("⚠️ Лимиты", callback_data="setlimit")],
        [InlineKeyboardButton("💱 Курсы валют", callback_data="rates")],
        [InlineKeyboardButton("📤 Экспорт CSV", callback_data="export")],
        [InlineKeyboardButton("🧹 Очистить чат", callback_data="clearchat")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ===================== DATE PICKER =====================

def date_year_keyboard(current_year: int) -> InlineKeyboardMarkup:
    years = [current_year - 1, current_year, current_year + 1]
    buttons = [[InlineKeyboardButton(str(y), callback_data=f"year_{y}") for y in years]]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def date_month_keyboard(year: int) -> InlineKeyboardMarkup:
    months = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    buttons = []
    row = []
    for i, m in enumerate(months, 1):
        row.append(InlineKeyboardButton(m, callback_data=f"month_{year}_{i}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Год", callback_data="back_year")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def date_day_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    import calendar
    days_in_month = calendar.monthrange(year, month)[1]
    today = datetime.now()
    buttons = []
    row = []
    for d in range(1, days_in_month + 1):
        dt = datetime(year, month, d)
        label = str(d)
        if dt.date() == today.date():
            label = f"•{d}•"
        elif dt.date() > today.date():
            label = f"🔒{d}"
        row.append(InlineKeyboardButton(label, callback_data=f"day_{year}_{month}_{d}"))
        if len(row) == 7:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Месяц", callback_data=f"back_month_{year}")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


# ===================== START =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db.init()
    for cat_name in DEFAULT_CATEGORIES:
        await db.add_category(user_id, cat_name)
    await update.message.reply_text(
        "Привет! Я бот для учёта расходов.\n\n"
        "Используй кнопки меню ниже:\n"
        "💶 Баланс — посмотреть баланс\n"
        "➕ Добавить трату — записать расход\n"
        "📊 Статистика — отчёты\n"
        "📋 Подписки — управление подписками\n"
        "✏️ Редактировать — изменить трату\n"
        "🗑 Удалить — удалить трату\n"
        "⚙️ Настройки — баланс, категории, лимиты\n"
        "❌ Отмена — отменить текущее действие",
        reply_markup=MENU_KEYBOARD,
    )
    return IDLE


# ===================== CLEAR CHAT =====================

async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    bot = context.bot

    # Сохраняем ID текущего сообщения (кнопка которая нажята) чтобы не удалять его
    current_msg_id = update.callback_query.message.message_id

    try:
        message_ids = await db.get_message_ids(user_id, chat_id)
        deleted = 0
        for msg_id in message_ids:
            if msg_id == current_msg_id:
                continue
            try:
                await bot.delete_message(chat_id, msg_id)
                deleted += 1
            except Exception:
                pass
        await db.clear_message_log(user_id, chat_id)

        # Отправляем новое сообщение вместо редактирования удаленного
        await bot.send_message(
            chat_id,
            f"🧹 Удалено сообщений: {deleted}",
            reply_markup=MENU_KEYBOARD,
        )
    except Exception as e:
        logger.error(f"Clear chat error: {e}")
        await bot.send_message(
            chat_id,
            "❌ Не удалось очистить чат. Возможно, сообщения старше 48 часов.",
            reply_markup=MENU_KEYBOARD,
        )
    return IDLE


# ===================== MESSAGE LOGGING =====================

async def log_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Middleware: сохраняет ID всех сообщений (и бота, и пользователя)."""
    if update.message:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        _CHAT_USER_MAP[chat_id] = user_id
        await db.log_message(
            user_id, chat_id, update.message.message_id,
            1 if update.message.from_user.is_bot else 0,
        )


# ===================== CANCEL =====================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Отменено.")
    else:
        await update.message.reply_text("❌ Отменено.", reply_markup=MENU_KEYBOARD)
    return IDLE


# ===================== BALANCE =====================

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bal = await db.get_balance(user_id)
    week_start, week_end = get_week_range()
    week_spent = sum(e["amount"] for e in await db.get_expenses(user_id, start_date=week_start, end_date=week_end))
    monthly = await db.get_monthly_spending_by_category(user_id)
    lines = [f"💶 Текущий баланс: {eur(bal)}", f"💸 Потрачено за неделю: {eur(week_spent)}"]
    if monthly:
        lines.append("\n📊 За 30 дней:")
        total_monthly = 0
        for m in monthly:
            if m["spent"] > 0:
                lines.append(f"  • {m['category_name']}: {eur(m['spent'])}")
                total_monthly += m["spent"]
        lines.append(f"  Итого: {eur(total_monthly)}")
    lines.append("\n💰 Изменить: ⚙️ Настройки → 💰 Изменить баланс")
    await update.message.reply_text("\n".join(lines), reply_markup=MENU_KEYBOARD)
    return IDLE


async def setbalance_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введи новый баланс (в EUR):")
    return BALANCE_INPUT


async def setbalance_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("❌ Не могу распознать сумму. Попробуй ещё раз:")
        return BALANCE_INPUT
    await db.set_balance(user_id, amount)
    await update.message.reply_text(f"✅ Баланс установлен: {eur(amount)}", reply_markup=MENU_KEYBOARD)
    return IDLE


# ===================== ADD EXPENSE =====================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "➕ Новая трата\n\nШаг 1/4: Введи сумму (можно с валютой: 15.50, 200 UAH, 20 USD):"
    )
    return ADD_AMOUNT


async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parts = text.split()
    try:
        amount = float(parts[0])
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Не могу распознать сумму. Попробуй ещё раз:")
        return ADD_AMOUNT

    currency = "EUR"
    if len(parts) > 1 and parts[1].upper() in ("EUR", "USD", "UAH"):
        currency = parts[1].upper()

    context.user_data["add_amount"] = amount
    context.user_data["add_currency"] = currency

    keyboard = await categories_keyboard(update.effective_user.id)
    eur_amt = await to_eur(amount, currency)
    await update.message.reply_text(
        f"Шаг 1/4: ✅ {amount} {currency} = {eur(eur_amt)}\n\nШаг 2/4: Выбери категорию:",
        reply_markup=keyboard,
    )
    return ADD_CATEGORY


async def add_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    category_id = int(query.data.split("_")[1])
    context.user_data["add_category_id"] = category_id

    today = datetime.now()
    await query.edit_message_text(
        f"Шаг 2/4: ✅ Категория выбрана\n\nШаг 3/4: Выбери дату:",
        reply_markup=date_year_keyboard(today.year),
    )
    return ADD_DATE_YEAR


async def add_date_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    year = int(query.data.split("_")[-1])
    context.user_data["add_date_year"] = year
    await query.edit_message_text("Шаг 3/4: Выбери месяц:", reply_markup=date_month_keyboard(year))
    return ADD_DATE_MONTH


async def add_date_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    parts = query.data.split("_")
    year, month = int(parts[-2]), int(parts[-1])
    context.user_data["add_date_year"] = year
    context.user_data["add_date_month"] = month
    await query.edit_message_text("Шаг 3/4: Выбери день:", reply_markup=date_day_keyboard(year, month))
    return ADD_DATE_DAY


async def add_date_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    parts = query.data.split("_")
    year, month, day = int(parts[-3]), int(parts[-2]), int(parts[-1])
    exp_date = f"{year:04d}-{month:02d}-{day:02d}"
    context.user_data["add_date"] = exp_date

    await query.edit_message_text(
        f"Шаг 3/4: ✅ Дата: {exp_date}\n\nШаг 4/4: Введи описание (или /skip):"
    )
    return ADD_DESC


async def add_date_back_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    today = datetime.now()
    await query.edit_message_text("Шаг 3/4: Выбери год:", reply_markup=date_year_keyboard(today.year))
    return ADD_DATE_YEAR


async def add_date_back_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    year = context.user_data.get("add_date_year", datetime.now().year)
    await query.edit_message_text("Шаг 3/4: Выбери месяц:", reply_markup=date_month_keyboard(year))
    return ADD_DATE_MONTH


async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    description = None if text == "/skip" else text

    amount = context.user_data["add_amount"]
    currency = context.user_data["add_currency"]
    category_id = context.user_data["add_category_id"]
    exp_date = context.user_data["add_date"]

    eur_amount = await to_eur(amount, currency)
    await db.add_expense(user_id, eur_amount, currency, category_id, description, exp_date)
    await db.add_to_balance(user_id, -eur_amount)

    cats = await db.get_categories(user_id)
    cat = next((c for c in cats if c["id"] == category_id), None)
    cat_name = cat["name"] if cat else "?"

    limit_msg = ""
    if cat and cat.get("weekly_limit"):
        week_start, week_end = get_week_range()
        week_spent = sum(
            e["amount"] for e in await db.get_expenses(user_id, start_date=week_start, end_date=week_end, category_id=category_id)
        )
        if week_spent >= cat["weekly_limit"]:
            limit_msg = f"\n\n⚠️ Лимит на «{cat_name}» превышен! ({eur(week_spent)} из {eur(cat['weekly_limit'])})"
        elif week_spent >= cat["weekly_limit"] * 0.8:
            limit_msg = f"\n\n⚡ Лимит на «{cat_name}» почти исчерпан: {eur(week_spent)} из {eur(cat['weekly_limit'])}"

    bal = await db.get_balance(user_id)
    await update.message.reply_text(
        f"✅ Трата добавлена!\n\n"
        f"💰 {amount} {currency} ({eur(eur_amount)})\n"
        f"📂 {cat_name}\n"
        f"📅 {exp_date}\n"
        f"📝 {description or '—'}\n\n"
        f"💶 Баланс: {eur(bal)}{limit_msg}",
        reply_markup=MENU_KEYBOARD,
    )
    context.user_data.clear()
    return IDLE


# ===================== EDIT (BY DATE - SAME AS DELETE) =====================

async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    total = await db.get_expenses_count(user_id)
    if total == 0:
        await update.message.reply_text("❌ Нет трат для редактирования.", reply_markup=MENU_KEYBOARD)
        return IDLE

    today = datetime.now()
    await update.message.reply_text(
        "✏️ Редактирование\n\nШаг 1/3: Выбери дату:",
        reply_markup=date_year_keyboard(today.year),
    )
    return EDIT_DATE_YEAR


async def edit_date_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    year = int(query.data.split("_")[-1])
    context.user_data["edit_date_year"] = year
    await query.edit_message_text("Выбери месяц:", reply_markup=date_month_keyboard(year))
    return EDIT_DATE_MONTH


async def edit_date_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    parts = query.data.split("_")
    year, month = int(parts[-2]), int(parts[-1])
    context.user_data["edit_date_year"] = year
    context.user_data["edit_date_month"] = month
    await query.edit_message_text("Выбери день:", reply_markup=date_day_keyboard(year, month))
    return EDIT_DATE_DAY


async def edit_date_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    parts = query.data.split("_")
    year, month, day = int(parts[-3]), int(parts[-2]), int(parts[-1])
    exp_date = f"{year:04d}-{month:02d}-{day:02d}"
    context.user_data["edit_date"] = exp_date

    user_id = update.effective_user.id
    expenses = await db.get_expenses(user_id, start_date=exp_date, end_date=exp_date)
    if not expenses:
        await query.edit_message_text(f"❌ Нет трат за {exp_date}", reply_markup=MENU_KEYBOARD)
        context.user_data.clear()
        return IDLE

    lines = [f"✏️ Траты за {exp_date} ({len(expenses)}):\n"]
    buttons = []
    for e in expenses:
        desc = f" — {e['description']}" if e["description"] else ""
        lines.append(f"• {eur(e['amount'])} | {e['category_name']}{desc}")
        buttons.append([InlineKeyboardButton(
            f"✏️ {eur(e['amount'])} {e['category_name']}",
            callback_data=f"edit_select_{e['id']}"
        )])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await query.edit_message_text(
        "\n".join(lines) + "\n\nВыбери трату для редактирования:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_SELECT


async def edit_date_back_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    today = datetime.now()
    await query.edit_message_text("Выбери год:", reply_markup=date_year_keyboard(today.year))
    return EDIT_DATE_YEAR


async def edit_date_back_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    year = context.user_data.get("edit_date_year", datetime.now().year)
    await query.edit_message_text("Выбери месяц:", reply_markup=date_month_keyboard(year))
    return EDIT_DATE_MONTH


async def edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    exp_id = int(query.data.split("_")[-1])
    context.user_data["edit_expense_id"] = exp_id

    exp = await db.get_expense_by_id(update.effective_user.id, exp_id)
    if not exp:
        await query.edit_message_text("❌ Трата не найдена.")
        context.user_data.clear()
        return IDLE

    keyboard = [
        [InlineKeyboardButton("💰 Сумма", callback_data="edit_field_amount")],
        [InlineKeyboardButton("📂 Категория", callback_data="edit_field_category")],
        [InlineKeyboardButton("📝 Описание", callback_data="edit_field_desc")],
        [InlineKeyboardButton("📅 Дата", callback_data="edit_field_date")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    await query.edit_message_text(
        f"✏️ Трата:\n\n"
        f"💰 {eur(exp['amount'])} ({exp['currency']})\n"
        f"📂 {exp['category_name']}\n"
        f"📝 {exp['description'] or '—'}\n"
        f"📅 {exp['expense_date']}\n\n"
        f"Что изменить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return EDIT_FIELD


async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    field = query.data.replace("edit_field_", "")
    context.user_data["edit_field"] = field

    if field == "amount":
        await query.edit_message_text("Введи новую сумму (в EUR):")
        return EDIT_VALUE
    elif field == "description":
        await query.edit_message_text("Введи новое описание:")
        return EDIT_VALUE
    elif field == "category":
        user_id = update.effective_user.id
        keyboard = await categories_keyboard(user_id)
        await query.edit_message_text("Выбери новую категорию:", reply_markup=keyboard)
        return EDIT_VALUE
    elif field == "date":
        today = datetime.now()
        await query.edit_message_text("Выбери новую дату:", reply_markup=date_year_keyboard(today.year))
        return EDIT_NEW_DATE_YEAR


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle date picker callbacks for date editing
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "cancel":
            context.user_data.clear()
            await query.edit_message_text("❌ Отменено.")
            return IDLE

        field = context.user_data.get("edit_field")

        if field == "date" and data.startswith("year_"):
            year = int(data.split("_")[-1])
            context.user_data["edit_new_date_year"] = year
            await query.edit_message_text("Выбери месяц:", reply_markup=date_month_keyboard(year))
            return EDIT_NEW_DATE_MONTH
        elif field == "date" and data.startswith("month_"):
            parts = data.split("_")
            year, month = int(parts[-2]), int(parts[-1])
            context.user_data["edit_new_date_year"] = year
            context.user_data["edit_new_date_month"] = month
            await query.edit_message_text("Выбери день:", reply_markup=date_day_keyboard(year, month))
            return EDIT_NEW_DATE_DAY
        elif field == "date" and data.startswith("day_"):
            parts = data.split("_")
            year, month, day = int(parts[-3]), int(parts[-2]), int(parts[-1])
            exp_date = f"{year:04d}-{month:02d}-{day:02d}"
            user_id = update.effective_user.id
            exp_id = context.user_data.get("edit_expense_id")
            await db.update_expense(user_id, exp_id, expense_date=exp_date)
            await query.edit_message_text(f"✅ Дата изменена на {exp_date}")
            context.user_data.clear()
            return IDLE
        elif field == "date" and data == "back_year":
            today = datetime.now()
            await query.edit_message_text("Выбери год:", reply_markup=date_year_keyboard(today.year))
            return EDIT_NEW_DATE_YEAR
        elif field == "date" and data.startswith("back_month_"):
            year = int(data.split("_")[-1])
            await query.edit_message_text("Выбери месяц:", reply_markup=date_month_keyboard(year))
            return EDIT_NEW_DATE_MONTH
        elif field == "category" and data.startswith("cat_"):
            category_id = int(data.split("_")[1])
            user_id = update.effective_user.id
            exp_id = context.user_data.get("edit_expense_id")
            await db.update_expense(user_id, exp_id, category_id=category_id)
            cats = await db.get_categories(user_id)
            cat = next((c for c in cats if c["id"] == category_id), None)
            cat_name = cat["name"] if cat else "?"
            await query.edit_message_text(f"✅ Категория изменена на «{cat_name}»")
            context.user_data.clear()
            return IDLE

    # Text input for amount/description
    user_id = update.effective_user.id
    exp_id = context.user_data.get("edit_expense_id")
    field = context.user_data.get("edit_field")
    text = update.message.text.strip()

    exp = await db.get_expense_by_id(user_id, exp_id)
    if not exp:
        await update.message.reply_text("❌ Трата не найдена.")
        context.user_data.clear()
        return IDLE

    old_amount = exp["amount"]

    if field == "amount":
        try:
            new_amount = float(text)
        except ValueError:
            await update.message.reply_text("❌ Не могу распознать сумму. Попробуй ещё раз:")
            return EDIT_VALUE
        await db.add_to_balance(user_id, old_amount)
        await db.add_to_balance(user_id, -new_amount)
        await db.update_expense(user_id, exp_id, amount=new_amount)
        await update.message.reply_text(f"✅ Сумма изменена на {eur(new_amount)}", reply_markup=MENU_KEYBOARD)

    elif field == "description":
        await db.update_expense(user_id, exp_id, description=text)
        await update.message.reply_text(f"✅ Описание обновлено: {text}", reply_markup=MENU_KEYBOARD)

    context.user_data.clear()
    return IDLE


# ===================== DELETE (BY DATE) =====================

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    total = await db.get_expenses_count(user_id)
    if total == 0:
        await update.message.reply_text("❌ Нет трат для удаления.", reply_markup=MENU_KEYBOARD)
        return IDLE

    today = datetime.now()
    await update.message.reply_text(
        "🗑 Удаление\n\nШаг 1/3: Выбери дату:",
        reply_markup=date_year_keyboard(today.year),
    )
    return DEL_DATE_YEAR


async def delete_date_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    year = int(query.data.split("_")[-1])
    context.user_data["del_date_year"] = year
    await query.edit_message_text("Выбери месяц:", reply_markup=date_month_keyboard(year))
    return DEL_DATE_MONTH


async def delete_date_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    parts = query.data.split("_")
    year, month = int(parts[-2]), int(parts[-1])
    context.user_data["del_date_year"] = year
    context.user_data["del_date_month"] = month
    await query.edit_message_text("Выбери день:", reply_markup=date_day_keyboard(year, month))
    return DEL_DATE_DAY


async def delete_date_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    parts = query.data.split("_")
    year, month, day = int(parts[-3]), int(parts[-2]), int(parts[-1])
    exp_date = f"{year:04d}-{month:02d}-{day:02d}"
    context.user_data["del_date"] = exp_date

    user_id = update.effective_user.id
    expenses = await db.get_expenses(user_id, start_date=exp_date, end_date=exp_date)
    if not expenses:
        await query.edit_message_text(f"❌ Нет трат за {exp_date}", reply_markup=MENU_KEYBOARD)
        context.user_data.clear()
        return IDLE

    lines = [f"🗑 Траты за {exp_date} ({len(expenses)}):\n"]
    buttons = []
    for e in expenses:
        desc = f" — {e['description']}" if e["description"] else ""
        lines.append(f"• {eur(e['amount'])} | {e['category_name']}{desc}")
        buttons.append([InlineKeyboardButton(
            f"🗑 {eur(e['amount'])} {e['category_name']}",
            callback_data=f"del_select_{e['id']}"
        )])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await query.edit_message_text(
        "\n".join(lines) + "\n\nВыбери трату для удаления:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return DEL_SELECT


async def delete_date_back_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    today = datetime.now()
    await query.edit_message_text("Выбери год:", reply_markup=date_year_keyboard(today.year))
    return DEL_DATE_YEAR


async def delete_date_back_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    year = context.user_data.get("del_date_year", datetime.now().year)
    await query.edit_message_text("Выбери месяц:", reply_markup=date_month_keyboard(year))
    return DEL_DATE_MONTH


async def delete_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    exp_id = int(query.data.split("_")[-1])
    context.user_data["del_expense_id"] = exp_id

    exp = await db.get_expense_by_id(update.effective_user.id, exp_id)
    if not exp:
        await query.edit_message_text("❌ Трата не найдена.")
        context.user_data.clear()
        return IDLE

    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="del_confirm_yes")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    await query.edit_message_text(
        f"Удалить трату?\n\n"
        f"💰 {eur(exp['amount'])} на «{exp['category_name']}»\n"
        f"📅 {exp['expense_date']}\n"
        f"📝 {exp['description'] or '—'}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return DEL_SELECT


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "del_confirm_yes":
        exp_id = context.user_data.get("del_expense_id")
        exp = await db.get_expense_by_id(user_id, exp_id)
        if exp:
            await db.delete_expense(user_id, exp_id)
            await db.add_to_balance(user_id, exp["amount"])
            await query.edit_message_text(
                f"✅ Трата удалена. Баланс: {eur(await db.get_balance(user_id))}"
            )
        else:
            await query.edit_message_text("❌ Трата не найдена.")
    elif query.data == "cancel":
        await query.edit_message_text("❌ Отменено.")
    else:
        await query.edit_message_text("❌ Отменено.")

    context.user_data.clear()
    return IDLE


# ===================== STATS =====================

async def stats_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Выбери период:", reply_markup=stats_keyboard())
    return IDLE


async def stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data == "stats_back":
        await query.edit_message_text("Меню:", reply_markup=MENU_KEYBOARD)
        return IDLE

    if data == "stats_week":
        start, end = get_week_range()
        title = "📊 За неделю"
    elif data == "stats_month":
        start, end = get_month_range()
        title = "📊 За месяц"
    elif data == "stats_year":
        start, end = get_date_range("year")
        title = "📊 За год"
    else:
        start = None
        end = None
        title = "📊 Все траты"

    expenses = await db.get_expenses(user_id, start_date=start, end_date=end)
    if not expenses:
        await query.edit_message_text(f"{title}\n\n❌ Нет трат за этот период.")
        return IDLE

    total = sum(e["amount"] for e in expenses)
    lines = [f"{title}", f"💸 Всего: {eur(total)}", f"📝 Трат: {len(expenses)}\n"]

    by_cat: dict[str, float] = {}
    for e in expenses:
        cat_name = e["category_name"]
        by_cat[cat_name] = by_cat.get(cat_name, 0) + e["amount"]

    lines.append("По категориям:")
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"  • {cat}: {eur(amt)}")

    weekly = await db.get_weekly_spending_by_category(user_id)
    limits = [w for w in weekly if w.get("weekly_limit")]
    if limits:
        lines.append("\nЛимиты (неделя):")
        for w in limits:
            pct = (w["spent"] / w["weekly_limit"] * 100) if w["weekly_limit"] else 0
            icon = "🔴" if pct >= 100 else ("🟡" if pct >= 80 else "🟢")
            lines.append(f"  {icon} {w['category_name']}: {eur(w['spent'])} / {eur(w['weekly_limit'])}")

    lines.append(f"\n📋 Последние траты:")
    for e in expenses[:10]:
        desc = f" — {e['description']}" if e["description"] else ""
        lines.append(f"  • {e['expense_date']} {e['created_at'][11:16]} | {eur(e['amount'])} | {e['category_name']}{desc}")

    await query.edit_message_text("\n".join(lines))
    return IDLE


# ===================== SUBSCRIPTIONS =====================

async def show_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    subs = await db.get_subscriptions(user_id)
    if not subs:
        keyboard = [[InlineKeyboardButton("➕ Добавить подписку", callback_data="sub_add")]]
        await update.message.reply_text("📋 Подписок пока нет.", reply_markup=InlineKeyboardMarkup(keyboard))
        return IDLE

    lines = ["📋 Подписки:\n"]
    total_monthly = 0
    for s in subs:
        lines.append(f"• {s['name']}: {eur(s['amount'])} / {s['period']}")
        if s["period"] == "monthly":
            total_monthly += s["amount"]
        elif s["period"] == "yearly":
            total_monthly += s["amount"] / 12
    lines.append(f"\n💸 В месяц: {eur(total_monthly)}")

    keyboard = [
        [InlineKeyboardButton("➕ Добавить", callback_data="sub_add")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="sub_del")],
    ]
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))
    return IDLE


async def sub_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введи название подписки:")
    return ADDSUB_NAME


async def sub_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sub_name"] = update.message.text.strip()
    await update.message.reply_text("Введи сумму (в EUR):")
    return ADDSUB_AMOUNT


async def sub_add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Не могу распознать сумму. Попробуй ещё раз:")
        return ADDSUB_AMOUNT
    context.user_data["sub_amount"] = amount

    keyboard = [
        [InlineKeyboardButton("📅 Ежемесячно", callback_data="period_monthly")],
        [InlineKeyboardButton("📅 Ежегодно", callback_data="period_yearly")],
    ]
    await update.message.reply_text("Выбери период:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADDSUB_PERIOD


async def sub_add_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    period = "monthly" if query.data == "period_monthly" else "yearly"
    name = context.user_data["sub_name"]
    amount = context.user_data["sub_amount"]
    await db.add_subscription(user_id, name, amount, period, None)
    context.user_data.clear()
    await query.edit_message_text(f"✅ Подписка «{name}» добавлена: {eur(amount)} / {period}")
    return IDLE


async def sub_del_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    subs = await db.get_subscriptions(user_id)
    if not subs:
        await query.edit_message_text("❌ Нет подписок для удаления.")
        return IDLE

    buttons = []
    for s in subs:
        buttons.append([InlineKeyboardButton(f"🗑 {s['name']} ({eur(s['amount'])})", callback_data=f"subdel_{s['id']}")])
    await query.edit_message_text("Выбери подписку для удаления:", reply_markup=InlineKeyboardMarkup(buttons))
    return IDLE


async def sub_del_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    sub_id = int(query.data.split("_")[1])
    await db.delete_subscription(user_id, sub_id)
    await query.edit_message_text("✅ Подписка удалена.")
    return IDLE


# ===================== SETTINGS CALLBACKS =====================

async def settings_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚙️ Настройки:", reply_markup=settings_keyboard())
    return IDLE


async def settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "settings_back":
        await query.edit_message_text("Меню:", reply_markup=MENU_KEYBOARD)
    elif data == "setbalance":
        await query.edit_message_text("Введи новый баланс (в EUR):")
        return BALANCE_INPUT
    elif data == "addcat":
        await query.edit_message_text("Введи название новой категории:")
        return ADDCAT_NAME
    elif data == "setlimit":
        user_id = update.effective_user.id
        cats = await db.get_categories(user_id)
        buttons = []
        row = []
        for c in cats:
            row.append(InlineKeyboardButton(c["name"], callback_data=f"limit_{c['name']}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
        await query.edit_message_text("Выбери категорию для лимита:", reply_markup=InlineKeyboardMarkup(buttons))
        return SETLIMIT_CATEGORY
    elif data == "rates":
        rates = await get_exchange_rates()
        if not rates:
            await query.edit_message_text("❌ Не удалось получить курсы валют.")
            return IDLE
        lines = ["💱 Курсы валют (EUR база):\n"]
        for currency, rate in rates.items():
            lines.append(f"• 1 EUR = {rate:.4f} {currency}")
        await query.edit_message_text("\n".join(lines))
    elif data == "clearchat":
        return await clear_chat(update, context)
    elif data == "export":
        user_id = update.effective_user.id
        expenses = await db.get_expenses(user_id, limit=10000)
        if not expenses:
            await query.edit_message_text("❌ Нет трат для экспорта.")
            return IDLE
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["Дата", "Сумма (EUR)", "Категория", "Описание", "Валюта", "Исходная сумма"])
        for e in expenses:
            writer.writerow([e["expense_date"], e["amount"], e["category_name"], e["description"] or "", e["currency"], e["amount"]])
        csv_content = output.getvalue()
        await update.effective_message.reply_document(
            document=csv_content.encode("utf-8-sig"),
            filename="expenses.csv",
            caption=f"📤 Экспортировано {len(expenses)} трат.",
        )
    return IDLE


# ===================== LIMITS =====================

async def setlimit_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    context.user_data["limit_category"] = query.data.replace("limit_", "")
    await query.edit_message_text(f"Введи недельный лимит для «{context.user_data['limit_category']}» (в EUR):")
    return SETLIMIT_AMOUNT


async def setlimit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        limit = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Не могу распознать сумму. Попробуй ещё раз:")
        return SETLIMIT_AMOUNT

    cat_name = context.user_data["limit_category"]
    await db.set_category_limit(user_id, cat_name, limit)
    await update.message.reply_text(f"✅ Лимит для «{cat_name}»: {eur(limit)} в неделю", reply_markup=MENU_KEYBOARD)
    context.user_data.clear()
    return IDLE


# ===================== ADD CATEGORY =====================

async def addcat_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.message.text.strip()
    await db.add_category(user_id, name)
    await update.message.reply_text(f"✅ Категория «{name}» добавлена.", reply_markup=MENU_KEYBOARD)
    return IDLE


# ===================== MAIN =====================

def main():
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, start),
        ],
        states={
            IDLE: [
                MessageHandler(filters.Regex("^💶 Баланс$"), show_balance),
                MessageHandler(filters.Regex("^➕ Добавить трату$"), add_start),
                MessageHandler(filters.Regex("^📊 Статистика$"), stats_prompt),
                MessageHandler(filters.Regex("^📋 Подписки$"), show_subs),
                MessageHandler(filters.Regex("^✏️ Редактировать$"), edit_start),
                MessageHandler(filters.Regex("^🗑 Удалить$"), delete_start),
                MessageHandler(filters.Regex("^⚙️ Настройки$"), settings_prompt),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                CallbackQueryHandler(stats_cb, pattern="^stats_"),
                CallbackQueryHandler(settings_cb, pattern="^(settings_|setbalance$|addcat$|setlimit$|rates$|export$|clearchat$)"),
                CallbackQueryHandler(sub_add_start, pattern="^sub_add$"),
                CallbackQueryHandler(sub_del_start, pattern="^sub_del$"),
                CallbackQueryHandler(sub_del_confirm, pattern="^subdel_"),
            ],
            ADD_AMOUNT: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount),
            ],
            ADD_CATEGORY: [CallbackQueryHandler(add_category, pattern="^(cat_|cancel)")],
            ADD_DATE_YEAR: [
                CallbackQueryHandler(add_date_year, pattern="^(year_|cancel)"),
                CallbackQueryHandler(add_date_back_year, pattern="^back_year$"),
            ],
            ADD_DATE_MONTH: [
                CallbackQueryHandler(add_date_month, pattern="^(month_|cancel)"),
                CallbackQueryHandler(add_date_back_month, pattern="^back_month_"),
            ],
            ADD_DATE_DAY: [CallbackQueryHandler(add_date_day, pattern="^(day_|cancel)")],
            ADD_DESC: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc),
            ],
            EDIT_DATE_YEAR: [
                CallbackQueryHandler(edit_date_year, pattern="^(year_|cancel)"),
                CallbackQueryHandler(edit_date_back_year, pattern="^back_year$"),
            ],
            EDIT_DATE_MONTH: [
                CallbackQueryHandler(edit_date_month, pattern="^(month_|cancel)"),
                CallbackQueryHandler(edit_date_back_month, pattern="^back_month_"),
            ],
            EDIT_DATE_DAY: [CallbackQueryHandler(edit_date_day, pattern="^(day_|cancel)")],
            EDIT_SELECT: [CallbackQueryHandler(edit_select, pattern="^(edit_select_|cancel)")],
            EDIT_FIELD: [CallbackQueryHandler(edit_field, pattern="^(edit_field_|cancel)")],
            EDIT_VALUE: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value),
                CallbackQueryHandler(edit_value, pattern="^(cat_|year_|month_|day_|back_year$|back_month_|cancel)"),
            ],
            EDIT_NEW_DATE_YEAR: [
                CallbackQueryHandler(edit_value, pattern="^(year_|cancel)"),
                CallbackQueryHandler(edit_value, pattern="^back_year$"),
            ],
            EDIT_NEW_DATE_MONTH: [
                CallbackQueryHandler(edit_value, pattern="^(month_|cancel)"),
                CallbackQueryHandler(edit_value, pattern="^back_month_"),
            ],
            EDIT_NEW_DATE_DAY: [CallbackQueryHandler(edit_value, pattern="^(day_|cancel)")],
            DEL_DATE_YEAR: [
                CallbackQueryHandler(delete_date_year, pattern="^(year_|cancel)"),
                CallbackQueryHandler(delete_date_back_year, pattern="^back_year$"),
            ],
            DEL_DATE_MONTH: [
                CallbackQueryHandler(delete_date_month, pattern="^(month_|cancel)"),
                CallbackQueryHandler(delete_date_back_month, pattern="^back_month_"),
            ],
            DEL_DATE_DAY: [CallbackQueryHandler(delete_date_day, pattern="^(day_|cancel)")],
            DEL_SELECT: [
                CallbackQueryHandler(delete_select, pattern="^(del_select_|cancel)"),
                CallbackQueryHandler(delete_confirm, pattern="^del_confirm_"),
            ],
            ADDSUB_NAME: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sub_add_name),
            ],
            ADDSUB_AMOUNT: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sub_add_amount),
            ],
            ADDSUB_PERIOD: [CallbackQueryHandler(sub_add_period, pattern="^period_")],
            ADDCAT_NAME: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcat_name),
            ],
            SETLIMIT_CATEGORY: [CallbackQueryHandler(setlimit_category, pattern="^(limit_|cancel)")],
            SETLIMIT_AMOUNT: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setlimit_amount),
            ],
            BALANCE_INPUT: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setbalance_value),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(cancel, pattern="^cancel$"),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
        ],
    )

    app = Application.builder().token(TOKEN).build()
    app.add_handler(conv)

    # Middleware для логирования всех сообщений (для очистки чата)
    app.add_handler(MessageHandler(filters.ALL, log_message), group=1)

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
