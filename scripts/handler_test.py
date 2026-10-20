"""Прогон реальных хендлеров бота без Telegram: подменяем сетевые вызовы Bot.

Проверяет связку ConversationHandler → состояния → БД → ответы:
  /start, добавление дохода, /budget, добавление траты с предупреждением,
  добавление периодического платежа, /setwarn, /help.

Запуск: ./venv/bin/python scripts/handler_test.py
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

TMPDIR = tempfile.mkdtemp(prefix="expensebot-handler-")
os.environ["DB_PATH"] = os.path.join(TMPDIR, "test.db")
os.environ.setdefault("BOT_TOKEN", "test-token-not-used")

import bot as botmod  # noqa: E402
from telegram import Bot, Update  # noqa: E402

USER = 1084411025

sent: list[str] = []
markups: list = []
_uid = [1000]


def _next_id() -> int:
    _uid[0] += 1
    return _uid[0]


class FakeMessage:
    def __init__(self, mid: int):
        self.message_id = mid


async def fake_send_message(self, chat_id=None, text=None, **kwargs):
    sent.append(str(text))
    markups.append(kwargs.get("reply_markup"))
    return FakeMessage(_next_id())


async def fake_edit_message_text(self, *args, **kwargs):
    text = kwargs.get("text", args[0] if args else "")
    sent.append(str(text))
    markups.append(kwargs.get("reply_markup"))
    return FakeMessage(_next_id())


async def fake_ok(self, *args, **kwargs):
    return True


Bot.send_message = fake_send_message
Bot.edit_message_text = fake_edit_message_text
Bot.answer_callback_query = fake_ok
Bot.delete_message = fake_ok
Bot.send_document = fake_ok

FAILS: list[str] = []
CHECKS = [0]


def check(name: str, ok: bool, detail: str = ""):
    CHECKS[0] += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(f"{name}: {detail}")


def last() -> str:
    return sent[-1] if sent else ""


def item_plan(plan: dict, name: str) -> float:
    """План конкретной категории из результата load_plan."""
    return next((it["plan"] for it in plan["items"] if it["name"] == name), 0.0)


def last_markup_text() -> str:
    """Все подписи кнопок последней клавиатуры — для проверки reply_markup."""
    markup = markups[-1] if markups else None
    if markup is None:
        return ""
    labels = []

    def collect(obj):
        if isinstance(obj, (list, tuple)):
            for item in obj:
                collect(item)
        else:
            for attr in ("text", "button"):
                value = getattr(obj, attr, None)
                if isinstance(value, str):
                    labels.append(value)
                elif value is not None:
                    collect(value)
            nested = getattr(obj, "keyboard", None)
            if nested is not None:
                collect(nested)
            inline = getattr(obj, "inline_keyboard", None)
            if inline is not None:
                collect(inline)

    collect(markup)
    return " | ".join(labels)


def msg(bot, text: str) -> Update:
    entities = []
    if text.startswith("/"):
        command = text.split()[0]
        entities.append({"type": "bot_command", "offset": 0, "length": len(command)})
    return Update.de_json({
        "update_id": _next_id(),
        "message": {
            "message_id": _next_id(),
            "date": int(time.time()),
            "chat": {"id": USER, "type": "private"},
            "from": {"id": USER, "is_bot": False, "first_name": "Vadim"},
            "text": text,
            "entities": entities,
        },
    }, bot)


def callback(bot, data: str) -> Update:
    return Update.de_json({
        "update_id": _next_id(),
        "callback_query": {
            "id": str(_next_id()),
            "from": {"id": USER, "is_bot": False, "first_name": "Vadim"},
            "chat_instance": "1",
            "data": data,
            "message": {
                "message_id": _next_id(),
                "date": int(time.time()),
                "chat": {"id": USER, "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "bot"},
                "text": "prev",
            },
        },
    }, bot)


async def seed():
    """Настройки и платежи, как у пользователя."""
    db = botmod.db
    await db.init()
    for name in botmod.DEFAULT_CATEGORIES:
        await db.add_category(USER, name)
    await db.set_setting(USER, botmod.SET_PLANNED_INCOME, 350)
    await db.set_setting(USER, botmod.SET_WARN_THRESHOLD, 7)

    plans = {"еда": 100.0, "бытовое": 20.0, "прочее": 0.0, "транспорт": 0.0, "развлечения": 0.0,
             "резерв": None}
    for name, value in plans.items():
        await db.set_category_monthly_plan(USER, name, value)
    await db.set_setting(USER, botmod.SET_PLAN_BUDGET, 350)

    periodic = [
        ("4ка", 16.0, "monthly", 1.0, "подписки"),
        ("vodafon", 10.0, "monthly", 1.0, "подписки"),
        ("Зал", 39.0, "monthly", 1.0, "подписки"),
        ("Hermes", 23.0, "monthly", 1.0, "подписки"),
        ("Fly.io", 25.0, "yearly", 12.0, "подписки"),
        ("Общежитие", 180.0, "2months", 2.0, "общежитие"),
        ("Стрижка", 20.0, "custom", 1.5, "стрижка"),
    ]
    for name, amount, period, months, cat in periodic:
        cat_row = await db.get_category_by_name(USER, cat)
        await db.add_subscription(USER, name, amount, period, None, months=months, category_id=cat_row["id"])


async def scenario(app):
    bot = app.bot
    today = datetime.now()

    # 1. /start
    await app.process_update(msg(bot, "/start"))
    check("/start отвечает приветствием", "Привет" in last() and "Бюджет" in last())

    # 2. Добавление дохода через меню
    await app.process_update(msg(bot, "💰 Добавить доход"))
    check("доход: шаг 1 (сумма)", "Шаг 1/3" in last())
    await app.process_update(msg(bot, "350"))
    check("доход: шаг 2 (описание)", "Шаг 2/3" in last())
    await app.process_update(msg(bot, "стипендия"))
    check("доход: шаг 3 (дата)", "Шаг 3/3" in last())
    await app.process_update(msg(bot, "-"))
    check("доход добавлен", "Доход добавлен" in last(), last().replace("\n", " | ")[:120])
    check("баланс стал 350", "350.00€" in last())
    check("в ответе показан план месяца", "План месяца" in last())

    # 3. /budget
    await app.process_update(msg(bot, "/budget"))
    b = last()
    check("/budget: план от денег в наличии", "деньги в наличии" in b, b.replace("\n", " | ")[:160])
    check("/budget: периодика 193.42", "193.42€" in b)
    check("/budget: есть еда/общежитие/стрижка", all(x in b for x in ("еда", "общежитие", "стрижка")))
    check("/budget: еда ровно 100", "еда: 0.00€ / 100.00€" in b, b.replace("\n", " | ")[:300])
    check("/budget: остаток уходит в резерв", "Остаток уходит в «резерв»" in b)

    # 4. Трата с предупреждением: добиваем еду до остатка 5€
    db = botmod.db
    food = await db.get_category_by_name(USER, "еда")
    await db.add_expense(USER, 90.0, "EUR", food["id"], "закупка", today.strftime("%Y-%m-%d"))

    await app.process_update(msg(bot, "➕ Добавить трату"))
    check("трата: шаг 1 (сумма)", "Шаг 1/4" in last())
    await app.process_update(msg(bot, "5"))
    check("трата: шаг 2 (категория)", "Выбери категорию" in last())
    await app.process_update(callback(bot, f"cat_{food['id']}"))
    check("трата: шаг 3 (дата)", "Шаг 3/4" in last())
    await app.process_update(callback(bot, f"year_{today.year}"))
    await app.process_update(callback(bot, f"month_{today.year}_{today.month}"))
    day_buttons = last_markup_text()
    check("календарь: сегодняшний день отмечен и не заблокирован",
          f"•{today.day}•" in day_buttons and f"🔒{today.day}" not in day_buttons,
          f"метка '•{today.day}•' есть: {f'•{today.day}•' in day_buttons}, "
          f"замок на сегодня: {f'🔒{today.day}' in day_buttons}")
    await app.process_update(callback(bot, f"day_{today.year}_{today.month}_{today.day}"))
    check("трата: шаг 4 (описание)", "Шаг 4/4" in last())
    await app.process_update(msg(bot, "Теско"))
    reply = last()
    check("трата добавлена", "Трата добавлена" in reply, reply.replace("\n", " | ")[:120])
    check("предупреждение ⚠️ о конце категории", "⚠️" in reply and "еда" in reply,
          reply.replace("\n", " | ")[:200])

    # 5. Перерасход: ещё 10€ в еду
    await app.process_update(msg(bot, "➕ Добавить трату"))
    await app.process_update(msg(bot, "10"))
    await app.process_update(callback(bot, f"cat_{food['id']}"))
    await app.process_update(callback(bot, f"year_{today.year}"))
    await app.process_update(callback(bot, f"month_{today.year}_{today.month}"))
    await app.process_update(callback(bot, f"day_{today.year}_{today.month}_{today.day}"))
    await app.process_update(msg(bot, "/skip"))
    check("перерасход помечен 🔴", "🔴" in last() and "перерасход" in last(), last().replace("\n", " | ")[:200])

    # 6. Добавление периодического платежа через меню
    await app.process_update(msg(bot, "📋 Подписки"))
    check("подписки: список с периодикой", "В месяц: 193.42€" in last(), last().replace("\n", " | ")[:200])
    await app.process_update(callback(bot, "sub_add"))
    check("подписка: спросили название", "название платежа" in last())
    await app.process_update(msg(bot, "Fly.io Extra"))
    await app.process_update(msg(bot, "25"))
    check("подписка: спросили период", "период платежа" in last())
    await app.process_update(callback(bot, "period_yearly"))
    check("подписка: предложены категории", "категории" in last(), last().replace("\n", " | ")[:120])
    sub_cat = await db.get_category_by_name(USER, "подписки")
    await app.process_update(callback(bot, f"subcat_{sub_cat['id']}"))
    check("подписка добавлена (годовая → 2.08/мес)", "добавлен" in last() and "2.08€/мес" in last(),
          last().replace("\n", " | ")[:200])
    check("периодика выросла до 195.50", "195.50€" in last(), last().replace("\n", " | ")[:200])

    # 7. /setwarn
    await app.process_update(msg(bot, "/setwarn 5"))
    check("/setwarn 5 сохранён", "5.00€" in last(), last().replace("\n", " | ")[:120])
    await app.process_update(msg(bot, "/setwarn"))
    check("/setwarn без аргумента просит число", "порог предупреждения" in last())
    await app.process_update(msg(bot, "7"))
    check("/setwarn 7 в диалоге сохранён", "7.00€" in last())

    # 8. Месячный план категории через настройки
    await app.process_update(msg(bot, "⚙️ Настройки"))
    buttons = last_markup_text()
    check("настройки: кнопки плана, дохода и порога",
          all(x in buttons for x in ("Месячные планы", "Плановый доход", "Порог предупреждения")),
          buttons)
    await app.process_update(callback(bot, "setlimit"))
    check("выбор категории для плана", "месячного плана" in last())
    await app.process_update(callback(bot, "limit_еда"))
    check("запрос суммы плана", "Месячный план для «еда»" in last())
    await app.process_update(msg(bot, "150"))
    check("план еды стал 150 и виден в плане", "150.00€" in last() and "План на месяц" in last(),
          last().replace("\n", " | ")[:200])

    # 9. /help
    await app.process_update(msg(bot, "/help"))
    check("/help показывает команды", "/income" in last() and "/budget" in last())

    # 10. Команда /income с аргументами
    base_before_income = (await botmod.load_plan(USER))["base"]
    await app.process_update(msg(bot, "/income 100 подработка"))
    check("/income 100 добавлен", "Доход 100.00€" in last(), last().replace("\n", " | ")[:150])
    base_after_income = (await botmod.load_plan(USER))["base"]
    check("доход поднял базу плана ровно на 100",
          abs(base_after_income - base_before_income - 100.0) < 1e-9,
          f"{base_before_income:.2f} → {base_after_income:.2f}")


    # 11. Удаление / изменение / добавление платежа пересчитывает план
    print("\n--- правки платежей ---")
    db = botmod.db
    subs = {s["name"]: s for s in await db.get_subscriptions(USER)}

    pl0 = await botmod.load_plan(USER)
    print(f"было: периодика {pl0['periodic_total']:.2f}€, план категорий {pl0['planned_total']:.2f}€, "
          f"в резерв {item_plan(pl0, 'резерв'):.2f}€")

    # 11a. Удаление «Зал» (39€/мес)
    await app.process_update(callback(bot, "sub_del"))
    await app.process_update(callback(bot, f"subdel_{subs['Зал']['id']}"))
    pl1 = await botmod.load_plan(USER)
    print(f"после удаления «Зал»: периодика {pl1['periodic_total']:.2f}€, "
          f"в резерв {item_plan(pl1, 'резерв'):.2f}€")
    check("удаление платежа: отчёт с новым планом", "удалён" in last() and "План месяца" in last(),
          last().replace("\n", " | ")[:180])
    check("удаление «Зал»: периодика −39€",
          abs(pl1["periodic_total"] - (pl0["periodic_total"] - 39.0)) < 1e-9,
          f"{pl1['periodic_total']:.2f}")
    check("удаление «Зал»: в резерв ушло на 39€ больше",
          abs(item_plan(pl1, "резерв") - (item_plan(pl0, "резерв") + 39.0)) < 1e-6,
          f"{item_plan(pl0, 'резерв'):.2f} → {item_plan(pl1, 'резерв'):.2f}")
    food_before = next(it for it in pl0["items"] if it["name"] == "еда")
    food_after = next(it for it in pl1["items"] if it["name"] == "еда")
    check("фикс-план «еда» при этом не поехал", abs(food_before["plan"] - food_after["plan"]) < 1e-9,
          f"{food_before['plan']:.2f} → {food_after['plan']:.2f}")

    # 11b. Правка суммы: vodafon 10 → 15
    vod = subs["vodafon"]["id"]
    await app.process_update(callback(bot, "sub_edit"))
    check("редактирование: список платежей", "Что меняем?" in last())
    await app.process_update(callback(bot, f"subedit_{vod}"))
    check("редактирование: предложены поля платежа",
          "Что меняем?" in last() and all(x in last_markup_text() for x in ("Сумма", "Период", "Категория", "Название")),
          last_markup_text())
    await app.process_update(callback(bot, f"subfield_amount_{vod}"))
    await app.process_update(msg(bot, "15"))
    pl2 = await botmod.load_plan(USER)
    print(f"после vodafon 10→15: периодика {pl2['periodic_total']:.2f}€, "
          f"в резерв {item_plan(pl2, 'резерв'):.2f}€")
    check("правка суммы: периодика +5€",
          abs(pl2["periodic_total"] - (pl1["periodic_total"] + 5.0)) < 1e-9, f"{pl2['periodic_total']:.2f}")
    check("правка суммы: отчёт показывает «Сумма изменена» и категорию",
          "Сумма изменена" in last() and "«подписки»" in last(), last().replace("\n", " | ")[:200])

    # 11c. Правка периода: стрижка 20€ каждые 1.5 мес → каждые 2 мес (13.33 → 10 €/мес)
    hair = subs["Стрижка"]["id"]
    await app.process_update(callback(bot, "sub_edit"))
    await app.process_update(callback(bot, f"subedit_{hair}"))
    await app.process_update(callback(bot, f"subfield_months_{hair}"))
    await app.process_update(callback(bot, "ep_2months"))
    pl3 = await botmod.load_plan(USER)
    print(f"после стрижки 1.5→2 мес: периодика {pl3['periodic_total']:.2f}€")
    check("правка периода: 20€ каждые 2 мес = 10€/мес",
          "10.00€/мес" in last() and "каждые 2 мес." in last(), last().replace("\n", " | ")[:160])
    check("правка периода: периодика −3.33€",
          abs(pl3["periodic_total"] - (pl2["periodic_total"] - 3.3333)) < 0.001,
          f"{pl3['periodic_total']:.2f}")

    # 11d. Правка категории: переносим Hermes из «подписки» в «бытовое»
    hermes = subs["Hermes"]["id"]
    await app.process_update(callback(bot, "sub_edit"))
    await app.process_update(callback(bot, f"subedit_{hermes}"))
    await app.process_update(callback(bot, f"subfield_category_{hermes}"))
    bytovoe = await db.get_category_by_name(USER, "бытовое")
    await app.process_update(callback(bot, f"subcat_{bytovoe['id']}"))
    pl4 = await botmod.load_plan(USER)
    subs_plan = next(it for it in pl4["items"] if it["name"] == "подписки")
    bytovoe_item = next(it for it in pl4["items"] if it["name"] == "бытовое")
    print(f"после переноса Hermes: «подписки» {subs_plan['plan']:.2f}€, «бытовое» {bytovoe_item['plan']:.2f}€")
    check("правка категории: отчёт про смену категории", "Категория изменена" in last(),
          last().replace("\n", " | ")[:160])
    check("Hermes ушёл из «подписки» в «бытовое»",
          abs(bytovoe_item["periodic"] - 23.0) < 1e-9
          and abs(subs_plan["periodic"] - (16.0 + 15.0 + 2.0833 * 2)) < 0.01,
          f"бытовое periodic={bytovoe_item['periodic']:.2f}, подписки periodic={subs_plan['periodic']:.2f}")

    # 11e. Добавление нового платежа
    await app.process_update(msg(bot, "📋 Подписки"))
    await app.process_update(callback(bot, "sub_add"))
    await app.process_update(msg(bot, "Netflix"))
    await app.process_update(msg(bot, "15"))
    await app.process_update(callback(bot, "period_monthly"))
    sub_cat = await db.get_category_by_name(USER, "подписки")
    await app.process_update(callback(bot, f"subcat_{sub_cat['id']}"))
    pl5 = await botmod.load_plan(USER)
    print(f"после добавления Netflix 15€/мес: периодика {pl5['periodic_total']:.2f}€, "
          f"в резерв {item_plan(pl5, 'резерв'):.2f}€")
    check("добавление: периодика +15€",
          abs(pl5["periodic_total"] - (pl4["periodic_total"] + 15.0)) < 1e-9, f"{pl5['periodic_total']:.2f}")

    # 11f. «Авто»-категории делят остаток: прочее + резерв
    await app.process_update(msg(bot, "⚙️ Настройки"))
    await app.process_update(callback(bot, "setlimit"))
    await app.process_update(callback(bot, "limit_прочее"))
    await app.process_update(msg(bot, "авто"))
    pl6 = await botmod.load_plan(USER)
    free_for_auto = pl6["base"] - pl6["periodic_total"] - pl6["fixed_total"]
    autos = [it for it in pl6["items"] if it["auto"]]
    print(f"после перевода «прочее» в авто: авто={[(it['name'], round(it['plan'], 2)) for it in autos]}, "
          f"свободно {free_for_auto:.2f}€")
    check("две «авто»-категории делят остаток поровну",
          len(autos) == 2 and abs(sum(it["plan"] for it in autos) - free_for_auto) < 0.01
          and abs(autos[0]["plan"] - autos[1]["plan"]) < 0.01 and abs(pl6["unallocated"]) < 0.01,
          f"авто={[(it['name'], round(it['plan'], 2)) for it in autos]}, свободно={free_for_auto:.2f}")

    # 11g. Режим «доход месяца» и нехватка денег
    for income in await db.get_incomes(USER):
        await db.delete_income(USER, income["id"])
    await db.set_balance(USER, 0.0)
    await app.process_update(msg(bot, "/setplan 250"))
    await app.process_update(callback(bot, "mode_income"))
    pl7 = await botmod.load_plan(USER)
    print(f"режим «доход месяца», плановый доход 250€: база {pl7['base']:.2f}€ ({pl7['base_source']}), "
          f"план {pl7['planned_total']:.2f}€, не хватает {-pl7['unallocated']:.2f}€")
    check("переключение на «доход месяца» применено",
          pl7["mode"] == "income" and "доход этого месяца" in last(), last().replace("\n", " | ")[:160])
    check("база = плановый доход, когда доходов нет",
          pl7["base"] == 250.0 and pl7["base_source"] == "плановый доход",
          f"base={pl7['base']}, источник={pl7['base_source']}")
    check("план не сходится, когда денег мало",
          "План не сходится" in last() and pl7["unallocated"] < 0, last().replace("\n", " | ")[:200])

    # 12. План не зависит от дохода: еда всегда 100, лишнее — в резерв
    await app.process_update(callback(bot, "mode_available"))
    # в п.8 еду меняли на 150 — возвращаем 100 через настройки
    await app.process_update(msg(bot, "⚙️ Настройки"))
    await app.process_update(callback(bot, "setlimit"))
    await app.process_update(callback(bot, "limit_еда"))
    await app.process_update(msg(bot, "100"))
    check("еда возвращена к 100€",
          item_plan(await botmod.load_plan(USER), "еда") == 100.0,
          last().replace("\n", " | ")[:150])

    pl_base_before = await botmod.load_plan(USER)
    base_before = pl_base_before["base"]

    await app.process_update(msg(bot, "➕ Добавить трату"))
    await app.process_update(msg(bot, "10"))
    await app.process_update(callback(bot, f"cat_{food['id']}"))
    await app.process_update(callback(bot, f"year_{today.year}"))
    await app.process_update(callback(bot, f"month_{today.year}_{today.month}"))
    await app.process_update(callback(bot, f"day_{today.year}_{today.month}_{today.day}"))
    await app.process_update(msg(bot, "/skip"))
    pl_after_spend = await botmod.load_plan(USER)
    check("трата не уменьшает базу плана (деньги в наличии)",
          abs(pl_after_spend["base"] - base_before) < 1e-9,
          f"{base_before:.2f} → {pl_after_spend['base']:.2f}")

    await db.set_balance(USER, 1200.0)
    pl_rich = await botmod.load_plan(USER)
    print(f"баланс 1200€: база {pl_rich['base']:.2f}€, еда {item_plan(pl_rich, 'еда'):.2f}€, "
          f"резерв {item_plan(pl_rich, 'резерв'):.2f}€")
    check("баланс 1200€: еда всё ещё 100€",
          item_plan(pl_rich, "еда") == 100.0, f"{item_plan(pl_rich, 'еда'):.2f}")
    autos_rich = [it for it in pl_rich["items"] if it["auto"]]
    check("баланс 1200€: лишнее ушло в авто-категории, а не в лимиты",
          abs(sum(it["plan"] for it in autos_rich)
              - (pl_rich["base"] - pl_rich["periodic_total"] - pl_rich["fixed_total"])) < 0.01
          and sum(it["plan"] for it in autos_rich) > 1000.0 and abs(pl_rich["unallocated"]) < 0.01,
          f"авто={[(it['name'], round(it['plan'], 2)) for it in autos_rich]}")

    # 13. Режим «бюджет месяца»
    await app.process_update(msg(bot, "/setbudget 400"))
    await app.process_update(callback(bot, "mode_budget"))
    pl_budget = await botmod.load_plan(USER)
    print(f"режим «бюджет месяца» 400€: база {pl_budget['base']:.2f}€, еда {item_plan(pl_budget, 'еда'):.2f}€, "
          f"резерв {item_plan(pl_budget, 'резерв'):.2f}€")
    check("/setbudget 400 сохранён", "400.00€" in last(), last().replace("\n", " | ")[:120])
    check("режим «бюджет месяца»: база = 400 независимо от баланса 1200€",
          pl_budget["base"] == 400.0 and pl_budget["mode"] == "budget",
          f"base={pl_budget['base']}, mode={pl_budget['mode']}")
    check("бюджет 400: еда всё ещё 100€", item_plan(pl_budget, "еда") == 100.0)
    check("бюджет 400: план сходится через резерв", abs(pl_budget["unallocated"]) < 0.01,
          f"резерв={item_plan(pl_budget, 'резерв'):.2f}")

    # 14. Отчёт по дням: сегодняшняя строка всегда актуальна
    await app.process_update(msg(bot, "/days"))
    days_report = last()
    today_str = today.strftime("%Y-%m-%d")
    daily = await db.get_daily_spending(USER, today_str, today_str)
    today_total = float(daily[0]["spent"]) if daily else 0.0
    today_lines = [ln for ln in days_report.split("\n") if "← сегодня" in ln]
    print(f"отчёт по дням: {len(days_report.splitlines())} строк, "
          f"сегодня ({today.strftime('%d.%m')}) = {today_total:.2f}€")
    check("/days: строка сегодня одна и помечена", len(today_lines) == 1, days_report.replace("\n", " | ")[:200])
    check("/days: в сегодняшней строке сумма дня",
          f"{today_total:.2f}€" in today_lines[0] and today_total > 0, today_lines[0] if today_lines else "")
    check("/days: есть строка на каждый день месяца до сегодня",
          len([ln for ln in days_report.split("\n") if ln[:2].isdigit() and "." in ln[:3]]) == today.day,
          f"{len([ln for ln in days_report.splitlines() if ln[:2].isdigit()])} из {today.day}")
    check("/days: итог за месяц совпадает с планом",
          f"потрачено {pl_budget['spent_total']:.2f}€" in days_report, days_report.replace("\n", " | ")[-200:])

    await app.process_update(msg(bot, "/budget"))
    check("/budget показывает траты за сегодня", "Сегодня потрачено" in last(),
          last().replace("\n", " | ")[:200])

    # 15. Отчёт по дням доступен и из Статистики
    await app.process_update(msg(bot, "📊 Статистика"))
    check("в статистике есть кнопка «По дням месяца»", "По дням месяца" in last_markup_text(),
          last_markup_text())
    await app.process_update(callback(bot, "stats_days"))
    check("кнопка «По дням месяца» открывает отчёт", "по дням —" in last() and "← сегодня" in last(),
          last().replace("\n", " | ")[:160])

    # 16. Удаление дохода через меню
    print("\n--- удаление дохода ---")
    balance_before = await db.get_balance(USER)
    await app.process_update(msg(bot, "/income 77 подработка"))
    check("доход 77 добавлен перед удалением", "Доход 77.00€" in last(), last().replace("\n", " | ")[:120])
    check("баланс вырос на 77", abs(await db.get_balance(USER) - balance_before - 77.0) < 1e-9,
          f"{balance_before:.2f} → {await db.get_balance(USER):.2f}")

    await app.process_update(msg(bot, "🗑 Удалить"))
    check("удаление: спросили, что удалять", "Что удалить?" in last(), last().replace("\n", " | ")[:120])
    check("удаление: есть кнопки траты и дохода",
          all(x in last_markup_text() for x in ("Трату", "Доход")), last_markup_text())

    await app.process_update(callback(bot, "delkind_income"))
    check("удаление дохода: показан список доходов", "Выбери доход для удаления" in last(),
          last().replace("\n", " | ")[:160])

    incs = await db.get_incomes(USER, limit=20)
    target = next(i for i in incs if i["description"] == "подработка")
    await app.process_update(callback(bot, f"delinc_select_{target['id']}"))
    check("удаление дохода: подтверждение с суммой", "Удалить доход?" in last() and "77.00€" in last(),
          last().replace("\n", " | ")[:160])

    await app.process_update(callback(bot, "delinc_confirm_yes"))
    check("доход удалён", "Доход удалён" in last(), last().replace("\n", " | ")[:120])
    check("запись исчезла из БД", await db.get_income_by_id(USER, target["id"]) is None)
    check("баланс вернулся к прежнему", abs(await db.get_balance(USER) - balance_before) < 1e-9,
          f"{await db.get_balance(USER):.2f} vs {balance_before:.2f}")

    # 16a. Кнопка «Трату» в выборе ведёт в прежний календарь удаления трат
    await app.process_update(msg(bot, "🗑 Удалить"))
    await app.process_update(callback(bot, "delkind_expense"))
    check("удаление траты: остался прежний календарь", "Выбери дату" in last(),
          last().replace("\n", " | ")[:120])


async def main():
    print(f"tmp: {TMPDIR}")
    await seed()
    app = botmod.build_application()
    app._initialized = True  # обходим get_me() — сети в тесте нет
    from telegram import User
    app.bot._bot_user = User(id=1, is_bot=True, first_name="TestBot", username="test_bot")
    await scenario(app)
    print(f"\nпроверок: {CHECKS[0]}, провалов: {len(FAILS)}")
    for f in FAILS:
        print("  FAILED:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
