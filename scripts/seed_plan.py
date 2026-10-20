"""Готовит чистое БД с настроенным планом (категории, планы, платежи, порог).

Запуск: ./venv/bin/python scripts/seed_plan.py [путь_к_бд] [user_id]

Скрипт создаёт базу заново: если файл существует — перезаписывает, старых трат и доходов не будет.
"""
import asyncio
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from db import Database, SET_PLANNED_INCOME, SET_PLAN_BUDGET, SET_PLAN_MODE, SET_WARN_THRESHOLD  # noqa: E402

DEFAULT_USER = 1084411025
DEFAULT_PATH = os.path.join(PROJECT, "build", "expenses.db")

CATEGORIES = ["еда", "бытовое", "подписки", "общежитие", "стрижка", "транспорт", "развлечения",
              "прочее", "резерв"]

# Месячные планы категорий (фикс). None — режим «авто» (делить/забирать остаток).
PLANS = {
    "еда": 100.0,          # жёсткий лимит: 100€, сколько бы денег ни было
    "бытовое": 20.0,
    "транспорт": 0.0,      # не планируем тратить
    "развлечения": 0.0,
    "прочее": 0.0,
    "резерв": None,        # авто: сюда уходит весь остаток денег месяца
    # подписки, общежитие, стрижка — план считается из их платежей
}

# (название, сумма, период (месяцев), категория)
PERIODIC = [
    ("4ка", 16.0, 1.0, "подписки"),
    ("vodafon", 10.0, 1.0, "подписки"),
    ("Зал", 39.0, 1.0, "подписки"),
    ("Hermes", 23.0, 1.0, "подписки"),
    ("Fly.io", 25.0, 12.0, "подписки"),
    ("Общежитие", 180.0, 2.0, "общежитие"),
    ("Стрижка", 20.0, 1.5, "стрижка"),
]

PLANNED_INCOME = 350.0
PLAN_BUDGET = 350.0
PLAN_MODE = "available"    # план считается от денег в наличии, а не от дохода
WARN_THRESHOLD = 7.0

PERIOD_NAMES = {1.0: "monthly", 2.0: "2months", 3.0: "quarterly", 6.0: "halfyear", 12.0: "yearly"}


async def seed(path: str, user_id: int):
    if os.path.exists(path):
        os.remove(path)
    print(f"БД: {path}")
    print(f"Пользователь: {user_id}")

    db = Database(path)
    await db.init()

    for name in CATEGORIES:
        await db.add_category(user_id, name)
    print(f"Категорий: {len(CATEGORIES)}")

    for name, value in PLANS.items():
        await db.set_category_monthly_plan(user_id, name, value)
    print(f"Месячные планы: {PLANS}")

    total_periodic = 0.0
    for name, amount, months, cat in PERIODIC:
        cat_row = await db.get_category_by_name(user_id, cat)
        period = PERIOD_NAMES.get(months, "custom")
        await db.add_subscription(user_id, name, amount, period, None, months=months,
                                  category_id=cat_row["id"])
        total_periodic += amount / months
    print(f"Периодических платежей: {len(PERIODIC)} → {total_periodic:.2f}€/мес")

    await db.set_setting(user_id, SET_PLANNED_INCOME, PLANNED_INCOME)
    await db.set_setting(user_id, SET_PLAN_BUDGET, PLAN_BUDGET)
    await db.set_setting(user_id, SET_PLAN_MODE, PLAN_MODE)
    await db.set_setting(user_id, SET_WARN_THRESHOLD, WARN_THRESHOLD)
    await db.set_balance(user_id, 0.0)
    print(f"Основа плана: {PLAN_MODE} (деньги в наличии, доход на план не влияет)")
    print(f"Бюджет месяца (для режима «бюджет»): {PLAN_BUDGET}€, плановый доход: {PLANNED_INCOME}€, "
          f"порог: {WARN_THRESHOLD}€, баланс: 0€")

    fixed = sum(v for v in PLANS.values() if v)
    print(f"\nПериодика: {total_periodic:.2f}€/мес; фикс-планы: {fixed:.2f}€")
    print(f"«резерв» — авто: забирает весь остаток денег после платежей и фикс-планов")
    print("Трат, доходов и сообщений в базе нет — старт с нуля.")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    user_id = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_USER
    os.makedirs(os.path.dirname(path), exist_ok=True)
    asyncio.run(seed(path, user_id))
    print(f"\nГотово: {path} ({os.path.getsize(path)} байт)")


if __name__ == "__main__":
    main()
