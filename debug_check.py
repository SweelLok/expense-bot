#!/usr/bin/env python3
"""Debug script: проверяет текущее состояние базы данных для статистики."""
import sqlite3
from datetime import datetime, timedelta

DB_PATH = "expenses.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("=" * 60)
    print("1. ВСЕ траты в базе (с категорией):")
    print("=" * 60)
    cur.execute("""
        SELECT e.*, c.name as category_name FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        ORDER BY e.expense_date DESC, e.id DESC
    """)
    rows = cur.fetchall()
    if not rows:
        print("  НЕТ ТРАТ В БАЗЕ")
    else:
        for r in rows:
            print(f"  id={r['id']} | дата={r['expense_date']} | сумма={r['amount']} | категория={r['category_name']} | desc={r['description']}")

    print()
    print("=" * 60)
    print("2. Проверка диапазонов дат:")
    print("=" * 60)
    today = datetime.now()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    year_ago = today - timedelta(days=365)

    print(f"  Сейчас: {today.strftime('%Y-%m-%d %H:%M')}")
    print(f"  Неделя назад: {week_ago.strftime('%Y-%m-%d')}")
    print(f"  Месяц назад: {month_ago.strftime('%Y-%m-%d')}")
    print(f"  Год назад: {year_ago.strftime('%Y-%m-%d')}")

    print()
    print("=" * 60)
    print("3. Траты за неделю (10-10..17):")
    print("=" * 60)
    cur.execute("""
        SELECT e.*, c.name as category_name FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        WHERE e.user_id = 1084411025 AND e.expense_date >= ? AND e.expense_date <= ?
        ORDER BY e.expense_date DESC
    """, (week_ago.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")))
    week_rows = cur.fetchall()
    print(f"  Найдено: {len(week_rows)}")
    for r in week_rows:
        print(f"    {r['expense_date']} | {r['amount']} | {r['category_name']}")

    print()
    print("=" * 60)
    print("4. Траты за месяц (09-17..10-17):")
    print("=" * 60)
    month_start = month_ago.strftime("%Y-%m-%d")
    cur.execute("""
        SELECT e.*, c.name as category_name FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        WHERE e.user_id = 1084411025 AND e.expense_date >= ? AND e.expense_date <= ?
        ORDER BY e.expense_date DESC
    """, (month_start, today.strftime("%Y-%m-%d")))
    month_rows = cur.fetchall()
    print(f"  Найдено: {len(month_rows)}")
    for r in month_rows:
        print(f"    {r['expense_date']} | {r['amount']} | {r['category_name']}")

    print()
    print("=" * 60)
    print("5. Все траты (без фильтра даты):")
    print("=" * 60)
    cur.execute("""
        SELECT e.*, c.name as category_name FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        ORDER BY e.expense_date DESC
    """)
    all_rows = cur.fetchall()
    print(f"  Найдено: {len(all_rows)}")
    for r in all_rows:
        print(f"    {r['expense_date']} | {r['amount']} | {r['category_name']}")

    conn.close()

if __name__ == "__main__":
    main()
