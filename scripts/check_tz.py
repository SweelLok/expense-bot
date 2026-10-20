"""Проверка часового пояса и отчёта по дням прямо в контейнере.

Запуск: python /app/scripts/check_tz.py [user_id]
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

os.environ.setdefault("BOT_TOKEN", "local-check-no-token")   # для локального запуска

import bot  # noqa: E402


async def main(user_id: int):
    await bot.db.init()   # на пустом файле создаст таблицы, а не упадёт
    local = bot.now_local()
    utc = datetime.now(timezone.utc)
    print(f"TZ:        {os.environ.get('TZ')}")
    print(f"локально:  {local:%Y-%m-%d %H:%M:%S} ({local:%a})")
    print(f"UTC:       {utc:%Y-%m-%d %H:%M:%S}")
    print(f"сдвиг:     {(local - utc.replace(tzinfo=None)).total_seconds() / 3600:+.1f} ч")
    month_start, today, days_left = bot.month_bounds(local)
    print(f"месяц:     с {month_start}, сегодня {today}, до конца месяца {days_left} дн.")
    print(f"база БД:   {bot.db.db_path}")
    print()
    try:
        print(await bot.days_text(user_id))
    except Exception as exc:      # пустая/чужая база — показываем причину, а не трейсбек
        print(f"отчёт по дням недоступен: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 1084411025
    asyncio.run(main(uid))
