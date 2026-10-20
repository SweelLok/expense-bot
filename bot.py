import os
import csv
import time
import asyncio
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

from budget import build_plan, monthly_amount, months_of, period_label, month_bounds, warning_lines
from db import Database, SET_PLANNED_INCOME, SET_PLAN_MODE, SET_PLAN_BUDGET, SET_WARN_THRESHOLD

load_dotenv()

# --- Часовой пояс ---------------------------------------------------------
# Контейнер по умолчанию живёт в UTC: тогда «сегодня» съезжает на день назад,
# вечером трата пишется вчерашним числом, а ночью календарь блокирует текущий день.
# Берём зону из TZ (в проде задано в fly.toml), иначе — Bratislava.
DEFAULT_TZ = "Europe/Bratislava"
if not os.environ.get("TZ"):
    os.environ["TZ"] = DEFAULT_TZ
try:
    time.tzset()          # чтобы локальное время подхватилось сразу, а не только при старте
except AttributeError:    # не POSIX — оставляем системную зону
    pass


def now_local() -> datetime:
    """Текущее время в зоне пользователя (TZ), а не в UTC контейнера."""
    return datetime.now()

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
    INCOME_AMOUNT, INCOME_DESC, INCOME_DATE,
    SETWARN_AMOUNT, SETPLAN_AMOUNT,
    ADDSUB_MONTHS, ADDSUB_CATEGORY,
    SUB_EDIT_SELECT, SUB_EDIT_FIELD, SUB_EDIT_AMOUNT, SUB_EDIT_MONTHS, SUB_EDIT_CATEGORY, SUB_EDIT_NAME,
    SETBUDGET_AMOUNT,
    DEL_KIND, DELINC_SELECT,
) = range(43)

DEFAULT_CATEGORIES = ["еда", "бытовое", "подписки", "общежитие", "стрижка", "транспорт",
                      "развлечения", "прочее", "резерв"]
DEFAULT_WARN_THRESHOLD = 7.0
DEFAULT_PERIOD_MONTHS = 1.0
DEFAULT_PLAN_BUDGET = 350.0

# От чего считается план месяца
PLAN_MODE_AVAILABLE = "available"   # деньги в наличии: баланс + потрачено с 1-го числа
PLAN_MODE_INCOME = "income"         # доход этого месяца → плановый доход → баланс
PLAN_MODE_BUDGET = "budget"         # фиксированный бюджет месяца из настроек

PLAN_MODE_LABELS = {
    PLAN_MODE_AVAILABLE: "деньги в наличии",
    PLAN_MODE_INCOME: "доход этого месяца",
    PLAN_MODE_BUDGET: "бюджет месяца",
}

MONTH_NAMES = ["", "январь", "февраль", "март", "апрель", "май", "июнь",
               "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]

exchange_rates: dict[str, float] = {}
last_rate_update: datetime | None = None

db = Database()

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("💶 Баланс"), KeyboardButton("🎯 Бюджет")],
        [KeyboardButton("➕ Добавить трату"), KeyboardButton("💰 Добавить доход")],
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


def parse_date_input(text: str) -> str | None:
    """Разбирает дату из текста: '12.10', '12.10.2026', '-' / пусто = сегодня."""
    text = (text or "").strip()
    if text in ("", "-", "сегодня", "today"):
        return datetime.now().strftime("%Y-%m-%d")
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    try:
        day, month = text.split(".")
        return datetime(datetime.now().year, int(month), int(day)).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


async def load_plan(user_id: int) -> dict:
    """Собирает месячный план из данных пользователя.

    От чего считается план — настройка plan_mode:
      available — деньги в наличии: баланс + потрачено с 1-го числа (не зависит от дохода);
      income    — доход этого месяца → плановый доход → баланс;
      budget    — фиксированный бюджет месяца из настроек.
    План категорий от базы не зависит: у каждой категории свой фикс-план,
    а остаток базы забирают категории в режиме «авто» (обычно «резерв»).
    """
    month_start, today_str, _ = month_bounds()
    income_this_month = await db.get_income_total(user_id, month_start, today_str)
    planned_income = await db.get_setting_float(user_id, SET_PLANNED_INCOME, 0.0)
    plan_budget = await db.get_setting_float(user_id, SET_PLAN_BUDGET, DEFAULT_PLAN_BUDGET)
    balance = await db.get_balance(user_id)
    mode = await db.get_setting(user_id, SET_PLAN_MODE, PLAN_MODE_AVAILABLE) or PLAN_MODE_AVAILABLE
    if mode not in PLAN_MODE_LABELS:
        mode = PLAN_MODE_AVAILABLE

    categories = await db.get_categories(user_id)
    subs = await db.get_subscriptions(user_id)
    spending = await db.get_spending_by_category(user_id, month_start, today_str)
    spent_by_cat = {row["category_name"]: float(row["spent"]) for row in spending}
    spent_this_month = sum(spent_by_cat.values())

    if mode == PLAN_MODE_BUDGET:
        base, base_source = plan_budget, "бюджет месяца"
    elif mode == PLAN_MODE_INCOME:
        if income_this_month > 0:
            base, base_source = income_this_month, "доход этого месяца"
        elif planned_income > 0:
            base, base_source = planned_income, "плановый доход"
        else:
            base, base_source = balance, "текущий баланс"
    else:
        base, base_source = balance + spent_this_month, "деньги в наличии"

    warn_threshold = await db.get_setting_float(user_id, SET_WARN_THRESHOLD, DEFAULT_WARN_THRESHOLD)
    plan = build_plan(base, categories, subs, spent_by_cat, warn_threshold)
    today_rows = await db.get_daily_spending(user_id, today_str, today_str)
    plan["today_spent"] = float(today_rows[0]["spent"]) if today_rows else 0.0
    plan["today_date"] = today_str
    plan["base_source"] = base_source
    plan["mode"] = mode
    plan["mode_label"] = PLAN_MODE_LABELS[mode]
    plan["income_this_month"] = income_this_month
    plan["planned_income"] = planned_income
    plan["plan_budget"] = plan_budget
    plan["balance"] = balance
    plan["spent_this_month"] = spent_this_month
    return plan


def leftover_line(plan: dict) -> str:
    """Куда уходят не распределённые деньги: в авто-категории (обычно «резерв») или никуда."""
    if plan["unallocated"] < 0:
        return f"🔴 План не сходится: не хватает {eur(abs(plan['unallocated']))}"
    autos = [it for it in plan["items"] if it["auto"]]
    if autos:
        total = sum(it["plan"] for it in autos)
        names = ", ".join(f"«{it['name']}»" for it in autos)
        return f"♻️ Остаток уходит в {names}: {eur(total)}"
    return f"🟢 Не распределено: {eur(plan['unallocated'])}"


def plan_text(plan: dict) -> str:
    """Текст плана на месяц: шапка, строки категорий, предупреждения."""
    lines = [
        f"🎯 План на месяц ({plan['base_source']}: {eur(plan['base'])})",
        "",
    ]
    if plan["mode"] == PLAN_MODE_AVAILABLE:
        lines.append(f"💶 Баланс {eur(plan['balance'])} + потрачено с 1-го числа {eur(plan['spent_this_month'])}")
    elif plan["mode"] == PLAN_MODE_BUDGET:
        lines.append(f"💵 Бюджет месяца (настройка): {eur(plan['plan_budget'])}")

    if plan.get("today_spent") is not None:
        lines.append(f"📅 Сегодня потрачено: {eur(plan['today_spent'])}")

    lines += [
        f"📋 Периодические платежи: {eur(plan['periodic_total'])}",
        f"⚙️ Фикс-планы категорий: {eur(plan['fixed_total'])}",
        f"➡️ Распределено всего: {eur(plan['planned_total'])}",
        leftover_line(plan),
    ]

    lines.append("")
    for it in plan["items"]:
        if it["plan"] <= 0 and it["spent"] <= 0:
            continue
        if it["left"] < 0:
            icon = "🔴"
        elif it["left"] <= plan["warn_threshold"]:
            icon = "🟡"
        else:
            icon = "🟢"
        tail = ""
        if it["plan"] > 0:
            tail = f" | остаток {eur(it['left'])} ({it['per_day']:.2f}€/день)"
        lines.append(f"{icon} {it['name']}: {eur(it['spent'])} / {eur(it['plan'])}{tail}")

    warn = warning_lines(plan)
    if warn:
        lines.append("")
        lines.extend(warn)

    if plan["days_left"]:
        lines.append("")
        lines.append(f"📅 До конца месяца: {plan['days_left']} дн.")
    return "\n".join(lines)


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


async def sub_category_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Выбор категории для периодического платежа."""
    cats = await db.get_categories(user_id)
    buttons = []
    row = []
    for c in cats:
        row.append(InlineKeyboardButton(c["name"], callback_data=f"subcat_{c['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("➖ Без категории", callback_data="subcat_none")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def stats_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📅 За неделю", callback_data="stats_week")],
        [InlineKeyboardButton("📆 За месяц", callback_data="stats_month")],
        [InlineKeyboardButton("🈷️ По дням месяца", callback_data="stats_days")],
        [InlineKeyboardButton("📆 За год", callback_data="stats_year")],
        [InlineKeyboardButton("📋 Все траты", callback_data="stats_all")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="stats_back")],
    ]
    return InlineKeyboardMarkup(keyboard)


def settings_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("💰 Изменить баланс", callback_data="setbalance")],
        [InlineKeyboardButton("🎯 Основа плана", callback_data="setmode")],
        [InlineKeyboardButton("💵 Бюджет месяца", callback_data="setbudget")],
        [InlineKeyboardButton("💵 Плановый доход", callback_data="setplan")],
        [InlineKeyboardButton("🎯 Месячные планы", callback_data="setlimit")],
        [InlineKeyboardButton("🔔 Порог предупреждения", callback_data="setwarn")],
        [InlineKeyboardButton("📋 Добавить категорию", callback_data="addcat")],
        [InlineKeyboardButton("💱 Курсы валют", callback_data="rates")],
        [InlineKeyboardButton("📤 Экспорт CSV", callback_data="export")],
        [InlineKeyboardButton("🧹 Очистить чат", callback_data="clearchat")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")],
    ]
    return InlineKeyboardMarkup(keyboard)


def plan_mode_keyboard(mode: str, budget: float) -> InlineKeyboardMarkup:
    """Кнопки выбора основы плана, текущий режим помечен галочкой."""
    def label(flag: bool, text: str) -> str:
        return ("✅ " if flag else "") + text

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label(mode == PLAN_MODE_AVAILABLE, "💶 Деньги в наличии"),
                              callback_data=f"mode_{PLAN_MODE_AVAILABLE}")],
        [InlineKeyboardButton(label(mode == PLAN_MODE_INCOME, "💰 Доход этого месяца"),
                              callback_data=f"mode_{PLAN_MODE_INCOME}")],
        [InlineKeyboardButton(label(mode == PLAN_MODE_BUDGET, f"💵 Бюджет месяца ({budget:.2f}€)"),
                              callback_data=f"mode_{PLAN_MODE_BUDGET}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


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
        "💶 Баланс — баланс, траты за месяц, состояние плана\n"
        "🎯 Бюджет — план на месяц по категориям\n"
        "➕ Добавить трату — записать расход\n"
        "💰 Добавить доход — записать приход денег\n"
        "📊 Статистика — отчёты\n"
        "📋 Подписки — подписки и другие регулярные платежи\n"
        "✏️ Редактировать — изменить трату\n"
        "🗑 Удалить — удалить трату или доход\n"
        "⚙️ Настройки — баланс, плановый доход, месячные планы, порог\n"
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
    plan = await load_plan(user_id)
    month_income = plan["income_this_month"]
    month_spent = plan["spent_total"]

    lines = [
        f"💶 Текущий баланс: {eur(bal)}",
        f"💸 Потрачено за неделю: {eur(week_spent)}",
        "",
        f"🎯 План месяца ({plan['base_source']}: {eur(plan['base'])})",
        f"   💰 Доход за месяц: {eur(month_income)}",
        f"   📋 Регулярные платежи: {eur(plan['periodic_total'])}",
        f"   💸 Потрачено с 1-го числа: {eur(month_spent)}",
        "   " + leftover_line(plan),
    ]

    warn = warning_lines(plan)
    if warn:
        lines.append("")
        lines.extend(warn)

    lines.append("\n📊 Бюджет по категориям: кнопка 🎯 Бюджет")
    lines.append("💰 Изменить баланс: ⚙️ Настройки → 💰 Изменить баланс")
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


# ===================== INCOME =====================

async def income_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "💰 Новый доход\n\nШаг 1/3: Введи сумму (можно с валютой: 350, 400 USD, 15000 UAH):"
    )
    return INCOME_AMOUNT


async def income_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    try:
        amount = float(parts[0].replace(",", "."))
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Не могу распознать сумму. Попробуй ещё раз:")
        return INCOME_AMOUNT

    currency = "EUR"
    if len(parts) > 1 and parts[1].upper() in ("EUR", "USD", "UAH"):
        currency = parts[1].upper()

    eur_amount = await to_eur(amount, currency)
    context.user_data["income_amount"] = amount
    context.user_data["income_currency"] = currency
    context.user_data["income_eur"] = eur_amount
    await update.message.reply_text(
        f"Шаг 1/3: ✅ {amount} {currency} = {eur(eur_amount)}\n\nШаг 2/3: Введи описание (или /skip):"
    )
    return INCOME_DESC


async def income_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["income_desc"] = None if text == "/skip" else text
    await update.message.reply_text("Шаг 3/3: Дата дохода — 12.10, 12.10.2026, или «-» / «сегодня»:")
    return INCOME_DATE


async def income_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    income_date = parse_date_input(update.message.text)
    if not income_date:
        await update.message.reply_text("❌ Не понял дату. Формат: 12.10, 12.10.2026 или «-» для сегодня:")
        return INCOME_DATE

    amount = context.user_data["income_amount"]
    currency = context.user_data["income_currency"]
    eur_amount = context.user_data["income_eur"]
    description = context.user_data.get("income_desc")

    await db.add_income(user_id, eur_amount, currency, description, income_date)
    await db.add_to_balance(user_id, eur_amount)
    context.user_data.clear()

    plan = await load_plan(user_id)
    lines = [
        "✅ Доход добавлен!",
        "",
        f"💰 {amount} {currency} ({eur(eur_amount)})",
        f"📅 {income_date}",
        f"📝 {description or '—'}",
        f"💶 Баланс: {eur(plan['balance'])}",
        "",
        f"🎯 План месяца ({plan['base_source']}: {eur(plan['base'])})",
        f"📋 Регулярные платежи: {eur(plan['periodic_total'])}",
        leftover_line(plan),
    ]
    warn = warning_lines(plan)
    if warn:
        lines.append("")
        lines.extend(warn)
    await update.message.reply_text("\n".join(lines), reply_markup=MENU_KEYBOARD)
    return IDLE


# ===================== BUDGET =====================

async def show_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    plan = await load_plan(user_id)
    await update.message.reply_text(plan_text(plan), reply_markup=MENU_KEYBOARD)
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

    plan = await load_plan(user_id)
    warnings = warning_lines(plan, only_category=cat_name)
    limit_msg = ("\n\n" + "\n".join(warnings)) if warnings else ""

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
    expenses_total = await db.get_expenses_count(user_id)
    incomes = await db.get_incomes(user_id, limit=1)
    if expenses_total == 0 and not incomes:
        await update.message.reply_text("❌ Нечего удалять — нет ни трат, ни доходов.", reply_markup=MENU_KEYBOARD)
        return IDLE

    buttons = []
    if expenses_total:
        buttons.append([InlineKeyboardButton("🧾 Трату", callback_data="delkind_expense")])
    if incomes:
        buttons.append([InlineKeyboardButton("💰 Доход", callback_data="delkind_income")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await update.message.reply_text(
        "🗑 Удаление\n\nЧто удалить?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return DEL_KIND


async def delete_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    if query.data == "delkind_expense":
        today = datetime.now()
        await query.edit_message_text(
            "🗑 Удаление траты\n\nШаг 1/3: Выбери дату:",
            reply_markup=date_year_keyboard(today.year),
        )
        return DEL_DATE_YEAR

    # delkind_income
    user_id = update.effective_user.id
    incomes = await db.get_incomes(user_id, limit=20)
    if not incomes:
        await query.edit_message_text("❌ Нет доходов для удаления.", reply_markup=MENU_KEYBOARD)
        context.user_data.clear()
        return IDLE

    lines = [f"🗑 Доходы ({len(incomes)} последних):\n"]
    buttons = []
    for inc in incomes:
        desc = f" — {inc['description']}" if inc["description"] else ""
        lines.append(f"• {eur(inc['amount'])} | {inc['income_date']}{desc}")
        label = f"💰 {eur(inc['amount'])} {inc['income_date']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"delinc_select_{inc['id']}")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await query.edit_message_text(
        "\n".join(lines) + "\n\nВыбери доход для удаления:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return DELINC_SELECT


async def deleteinc_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    inc_id = int(query.data.split("_")[-1])
    context.user_data["del_income_id"] = inc_id

    inc = await db.get_income_by_id(update.effective_user.id, inc_id)
    if not inc:
        await query.edit_message_text("❌ Доход не найден.")
        context.user_data.clear()
        return IDLE

    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="delinc_confirm_yes")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    await query.edit_message_text(
        f"Удалить доход?\n\n"
        f"💰 {eur(inc['amount'])}\n"
        f"📅 {inc['income_date']}\n"
        f"📝 {inc['description'] or '—'}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return DELINC_SELECT


async def deleteinc_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "delinc_confirm_yes":
        inc_id = context.user_data.get("del_income_id")
        inc = await db.get_income_by_id(user_id, inc_id)
        if inc:
            await db.delete_income(user_id, inc_id)
            await db.add_to_balance(user_id, -inc["amount"])
            await query.edit_message_text(
                f"✅ Доход удалён. Баланс: {eur(await db.get_balance(user_id))}"
            )
        else:
            await query.edit_message_text("❌ Доход не найден.")
    else:
        await query.edit_message_text("❌ Отменено.")

    context.user_data.clear()
    return IDLE


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


# ===================== ОТЧЁТ ПО ДНЯМ =====================

async def days_text(user_id: int) -> str:
    """Расходы по каждому дню текущего месяца; сегодняшний день всегда актуальный."""
    now = now_local()
    month_start, today_str, days_left = month_bounds(now)
    rows = await db.get_daily_spending(user_id, month_start, today_str)
    by_day = {r["day"]: r for r in rows}
    plan = await load_plan(user_id)

    lines = [f"📅 Расходы по дням — {MONTH_NAMES[now.month]} {now.year}", ""]
    total = 0.0
    for day in range(1, now.day + 1):
        date = datetime(now.year, now.month, day).strftime("%Y-%m-%d")
        row = by_day.get(date)
        amount = float(row["spent"]) if row else 0.0
        total += amount
        mark = "  ← сегодня" if day == now.day else ""
        value = eur(amount) if amount > 0 else "—"
        lines.append(f"{day:02d}.{now.month:02d}{mark}: {value}")

    average = total / now.day if now.day else 0.0
    left = plan["base"] - plan["spent_total"]
    lines += [
        "",
        f"💸 За {now.day} дн.: {eur(total)} (в среднем {eur(average)}/день)",
        f"🎯 План месяца: {eur(plan['planned_total'])} | потрачено {eur(plan['spent_total'])} | "
        f"остаток {eur(left)}",
    ]
    if days_left:
        lines.append(f"📆 До конца месяца {days_left} дн. → {eur(left / days_left)}/день")
    return "\n".join(lines)


async def show_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(await days_text(user_id), reply_markup=MENU_KEYBOARD)
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

    if data == "stats_days":
        await query.edit_message_text(await days_text(user_id))
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
    income = await db.get_income_total(user_id, start_date=start, end_date=end)

    if not expenses and income <= 0:
        await query.edit_message_text(f"{title}\n\n❌ Нет трат и доходов за этот период.")
        return IDLE

    total = sum(e["amount"] for e in expenses)
    lines = [
        f"{title}",
        f"💰 Доход: {eur(income)}",
        f"💸 Расходы: {eur(total)}",
        f"📈 Разница: {eur(income - total)}",
        f"📝 Трат: {len(expenses)}\n",
    ]

    by_cat: dict[str, float] = {}
    for e in expenses:
        cat_name = e["category_name"]
        by_cat[cat_name] = by_cat.get(cat_name, 0) + e["amount"]

    if by_cat:
        lines.append("По категориям:")
        for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"  • {cat}: {eur(amt)}")

    if data == "stats_month":
        plan = await load_plan(user_id)
        lines.append("")
        lines.append(f"🎯 План месяца ({plan['base_source']}: {eur(plan['base'])})")
        for it in plan["items"]:
            if it["plan"] <= 0 and it["spent"] <= 0:
                continue
            icon = "🔴" if it["left"] < 0 else ("🟡" if it["left"] <= plan["warn_threshold"] else "🟢")
            tail = f" | остаток {eur(it['left'])}" if it["plan"] > 0 else ""
            lines.append(f"  {icon} {it['name']}: {eur(it['spent'])} / {eur(it['plan'])}{tail}")

    if expenses:
        lines.append("\n📋 Последние траты:")
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

    lines = ["📋 Подписки и регулярные платежи:\n"]
    total_monthly = 0.0
    for s in subs:
        per_month = monthly_amount(s)
        total_monthly += per_month
        cat = s.get("category_name") or "подписки"
        lines.append(
            f"• {s['name']}: {eur(s['amount'])} {period_label(months_of(s))} "
            f"→ {eur(per_month)}/мес | категория: {cat}"
        )
    lines.append(f"\n💸 В месяц: {eur(total_monthly)}")

    plan = await load_plan(user_id)
    lines.append(f"🎯 Из плана месяца: {eur(plan['periodic_total'])} из {eur(plan['base'])}")
    if plan["unallocated"] < 0:
        lines.append(f"🔴 План не сходится: не хватает {eur(abs(plan['unallocated']))}")

    keyboard = [
        [InlineKeyboardButton("➕ Добавить", callback_data="sub_add")],
        [InlineKeyboardButton("✏️ Изменить", callback_data="sub_edit")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="sub_del")],
    ]
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))
    return IDLE


async def sub_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Введи название платежа (например: Hermes, Fly.io, Общежитие):")
    return ADDSUB_NAME


async def sub_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sub_name"] = update.message.text.strip()
    await update.message.reply_text("Введи сумму (в EUR):")
    return ADDSUB_AMOUNT


async def sub_add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Не могу распознать сумму. Попробуй ещё раз:")
        return ADDSUB_AMOUNT
    context.user_data["sub_amount"] = amount

    keyboard = [
        [InlineKeyboardButton("📅 Ежемесячно", callback_data="period_monthly"),
         InlineKeyboardButton("🗓 Раз в 2 месяца", callback_data="period_2months")],
        [InlineKeyboardButton("🗓 Раз в 3 месяца", callback_data="period_quarterly"),
         InlineKeyboardButton("🗓 Раз в 6 месяцев", callback_data="period_halfyear")],
        [InlineKeyboardButton("📅 Ежегодно", callback_data="period_yearly"),
         InlineKeyboardButton("✏️ Свой период", callback_data="period_custom")],
    ]
    await update.message.reply_text("Выбери период платежа:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADDSUB_MONTHS


async def sub_add_months(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "period_custom":
        await query.edit_message_text(
            "Введи период в месяцах (можно дробно: 1.5 — раз в полтора месяца, 2 — раз в два месяца):"
        )
        return ADDSUB_MONTHS

    presets = {
        "period_monthly": ("monthly", 1.0),
        "period_2months": ("2months", 2.0),
        "period_quarterly": ("quarterly", 3.0),
        "period_halfyear": ("halfyear", 6.0),
        "period_yearly": ("yearly", 12.0),
    }
    period, months = presets.get(query.data, ("monthly", 1.0))
    context.user_data["sub_period"] = period
    context.user_data["sub_months"] = months

    keyboard = await sub_category_keyboard(update.effective_user.id)
    await query.edit_message_text(
        f"Период: {period_label(months)} ({eur(monthly_amount({'amount': context.user_data['sub_amount'], 'months': months}))}/мес)\n\n"
        f"К какой категории отнести этот платёж?",
        reply_markup=keyboard,
    )
    return ADDSUB_CATEGORY


async def sub_add_months_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        months = float(update.message.text.strip().replace(",", "."))
        if months <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Нужно положительное число месяцев. Попробуй ещё раз:")
        return ADDSUB_MONTHS

    context.user_data["sub_period"] = "custom"
    context.user_data["sub_months"] = months
    keyboard = await sub_category_keyboard(update.effective_user.id)
    await update.message.reply_text(
        f"Период: {period_label(months)} ({eur(monthly_amount({'amount': context.user_data['sub_amount'], 'months': months}))}/мес)\n\n"
        f"К какой категории отнести этот платёж?",
        reply_markup=keyboard,
    )
    return ADDSUB_CATEGORY


async def sub_add_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    category_id = None if query.data == "subcat_none" else int(query.data.split("_")[1])
    name = context.user_data["sub_name"]
    amount = context.user_data["sub_amount"]
    period = context.user_data.get("sub_period", "monthly")
    months = context.user_data.get("sub_months", 1.0)

    await db.add_subscription(user_id, name, amount, period, None, months=months, category_id=category_id)
    context.user_data.clear()

    plan = await load_plan(user_id)
    lines = [
        f"✅ «{name}» добавлен: {eur(amount)} {period_label(months)} → {eur(amount / months)}/мес",
        "",
        f"📋 Регулярные платежи в месяц: {eur(plan['periodic_total'])}",
        f"🎯 План месяца ({plan['base_source']}: {eur(plan['base'])})",
    ]
    if plan["unallocated"] >= 0:
        lines.append(f"🟢 Свободно сверх плана: {eur(plan['unallocated'])}")
    else:
        lines.append(f"🔴 План не сходится: не хватает {eur(abs(plan['unallocated']))}")

    await query.edit_message_text("\n".join(lines))
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
    sub = await db.get_subscription(user_id, sub_id)
    await db.delete_subscription(user_id, sub_id)
    if sub:
        action = (f"🗑 «{sub['name']}» удалён: {eur(sub['amount'])} {period_label(months_of(sub))} "
                  f"(было {eur(monthly_amount(sub))}/мес)")
    else:
        action = "🗑 Платёж удалён."
    await query.edit_message_text(await build_sub_report(user_id, action))
    return IDLE


# ===================== РЕДАКТИРОВАНИЕ ПЛАТЕЖЕЙ =====================

def sub_fields_keyboard(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💶 Сумма", callback_data=f"subfield_amount_{sub_id}"),
         InlineKeyboardButton("🗓 Период", callback_data=f"subfield_months_{sub_id}")],
        [InlineKeyboardButton("📂 Категория", callback_data=f"subfield_category_{sub_id}"),
         InlineKeyboardButton("✏️ Название", callback_data=f"subfield_name_{sub_id}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data="sub_edit"),
         InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


async def build_sub_report(user_id: int, action: str, sub_id: int | None = None) -> str:
    """Отчёт после изменения платежей: новый план и что стало с категорией."""
    plan = await load_plan(user_id)
    lines = [action, ""]
    if sub_id:
        sub = await db.get_subscription(user_id, sub_id)
        if sub:
            cat = sub.get("category_name") or "без категории"
            lines.append(
                f"{sub['name']}: {eur(sub['amount'])} {period_label(months_of(sub))} "
                f"→ {eur(monthly_amount(sub))}/мес | {cat}"
            )
    lines += [
        f"📋 Регулярные платежи в месяц: {eur(plan['periodic_total'])}",
        f"🎯 План месяца ({plan['base_source']}: {eur(plan['base'])})",
        leftover_line(plan),
    ]

    if sub_id:
        sub = await db.get_subscription(user_id, sub_id)
        if sub and sub.get("category_name"):
            for it in plan["items"]:
                if it["name"] == sub["category_name"]:
                    lines.append(
                        f"📂 «{it['name']}»: план {eur(it['plan'])}, потрачено {eur(it['spent'])}, "
                        f"остаток {eur(it['left'])}"
                    )
                    break
    warn = warning_lines(plan)
    if warn:
        lines.append("")
        lines.extend(warn)
    return "\n".join(lines)


async def sub_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    subs = await db.get_subscriptions(user_id)
    if not subs:
        await query.edit_message_text("❌ Нет платежей, которые можно изменить.")
        return IDLE

    buttons = [
        [InlineKeyboardButton(f"{s['name']} — {eur(s['amount'])} {period_label(months_of(s))}",
                              callback_data=f"subedit_{s['id']}")]
        for s in subs
    ]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    await query.edit_message_text("Что меняем?", reply_markup=InlineKeyboardMarkup(buttons))
    return SUB_EDIT_SELECT


async def sub_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    user_id = update.effective_user.id
    sub_id = int(query.data.split("_")[1])
    sub = await db.get_subscription(user_id, sub_id)
    if not sub:
        await query.edit_message_text("❌ Платёж не найден.")
        return IDLE

    context.user_data["sub_edit_id"] = sub_id
    await query.edit_message_text(
        f"{sub['name']}: {eur(sub['amount'])} {period_label(months_of(sub))} "
        f"→ {eur(monthly_amount(sub))}/мес\n\nЧто меняем?",
        reply_markup=sub_fields_keyboard(sub_id),
    )
    return SUB_EDIT_FIELD


async def sub_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    field, sub_id = parts[1], int(parts[2])
    context.user_data["sub_edit_id"] = sub_id

    if field == "amount":
        await query.edit_message_text("Введи новую сумму (EUR):")
        return SUB_EDIT_AMOUNT
    if field == "name":
        await query.edit_message_text("Введи новое название:")
        return SUB_EDIT_NAME
    if field == "months":
        keyboard = [
            [InlineKeyboardButton("📅 Ежемесячно", callback_data="ep_monthly"),
             InlineKeyboardButton("🗓 Каждые 2 мес.", callback_data="ep_2months")],
            [InlineKeyboardButton("🗓 Каждые 3 мес.", callback_data="ep_quarterly"),
             InlineKeyboardButton("🗓 Каждые 6 мес.", callback_data="ep_halfyear")],
            [InlineKeyboardButton("📅 Ежегодно", callback_data="ep_yearly"),
             InlineKeyboardButton("✏️ Свой период", callback_data="ep_custom")],
        ]
        await query.edit_message_text("Выбери новый период:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SUB_EDIT_MONTHS
    if field == "category":
        keyboard = await sub_category_keyboard(update.effective_user.id)
        await query.edit_message_text("К какой категории отнести платёж?", reply_markup=keyboard)
        return SUB_EDIT_CATEGORY

    await query.edit_message_text("❌ Не понял, что менять.")
    return IDLE


async def sub_edit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Нужна положительная сумма. Попробуй ещё раз:")
        return SUB_EDIT_AMOUNT

    sub_id = context.user_data["sub_edit_id"]
    sub = await db.get_subscription(user_id, sub_id)
    await db.update_subscription(user_id, sub_id, amount=amount)
    context.user_data.clear()
    report = await build_sub_report(
        user_id, f"✏️ Сумма изменена: {eur(sub['amount'])} → {eur(amount)}", sub_id
    )
    await update.message.reply_text(report, reply_markup=MENU_KEYBOARD)
    return IDLE


async def sub_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Название не может быть пустым:")
        return SUB_EDIT_NAME
    sub_id = context.user_data["sub_edit_id"]
    sub = await db.get_subscription(user_id, sub_id)
    await db.update_subscription(user_id, sub_id, name=name)
    context.user_data.clear()
    report = await build_sub_report(user_id, f"✏️ «{sub['name']}» → «{name}»", sub_id)
    await update.message.reply_text(report, reply_markup=MENU_KEYBOARD)
    return IDLE


async def sub_edit_months(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    sub_id = context.user_data["sub_edit_id"]
    sub = await db.get_subscription(user_id, sub_id)

    if query.data == "ep_custom":
        await query.edit_message_text(
            f"Текущий период: {period_label(months_of(sub))}.\n"
            f"Введи новый период в месяцах (можно дробно: 1.5 — каждые полтора месяца):"
        )
        return SUB_EDIT_MONTHS

    presets = {
        "ep_monthly": ("monthly", 1.0),
        "ep_2months": ("2months", 2.0),
        "ep_quarterly": ("quarterly", 3.0),
        "ep_halfyear": ("halfyear", 6.0),
        "ep_yearly": ("yearly", 12.0),
    }
    if query.data not in presets:
        await query.edit_message_text("❌ Не понял период.")
        return IDLE

    period, months = presets[query.data]
    old = period_label(months_of(sub))
    await db.update_subscription(user_id, sub_id, period=period, months=months)
    context.user_data.clear()
    report = await build_sub_report(
        user_id, f"✏️ Период изменён: {old} → {period_label(months)}", sub_id
    )
    await query.edit_message_text(report)
    return IDLE


async def sub_edit_months_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        months = float(update.message.text.strip().replace(",", "."))
        if months <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Нужно положительное число месяцев. Попробуй ещё раз:")
        return SUB_EDIT_MONTHS

    sub_id = context.user_data["sub_edit_id"]
    sub = await db.get_subscription(user_id, sub_id)
    old = period_label(months_of(sub))
    await db.update_subscription(user_id, sub_id, period="custom", months=months)
    context.user_data.clear()
    report = await build_sub_report(
        user_id, f"✏️ Период изменён: {old} → {period_label(months)}", sub_id
    )
    await update.message.reply_text(report, reply_markup=MENU_KEYBOARD)
    return IDLE


async def sub_edit_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    sub_id = context.user_data["sub_edit_id"]
    category_id = None if query.data == "subcat_none" else int(query.data.split("_")[1])
    sub = await db.get_subscription(user_id, sub_id)
    old_cat = sub.get("category_name") or "без категории"
    await db.update_subscription(user_id, sub_id, category_id=category_id)
    context.user_data.clear()

    new_sub = await db.get_subscription(user_id, sub_id)
    report = await build_sub_report(
        user_id, f"✏️ Категория изменена: {old_cat} → {new_sub.get('category_name') or 'без категории'}", sub_id
    )
    await query.edit_message_text(report)
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
    elif data == "setplan":
        user_id = update.effective_user.id
        current = await db.get_setting_float(user_id, SET_PLANNED_INCOME, 0.0)
        await query.edit_message_text(
            f"Введи плановый доход месяца в EUR (сейчас: {eur(current)}).\n"
            f"Он используется, пока за этот месяц нет реальных доходов. 0 — не использовать:"
        )
        return SETPLAN_AMOUNT
    elif data == "setwarn":
        user_id = update.effective_user.id
        current = await db.get_setting_float(user_id, SET_WARN_THRESHOLD, DEFAULT_WARN_THRESHOLD)
        await query.edit_message_text(
            f"За сколько EUR до нуля предупреждать о заканчивающейся категории? Сейчас: {eur(current)}"
        )
        return SETWARN_AMOUNT
    elif data == "setmode":
        user_id = update.effective_user.id
        mode = await db.get_setting(user_id, SET_PLAN_MODE, PLAN_MODE_AVAILABLE) or PLAN_MODE_AVAILABLE
        budget = await db.get_setting_float(user_id, SET_PLAN_BUDGET, DEFAULT_PLAN_BUDGET)
        await query.edit_message_text(
            "От чего считать план на месяц?\n\n"
            "💶 Деньги в наличии — от того, сколько у тебя есть (баланс + потрачено с 1-го числа).\n"
            "Доход на план не влияет: у каждой категории свой лимит, а остаток уходит в «резерв».\n\n"
            "💰 Доход этого месяца — от фактического прихода.\n"
            "💵 Бюджет месяца — фиксированная сумма из настроек.",
            reply_markup=plan_mode_keyboard(mode, budget),
        )
    elif data == "setbudget":
        user_id = update.effective_user.id
        current = await db.get_setting_float(user_id, SET_PLAN_BUDGET, DEFAULT_PLAN_BUDGET)
        await query.edit_message_text(
            f"Введи бюджет месяца в EUR (сейчас {eur(current)}).\n"
            f"Используется в режиме «💵 Бюджет месяца»:"
        )
        return SETBUDGET_AMOUNT
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
        buttons.append([InlineKeyboardButton("♻️ Остальное — в авто", callback_data="limit_auto_all")])
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
        await query.edit_message_text(
            "Выбери категорию для месячного плана.\n"
            "При вводе суммы: число — фиксированный план, 0 — не тратить, «авто» — делить свободный остаток.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
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


# ===================== ОСНОВА ПЛАНА И БЮДЖЕТ МЕСЯЦА =====================

async def setmode_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    mode = query.data.replace("mode_", "")
    if mode not in PLAN_MODE_LABELS:
        await query.edit_message_text("❌ Неизвестный режим.")
        return IDLE

    await db.set_setting(user_id, SET_PLAN_MODE, mode)
    plan = await load_plan(user_id)
    await query.edit_message_text(f"✅ План считается от: {PLAN_MODE_LABELS[mode]}\n\n{plan_text(plan)}")
    return IDLE


async def setbudget_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        value = float(update.message.text.strip().replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Нужно положительное число. Попробуй ещё раз:")
        return SETBUDGET_AMOUNT

    await db.set_setting(user_id, SET_PLAN_BUDGET, value)
    plan = await load_plan(user_id)
    await update.message.reply_text(f"✅ Бюджет месяца: {eur(value)}\n\n{plan_text(plan)}", reply_markup=MENU_KEYBOARD)
    return IDLE


async def setmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    mode = await db.get_setting(user_id, SET_PLAN_MODE, PLAN_MODE_AVAILABLE) or PLAN_MODE_AVAILABLE
    budget = await db.get_setting_float(user_id, SET_PLAN_BUDGET, DEFAULT_PLAN_BUDGET)
    await update.message.reply_text(
        f"Сейчас план считается от: {PLAN_MODE_LABELS.get(mode, mode)}.\nВыбери основу:",
        reply_markup=plan_mode_keyboard(mode, budget),
    )
    return IDLE


async def setbudget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = list(context.args or [])
    current = await db.get_setting_float(user_id, SET_PLAN_BUDGET, DEFAULT_PLAN_BUDGET)
    if not args:
        await update.message.reply_text(
            f"Введи бюджет месяца в EUR (сейчас {eur(current)}; используется в режиме «💵 Бюджет месяца»):"
        )
        return SETBUDGET_AMOUNT
    try:
        value = float(args[0].replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Формат: /setbudget 350")
        return IDLE
    await db.set_setting(user_id, SET_PLAN_BUDGET, value)
    plan = await load_plan(user_id)
    await update.message.reply_text(f"✅ Бюджет месяца: {eur(value)}\n\n{plan_text(plan)}", reply_markup=MENU_KEYBOARD)
    return IDLE


# ===================== МЕСЯЧНЫЕ ПЛАНЫ КАТЕГОРИЙ =====================

async def setlimit_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Отменено.")
        return IDLE

    if query.data == "limit_auto_all":
        cats = await db.get_categories(user_id)
        subs = await db.get_subscriptions(user_id)
        periodic_cats = {s["category_name"] for s in subs if s.get("category_name")}
        changed = 0
        for c in cats:
            if c["name"] in periodic_cats:
                continue
            await db.set_category_monthly_plan(user_id, c["name"], None)
            changed += 1
        plan = await load_plan(user_id)
        await query.edit_message_text(
            f"♻️ Категорий в режиме «авто»: {changed}.\n\n{plan_text(plan)}"
        )
        return IDLE

    cat_name = query.data.replace("limit_", "")
    context.user_data["limit_category"] = cat_name
    cat = await db.get_category_by_name(user_id, cat_name)
    current = cat.get("monthly_limit") if cat else None
    current_text = "авто" if current is None else eur(float(current))
    await query.edit_message_text(
        f"Месячный план для «{cat_name}» (сейчас: {current_text}).\n"
        f"Введи число, 0 (не тратить) или «авто»:"
    )
    return SETLIMIT_AMOUNT


async def setlimit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    raw = update.message.text.strip().lower()

    if raw in ("авто", "auto", "-", "а"):
        plan_value = None
    else:
        try:
            plan_value = float(raw.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Нужно число, 0 или «авто». Попробуй ещё раз:")
            return SETLIMIT_AMOUNT

    cat_name = context.user_data["limit_category"]
    await db.set_category_monthly_plan(user_id, cat_name, plan_value)
    context.user_data.clear()

    if plan_value is None:
        answer = f"♻️ «{cat_name}» — режим «авто» (делит свободный остаток)"
    elif plan_value == 0:
        answer = f"✅ «{cat_name}»: план 0 — тратить не планируем"
    else:
        answer = f"✅ «{cat_name}»: {eur(plan_value)} в месяц"

    plan = await load_plan(user_id)
    await update.message.reply_text(f"{answer}\n\n{plan_text(plan)}", reply_markup=MENU_KEYBOARD)
    return IDLE


# ===================== ПОРОГ И ПЛАНОВЫЙ ДОХОД =====================

async def setwarn_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        value = float(update.message.text.strip().replace(",", "."))
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Нужно неотрицательное число. Попробуй ещё раз:")
        return SETWARN_AMOUNT

    await db.set_setting(user_id, SET_WARN_THRESHOLD, value)
    await update.message.reply_text(
        f"✅ Предупреждать буду, когда в категории остаётся {eur(value)} и меньше.",
        reply_markup=MENU_KEYBOARD,
    )
    return IDLE


async def setplan_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        value = float(update.message.text.strip().replace(",", "."))
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Нужно неотрицательное число. Попробуй ещё раз:")
        return SETPLAN_AMOUNT

    await db.set_setting(user_id, SET_PLANNED_INCOME, value)
    plan = await load_plan(user_id)
    if value == 0:
        head = "✅ Плановый доход отключён — план считается от баланса."
    else:
        head = f"✅ Плановый доход: {eur(value)} в месяц."
    await update.message.reply_text(f"{head}\n\n{plan_text(plan)}", reply_markup=MENU_KEYBOARD)
    return IDLE


# ===================== ADD CATEGORY =====================

async def addcat_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.message.text.strip()
    await db.add_category(user_id, name)
    await update.message.reply_text(f"✅ Категория «{name}» добавлена.", reply_markup=MENU_KEYBOARD)
    return IDLE


# ===================== КОМАНДЫ =====================

HELP_TEXT = (
    "Команды:\n"
    "/income 350 зарплата 01.10 — быстро добавить доход\n"
    "/income — добавить доход пошагово\n"
    "/budget — план на месяц по категориям\n"
    "/days — расходы по каждому дню месяца\n"
    "/setmode — от чего считать план (деньги в наличии / доход / бюджет)\n"
    "/setbudget 350 — фиксированный бюджет месяца\n"
    "/setplan 350 — плановый доход месяца (режим «доход месяца»)\n"
    "/setwarn 7 — за сколько EUR предупреждать о конце категории\n"
    "/cancel — отменить текущее действие\n"
    "/help — эта справка"
)


async def income_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/income 350 [валюта] [описание] [дата] — или без аргументов, пошагово."""
    args = list(context.args or [])
    if not args:
        return await income_start(update, context)

    user_id = update.effective_user.id
    try:
        amount = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Формат: /income 350 зарплата 01.10")
        return IDLE

    currency = "EUR"
    rest = args[1:]
    if rest and rest[0].upper() in ("EUR", "USD", "UAH"):
        currency = rest[0].upper()
        rest = rest[1:]

    income_date = datetime.now().strftime("%Y-%m-%d")
    if rest and any(ch.isdigit() for ch in rest[-1]):
        parsed = parse_date_input(rest[-1])
        if parsed:
            income_date = parsed
            rest = rest[:-1]

    description = " ".join(rest) or None
    eur_amount = await to_eur(amount, currency)
    await db.add_income(user_id, eur_amount, currency, description, income_date)
    await db.add_to_balance(user_id, eur_amount)

    plan = await load_plan(user_id)
    await update.message.reply_text(
        f"✅ Доход {eur(eur_amount)} ({income_date}) добавлен.\n"
        f"💶 Баланс: {eur(plan['balance'])}\n\n{plan_text(plan)}",
        reply_markup=MENU_KEYBOARD,
    )
    return IDLE


async def budget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await show_budget(update, context)


async def setwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = list(context.args or [])
    current = await db.get_setting_float(user_id, SET_WARN_THRESHOLD, DEFAULT_WARN_THRESHOLD)
    if not args:
        await update.message.reply_text(
            f"Введи порог предупреждения в EUR (сейчас {eur(current)}):"
        )
        return SETWARN_AMOUNT
    try:
        value = float(args[0].replace(",", "."))
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Формат: /setwarn 7")
        return IDLE
    await db.set_setting(user_id, SET_WARN_THRESHOLD, value)
    await update.message.reply_text(
        f"✅ Предупреждать буду, когда в категории остаётся {eur(value)} и меньше.",
        reply_markup=MENU_KEYBOARD,
    )
    return IDLE


async def setplan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = list(context.args or [])
    current = await db.get_setting_float(user_id, SET_PLANNED_INCOME, 0.0)
    if not args:
        await update.message.reply_text(
            f"Введи плановый доход месяца в EUR (сейчас {eur(current)}; 0 — считать от баланса):"
        )
        return SETPLAN_AMOUNT
    try:
        value = float(args[0].replace(",", "."))
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Формат: /setplan 350")
        return IDLE
    await db.set_setting(user_id, SET_PLANNED_INCOME, value)
    plan = await load_plan(user_id)
    await update.message.reply_text(f"✅ Плановый доход: {eur(value)}\n\n{plan_text(plan)}", reply_markup=MENU_KEYBOARD)
    return IDLE


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=MENU_KEYBOARD)
    return IDLE


# ===================== MAIN =====================

async def _post_init(application: Application):
    """Создаёт/мигрирует таблицы БД при старте приложения."""
    await db.init()
    logger.info("Database ready: %s", db.db_path)


def build_application() -> Application:
    """Собирает Application со всеми хендлерами (без запуска polling — удобно тестировать)."""
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("income", income_cmd),
            CommandHandler("budget", budget_cmd),
            CommandHandler("days", show_days),
            CommandHandler("help", help_cmd),
            CommandHandler("setplan", setplan_cmd),
            CommandHandler("setwarn", setwarn_cmd),
            CommandHandler("setmode", setmode_cmd),
            CommandHandler("setbudget", setbudget_cmd),
            MessageHandler(filters.TEXT & ~filters.COMMAND, start),
        ],
        states={
            IDLE: [
                MessageHandler(filters.Regex("^💶 Баланс$"), show_balance),
                MessageHandler(filters.Regex("^🎯 Бюджет$"), show_budget),
                MessageHandler(filters.Regex("^➕ Добавить трату$"), add_start),
                MessageHandler(filters.Regex("^💰 Добавить доход$"), income_start),
                MessageHandler(filters.Regex("^📊 Статистика$"), stats_prompt),
                MessageHandler(filters.Regex("^📋 Подписки$"), show_subs),
                MessageHandler(filters.Regex("^✏️ Редактировать$"), edit_start),
                MessageHandler(filters.Regex("^🗑 Удалить$"), delete_start),
                MessageHandler(filters.Regex("^⚙️ Настройки$"), settings_prompt),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                CallbackQueryHandler(stats_cb, pattern="^stats_"),
                CallbackQueryHandler(settings_cb, pattern="^(settings_|setbalance$|addcat$|setlimit$|setplan$|setwarn$|setmode$|setbudget$|rates$|export$|clearchat$)"),
                CallbackQueryHandler(setmode_choose, pattern="^mode_"),
                CallbackQueryHandler(sub_add_start, pattern="^sub_add$"),
                CallbackQueryHandler(sub_edit_start, pattern="^sub_edit$"),
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
                CommandHandler("skip", add_desc),
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
            DEL_KIND: [CallbackQueryHandler(delete_kind, pattern="^(delkind_|cancel)")],
            DELINC_SELECT: [
                CallbackQueryHandler(deleteinc_select, pattern="^(delinc_select_|cancel)"),
                CallbackQueryHandler(deleteinc_confirm, pattern="^delinc_confirm_"),
            ],
            ADDSUB_NAME: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sub_add_name),
            ],
            ADDSUB_AMOUNT: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sub_add_amount),
            ],
            ADDSUB_MONTHS: [
                CallbackQueryHandler(sub_add_months, pattern="^period_"),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sub_add_months_custom),
            ],
            ADDSUB_CATEGORY: [CallbackQueryHandler(sub_add_category, pattern="^(subcat_|cancel)")],
            SUB_EDIT_SELECT: [CallbackQueryHandler(sub_edit_select, pattern="^(subedit_|cancel)")],
            SUB_EDIT_FIELD: [CallbackQueryHandler(sub_edit_field, pattern="^subfield_")],
            SUB_EDIT_AMOUNT: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sub_edit_amount),
            ],
            SUB_EDIT_NAME: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sub_edit_name),
            ],
            SUB_EDIT_MONTHS: [
                CallbackQueryHandler(sub_edit_months, pattern="^ep_"),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sub_edit_months_custom),
            ],
            SUB_EDIT_CATEGORY: [CallbackQueryHandler(sub_edit_category, pattern="^(subcat_|cancel)")],
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
            INCOME_AMOUNT: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, income_amount),
            ],
            INCOME_DESC: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                CommandHandler("skip", income_desc),
                MessageHandler(filters.TEXT & ~filters.COMMAND, income_desc),
            ],
            INCOME_DATE: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, income_date),
            ],
            SETWARN_AMOUNT: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setwarn_amount),
            ],
            SETPLAN_AMOUNT: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setplan_amount),
            ],
            SETBUDGET_AMOUNT: [
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setbudget_amount),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CommandHandler("income", income_cmd),
            CommandHandler("budget", budget_cmd),
            CommandHandler("days", show_days),
            CommandHandler("setplan", setplan_cmd),
            CommandHandler("setwarn", setwarn_cmd),
            CommandHandler("setmode", setmode_cmd),
            CommandHandler("setbudget", setbudget_cmd),
            CommandHandler("help", help_cmd),
            CallbackQueryHandler(cancel, pattern="^cancel$"),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel),
        ],
    )

    app = Application.builder().token(TOKEN).post_init(_post_init).build()
    app.add_handler(conv)

    # Middleware для логирования всех сообщений (для очистки чата)
    app.add_handler(MessageHandler(filters.ALL, log_message), group=1)
    return app


def main():
    app = build_application()
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
