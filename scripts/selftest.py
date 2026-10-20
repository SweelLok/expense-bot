"""Локальные тесты бюджета и БД — без телеграма.

Запуск: ./venv/bin/python scripts/selftest.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budget  # noqa: E402
from budget import build_plan, month_bounds, monthly_amount, period_label, warning_lines  # noqa: E402
from db import Database, SET_PLANNED_INCOME, SET_WARN_THRESHOLD  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name: str, ok: bool, detail: str = ""):
    CHECKS.append(CHECKS.pop() + 1)
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(f"{name}: {detail}")


OLD_SCHEMA = """
CREATE TABLE balances (user_id INTEGER PRIMARY KEY, amount REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'EUR');
CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                         weekly_limit REAL, UNIQUE(user_id, name));
CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                            amount REAL NOT NULL, period TEXT NOT NULL, next_charge_date TEXT,
                            created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL,
                       currency TEXT NOT NULL DEFAULT 'EUR', category_id INTEGER NOT NULL, description TEXT,
                       expense_date TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE message_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                          message_id INTEGER NOT NULL, is_bot INTEGER NOT NULL DEFAULT 0,
                          created_at TEXT NOT NULL DEFAULT (datetime('now')));
"""

USER = 1084411025


def subs_fixture():
    return [
        {"name": "4ка", "amount": 16.0, "months": 1.0, "period": "monthly", "category_name": "подписки"},
        {"name": "vodafon", "amount": 10.0, "months": 1.0, "period": "monthly", "category_name": "подписки"},
        {"name": "Зал", "amount": 39.0, "months": 1.0, "period": "monthly", "category_name": "подписки"},
        {"name": "Hermes", "amount": 23.0, "months": 1.0, "period": "monthly", "category_name": "подписки"},
        {"name": "Fly.io", "amount": 25.0, "months": 12.0, "period": "yearly", "category_name": "подписки"},
        {"name": "Общежитие", "amount": 180.0, "months": 2.0, "period": "2months", "category_name": "общежитие"},
        {"name": "Стрижка", "amount": 20.0, "months": 1.5, "period": "custom", "category_name": "стрижка"},
    ]


def cats_fixture():
    plans = {"еда": 100.0, "бытовое": 20.0, "прочее": 0.0, "транспорт": 0.0, "развлечения": 0.0,
             "резерв": None}   # «резерв» — авто: забирает остаток
    out = []
    for name in ["еда", "бытовое", "подписки", "общежитие", "стрижка", "транспорт", "развлечения",
                 "прочее", "резерв"]:
        out.append({"name": name, "monthly_limit": plans.get(name)})
    return out


def reserve_plan(plan: dict) -> float:
    return next((it["plan"] for it in plan["items"] if it["name"] == "резерв"), 0.0)


def test_pure_math():
    subs = subs_fixture()
    check("monthly_amount: 25€/год = 2.083€/мес", abs(monthly_amount(subs[4]) - 2.0833) < 0.001,
          f"{monthly_amount(subs[4]):.4f}")
    check("monthly_amount: 180€ раз в 2 месяца = 90€/мес", abs(monthly_amount(subs[5]) - 90.0) < 1e-9)
    check("monthly_amount: 20€ раз в 1.5 месяца = 13.33€/мес", abs(monthly_amount(subs[6]) - 13.3333) < 0.001,
          f"{monthly_amount(subs[6]):.4f}")
    check("period_label(1.5) = 'каждые 1.5 мес.'", period_label(1.5) == "каждые 1.5 мес.", period_label(1.5))
    check("period_label(12) = 'в год'", period_label(12) == "в год")

    plan = build_plan(350.0, cats_fixture(), subs, {})
    check("периодика 193.42€/мес", abs(plan["periodic_total"] - 193.4167) < 0.01,
          f"{plan['periodic_total']:.4f}")
    check("фиксы (еда 100 + бытовое 20) = 120€", abs(plan["fixed_total"] - 120.0) < 1e-9,
          f"{plan['fixed_total']:.4f}")
    check("план сходится с доходом 350 (остаток в резерв)", abs(plan["unallocated"]) < 0.01,
          f"{plan['unallocated']:.4f}")
    check("«резерв» — авто и равен остатку 36.58€", abs(reserve_plan(plan) - 36.5833) < 0.01,
          f"{reserve_plan(plan):.4f}")

    by_name = {it["name"]: it for it in plan["items"]}
    check("«подписки» план = 90.08 (5 платежей)", abs(by_name["подписки"]["plan"] - 90.0833) < 0.01,
          f"{by_name['подписки']['plan']:.4f}")
    check("«общежитие» план = 90", abs(by_name["общежитие"]["plan"] - 90.0) < 1e-9)
    check("«стрижка» план = 13.33", abs(by_name["стрижка"]["plan"] - 13.3333) < 0.001)
    check("«еда» план = 100 (фикс)", by_name["еда"]["plan"] == 100.0 and by_name["еда"]["source"] == "fixed")

    # лишние деньги не поднимают лимиты — всё уходит в резерв
    plan_rich = build_plan(1200.0, cats_fixture(), subs, {})
    rich = {it["name"]: it for it in plan_rich["items"]}
    check("баланс 1200€: еда всё ещё 100€", rich["еда"]["plan"] == 100.0, f"{rich['еда']['plan']:.2f}")
    check("баланс 1200€: остаток 886.58€ в резерве", abs(reserve_plan(plan_rich) - 886.5833) < 0.01,
          f"{reserve_plan(plan_rich):.2f}")


def test_income_and_spending():
    subs = subs_fixture()
    cats = cats_fixture()

    spent = {"еда": 94.0}
    plan = build_plan(350.0, cats, subs, spent, warn_threshold=7.0)
    by_name = {it["name"]: it for it in plan["items"]}
    check("еда: остаток 6€", abs(by_name["еда"]["left"] - 6.0) < 1e-9, f"{by_name['еда']['left']:.2f}")
    warn = warning_lines(plan, only_category="еда")
    check("предупреждение по еде при остатке 6€ (порог 7)", any("еда" in w and "⚠️" in w for w in warn),
          str(warn))
    check("предупреждения по другим категориям не примешиваются",
          warning_lines(plan, only_category="бытовое") == [], str(warning_lines(plan, only_category="бытовое")))

    # перерасход
    spent_over = {"еда": 140.0}
    plan_over = build_plan(350.0, cats, subs, spent_over, warn_threshold=7.0)
    by_name_over = {it["name"]: it for it in plan_over["items"]}
    check("еда: перерасход −40€ (100 − 140)", abs(by_name_over["еда"]["left"] + 40.0) < 1e-9)
    check("перерасход помечен 🔴", any("🔴" in w and "еда" in w for w in warning_lines(plan_over)))
    check("порог 5€ — 6€ не предупреждает",
          warning_lines(build_plan(350.0, cats, subs, {"еда": 94.0}, warn_threshold=5.0),
                        only_category="еда") == [])

    # новая подписка уменьшает резерв
    subs_plus = subs + [{"name": "Netflix", "amount": 15.0, "months": 1.0, "category_name": "подписки"}]
    plan2 = build_plan(350.0, cats, subs_plus, {})
    check("новая подписка 15€ поднимает периодику ровно на 15",
          abs(plan2["periodic_total"] - plan["periodic_total"] - 15.0) < 1e-9)
    check("новая подписка съедает 15€ из резерва",
          abs(reserve_plan(plan2) - (reserve_plan(plan) - 15.0)) < 1e-9,
          f"{reserve_plan(plan):.2f} → {reserve_plan(plan2):.2f}")

    # платёж больше свободных денег — план не сходится, а не «уходит в минус резервом»
    subs_big = subs + [{"name": "Дорогое", "amount": 60.0, "months": 1.0, "category_name": "подписки"}]
    plan3 = build_plan(350.0, cats, subs_big, {})
    check("платёж 60€ при свободных 36.58€: план не сходится на 23.42€",
          plan3["unallocated"] < 0 and abs(plan3["unallocated"] + 23.4167) < 0.01
          and reserve_plan(plan3) == 0.0,
          f"unallocated={plan3['unallocated']:.4f}, резерв={reserve_plan(plan3):.2f}")

    # авто-режим
    cats_auto = [{"name": "еда", "monthly_limit": None}, {"name": "прочее", "monthly_limit": None}]
    plan4 = build_plan(100.0, cats_auto, [], {}, warn_threshold=7.0)
    check("авто: 100€ делятся поровну по 50",
          all(abs(it["plan"] - 50.0) < 1e-9 for it in plan4["items"]) and plan4["auto_share"] == 50.0)

    # категория, у которой и платежи, и фикс-план: план = сумма
    cats_mixed = [{"name": "бытовое", "monthly_limit": 20.0}, {"name": "подписки", "monthly_limit": None}]
    subs_mixed = [{"name": "Hermes", "amount": 23.0, "months": 1.0, "category_name": "бытовое"}]
    plan_mix = build_plan(100.0, cats_mixed, subs_mixed, {}, warn_threshold=7.0)
    mixed_item = next(it for it in plan_mix["items"] if it["name"] == "бытовое")
    check("платежи + фикс-план складываются: 23 + 20 = 43",
          abs(mixed_item["plan"] - 43.0) < 1e-9 and mixed_item["source"] == "mixed",
          f"plan={mixed_item['plan']}, source={mixed_item['source']}")
    check("при суммировании план сходится с доходом 100",
          abs(plan_mix["unallocated"]) < 1e-9, f"{plan_mix['unallocated']:.4f}")

    # платёж с категорией, которой у пользователя нет, всё равно виден в плане
    subs_unknown = [{"name": "X", "amount": 10.0, "months": 1.0, "category_name": "которой_нет"}]
    plan_unknown = build_plan(100.0, [{"name": "еда", "monthly_limit": 90.0}], subs_unknown, {})
    check("платёж с чужой категорией показан отдельной строкой",
          any(it["name"] == "которой_нет" and abs(it["plan"] - 10.0) < 1e-9 for it in plan_unknown["items"])
          and abs(plan_unknown["unallocated"]) < 1e-9,
          f"unallocated={plan_unknown['unallocated']:.4f}")

    # дни до конца месяца
    _, _, days_left = month_bounds(datetime(2026, 10, 20))
    check("дней до конца октября (20.10) = 12", days_left == 12, str(days_left))


def test_db(tmpdir):
    db_file = os.path.join(tmpdir, "test.db")
    db = Database(db_file)

    async def scenario():
        await db.init()
        for name in ["еда", "бытовое", "подписки", "общежитие", "стрижка"]:
            await db.add_category(USER, name)
        await db.set_setting(USER, SET_PLANNED_INCOME, 350)
        await db.set_setting(USER, SET_WARN_THRESHOLD, 7)
        check("settings: плановый доход 350", await db.get_setting_float(USER, SET_PLANNED_INCOME, 0) == 350.0)
        check("settings: порог 7", await db.get_setting_float(USER, SET_WARN_THRESHOLD, 0) == 7.0)

        await db.set_category_monthly_plan(USER, "еда", 130)
        cat = await db.get_category_by_name(USER, "еда")
        check("месячный план «еда» = 130", cat["monthly_limit"] == 130.0)
        await db.set_category_monthly_plan(USER, "бытовое", None)
        cat = await db.get_category_by_name(USER, "бытовое")
        check("«бытовое» — режим авто (NULL)", cat["monthly_limit"] is None)

        for s in subs_fixture():
            cat_row = await db.get_category_by_name(USER, s["category_name"])
            await db.add_subscription(USER, s["name"], s["amount"], s["period"], None,
                                      months=s["months"], category_id=cat_row["id"])
        subs = await db.get_subscriptions(USER)
        check("7 платежей в базе", len(subs) == 7, str(len(subs)))
        check("платёж знает категорию", all(s["category_name"] for s in subs))
        check("периодика из БД = 193.42", abs(sum(monthly_amount(s) for s in subs) - 193.4167) < 0.01,
              f"{sum(monthly_amount(s) for s in subs):.4f}")

        # доход
        await db.add_income(USER, 350.0, "EUR", "стипендия", datetime.now().strftime("%Y-%m-%d"))
        month_start, today_str, _ = month_bounds()
        income = await db.get_income_total(USER, month_start, today_str)
        check("доход этого месяца = 350", income == 350.0, str(income))
        await db.add_to_balance(USER, 350.0)
        check("баланс 350 после дохода", await db.get_balance(USER) == 350.0)

        # траты
        cat_food = await db.get_category_by_name(USER, "еда")
        for amount, desc in [(10.48, "Теско"), (5.46, "Кооп"), (3.99, "Tesco")]:
            await db.add_expense(USER, amount, "EUR", cat_food["id"], desc,
                                 datetime.now().strftime("%Y-%m-%d"))
        spent = await db.get_spending_by_category(USER, month_start, today_str)
        food = next(r for r in spent if r["category_name"] == "еда")
        check("траты по еде за месяц = 19.93", abs(food["spent"] - 19.93) < 1e-9, f"{food['spent']:.2f}")

        daily = await db.get_daily_spending(USER, month_start, today_str)
        check("траты по дням: одна запись на сегодня (3 траты, 19.93€)",
              len(daily) == 1 and daily[0]["cnt"] == 3 and abs(daily[0]["spent"] - 19.93) < 1e-9,
              str(daily))
        empty = await db.get_daily_spending(USER, "2000-01-01", "2000-01-31")
        check("траты по дням: пустой период = пустой список", empty == [], str(empty))

        # полный план из БД
        bplan = build_plan(income, await db.get_categories(USER), subs,
                           {r["category_name"]: r["spent"] for r in spent}, 7.0)
        by_name = {it["name"]: it for it in bplan["items"]}
        check("план из БД: еда остаток 110.07", abs(by_name["еда"]["left"] - 110.07) < 1e-9,
              f"{by_name['еда']['left']:.2f}")
        check("план из БД сходится", abs(bplan["unallocated"]) < 0.05, f"{bplan['unallocated']:.4f}")

        # очистка
        await db.reset_user(USER)
        check("reset_user: траты удалены", await db.get_expenses_count(USER) == 0)
        check("reset_user: доходы удалены", await db.get_income_total(USER) == 0.0)
        check("reset_user: подписки удалены", len(await db.get_subscriptions(USER)) == 0)
        check("reset_user: настройки удалены", await db.get_setting(USER, SET_PLANNED_INCOME) is None)

    asyncio.run(scenario())


def test_migration(tmpdir):
    """Старая база (как в проде до обновления) должна пережить init()."""
    path = os.path.join(tmpdir, "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO categories (user_id, name, weekly_limit) VALUES (?, 'еда', NULL)", (USER,))
    conn.execute("INSERT INTO subscriptions (user_id, name, amount, period, next_charge_date) "
                 "VALUES (?, 'Зал', 39.0, 'monthly', NULL)", (USER,))
    conn.execute("INSERT INTO balances (user_id, amount) VALUES (?, 346.07)", (USER,))
    conn.execute("INSERT INTO expenses (user_id, amount, currency, category_id, description, expense_date) "
                 "VALUES (?, 10.48, 'EUR', 1, 'Теско', '2026-10-17')", (USER,))
    conn.commit()
    conn.close()

    db = Database(path)

    async def scenario():
        await db.init()
        subs = await db.get_subscriptions(USER)
        check("миграция: подписка на месте", len(subs) == 1 and subs[0]["name"] == "Зал")
        check("миграция: months доставлен = 1", subs[0]["months"] == 1.0, str(subs[0]["months"]))
        cat = await db.get_category_by_name(USER, "еда")
        check("миграция: новая колонка monthly_limit есть", "monthly_limit" in cat and cat["monthly_limit"] is None)
        check("миграция: старые траты целы", await db.get_expenses_count(USER) == 1)
        check("миграция: баланс цел", await db.get_balance(USER) == 346.07)
        plan = build_plan(346.07, await db.get_categories(USER), subs, {}, 7.0)
        check("миграция: план считается", abs(plan["periodic_total"] - 39.0) < 1e-9)

    asyncio.run(scenario())


def test_parse_dates():
    os.environ.setdefault("BOT_TOKEN", "test-token-not-used")
    from bot import parse_date_input
    today = datetime.now().strftime("%Y-%m-%d")
    check("дата '-' = сегодня", parse_date_input("-") == today)
    check("дата '12.10' = 2026-10-12", parse_date_input("12.10").endswith("-10-12"), parse_date_input("12.10"))
    check("дата '01.11.2026' разобрана", parse_date_input("01.11.2026") == "2026-11-01")
    check("мусор вместо даты = None", parse_date_input("зарплата") is None)


def test_timezone():
    """TZ управляет «сегодня»: без него контейнер живёт в UTC и день съезжает назад."""
    import time as _time

    os.environ.setdefault("BOT_TOKEN", "test-token-not-used")
    import bot  # noqa: F401  — импорт бота выставляет TZ по умолчанию

    check("TZ по умолчанию — Europe/Bratislava", os.environ.get("TZ") == "Europe/Bratislava",
          str(os.environ.get("TZ")))

    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Pacific/Kiritimati"      # UTC+14, максимальный сдвиг
        _time.tzset()
        now = datetime.now()
        utc = datetime.now(timezone.utc).replace(tzinfo=None)
        shift = (now - utc).total_seconds() / 3600
        check("TZ=UTC+14 сдвигает локальное время на +14ч", abs(shift - 14) < 0.1, f"{shift:.2f}ч")

        _, today_str, _ = month_bounds()
        check("month_bounds берёт локальную дату, а не UTC", today_str == now.strftime("%Y-%m-%d"),
              f"{today_str} vs {now.strftime('%Y-%m-%d')} (UTC {utc.strftime('%Y-%m-%d')})")
        check("now_local() = локальное время", abs((bot.now_local() - now).total_seconds()) < 2)
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        _time.tzset()


def main():
    tmpdir = tempfile.mkdtemp(prefix="expensebot-test-")
    print(f"tmp: {tmpdir}\n")
    test_pure_math()
    print()
    test_income_and_spending()
    print()
    test_db(tmpdir)
    print()
    test_migration(tmpdir)
    print()
    test_parse_dates()
    print()
    test_timezone()
    print()
    print(f"проверок: {CHECKS[0]}, провалов: {len(FAILS)}")
    for f in FAILS:
        print("  FAILED:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
