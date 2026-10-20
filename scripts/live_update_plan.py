"""Точечно применяет новые деньги-настройки к существующей базе (не затирая траты и доходы).

Что делает:
  • «еда» → месячный план 100€ (и жёстко остаётся 100, сколько бы денег ни было);
  • «резерв» → режим «авто»: забирает остаток денег после платежей и фикс-планов;
  • основа плана → «деньги в наличии» (баланс + потрачено с 1-го числа), доход на план не влияет;
  • бюджет месяца → 350€ (для режима «бюджет месяца»).

Перед изменением делает копию файла базы рядом: <db>.bak-ГГГГММДД-ЧЧММСС

Запуск: ./venv/bin/python scripts/live_update_plan.py [путь_к_базе]
        (в контейнере путь берётся из DB_PATH)
"""
import asyncio
import os
import shutil
import sys
from datetime import datetime

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from budget import build_plan, month_bounds, monthly_amount  # noqa: E402
from db import Database, SET_PLAN_BUDGET, SET_PLAN_MODE, SET_WARN_THRESHOLD  # noqa: E402

DEFAULT_USER = 1084411025
FOOD_PLAN = 100.0
MONTH_BUDGET = 350.0
PLAN_MODE = "available"          # деньги в наличии
AUTO_CATEGORIES = ["резерв"]     # забирают остаток


async def show_state(db: Database, user_id: int, title: str):
    cats = await db.get_categories(user_id)
    subs = await db.get_subscriptions(user_id)
    month_start, today_str, _ = month_bounds()
    spending = await db.get_spending_by_category(user_id, month_start, today_str)
    spent_by_cat = {row["category_name"]: float(row["spent"]) for row in spending}
    balance = await db.get_balance(user_id)
    income = await db.get_income_total(user_id, month_start, today_str)
    mode = await db.get_setting(user_id, SET_PLAN_MODE, "available")
    base = balance + sum(spent_by_cat.values()) if mode == "available" else income
    plan = build_plan(base, cats, subs, spent_by_cat, 7.0)

    print(f"\n{title}")
    print(f"  основа: {mode} | баланс {balance:.2f}€ | доход месяца {income:.2f}€ | база {base:.2f}€")
    print(f"  периодика {plan['periodic_total']:.2f}€ | фикс-планы {plan['fixed_total']:.2f}€ | "
          f"не распределено {plan['unallocated']:.2f}€")
    for it in plan["items"]:
        if it["plan"] > 0 or it["spent"] > 0:
            flag = " (авто)" if it["auto"] else ""
            print(f"    {it['name']}: план {it['plan']:.2f}€{flag}")
    return plan


async def main(path: str, user_id: int):
    if not os.path.exists(path):
        print(f"❌ Базы нет: {path}")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak-{stamp}"
    shutil.copy2(path, backup)
    print(f"Копия базы: {backup}")

    db = Database(path)
    await db.init()
    await show_state(db, user_id, "ДО изменений:")

    changes = []

    cat = await db.get_category_by_name(user_id, "еда")
    if cat:
        old = cat["monthly_limit"]
        if old != FOOD_PLAN:
            await db.set_category_monthly_plan(user_id, "еда", FOOD_PLAN)
            changes.append(f"«еда»: {old} → {FOOD_PLAN}")
    else:
        print("⚠️ категории «еда» нет — пропускаю")

    for name in AUTO_CATEGORIES:
        cat = await db.get_category_by_name(user_id, name)
        if cat and cat["monthly_limit"] is not None:
            await db.set_category_monthly_plan(user_id, name, None)
            changes.append(f"«{name}»: {cat['monthly_limit']} → авто (остаток)")

    mode_now = await db.get_setting(user_id, SET_PLAN_MODE, None)
    if mode_now != PLAN_MODE:
        await db.set_setting(user_id, SET_PLAN_MODE, PLAN_MODE)
        changes.append(f"основа плана: {mode_now or '—'} → {PLAN_MODE}")

    budget_now = await db.get_setting_float(user_id, SET_PLAN_BUDGET, 0.0)
    if abs(budget_now - MONTH_BUDGET) > 1e-9:
        await db.set_setting(user_id, SET_PLAN_BUDGET, MONTH_BUDGET)
        changes.append(f"бюджет месяца: {budget_now or '—'} → {MONTH_BUDGET}")

    if await db.get_setting(user_id, SET_WARN_THRESHOLD) is None:
        await db.set_setting(user_id, SET_WARN_THRESHOLD, 7.0)
        changes.append("порог предупреждения: — → 7")

    print("\nИзменения:")
    for line in changes or ["нет — всё уже так"]:
        print(f"  • {line}")

    plan = await show_state(db, user_id, "ПОСЛЕ изменений:")
    print(f"\nИтого в резерв уходит: "
          f"{next((it['plan'] for it in plan['items'] if it['name'] == 'резерв'), 0.0):.2f}€")
    return 0


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "DB_PATH", os.path.join(PROJECT, "expenses.db")
    )
    user = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_USER
    print(f"База: {db_path}")
    sys.exit(asyncio.run(main(db_path, user)))
