import aiosqlite
import os
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "expenses.db"))

# Ключи таблицы settings
SET_WARN_THRESHOLD = "warn_threshold"      # порог предупреждения по остатку категории, EUR
SET_PLANNED_INCOME = "planned_income"      # плановый доход месяца, EUR (режим «доход месяца»)
SET_PLAN_MODE = "plan_mode"                # от чего считать план: available | income | budget
SET_PLAN_BUDGET = "plan_budget"            # бюджет месяца, EUR (режим «бюджет месяца»)


async def _ensure_column(db, table: str, column: str, decl: str):
    """Добавляет колонку, если её нет — миграция старых баз без потери данных."""
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        cols = [r[1] for r in await cur.fetchall()]
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS balances (
                    user_id INTEGER PRIMARY KEY,
                    amount REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'EUR'
                );

                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    weekly_limit REAL,
                    monthly_limit REAL,
                    UNIQUE(user_id, name)
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    amount REAL NOT NULL,
                    period TEXT NOT NULL,
                    months REAL DEFAULT 1,
                    category_id INTEGER,
                    next_charge_date TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'EUR',
                    category_id INTEGER NOT NULL,
                    description TEXT,
                    expense_date TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (category_id) REFERENCES categories(id)
                );

                CREATE TABLE IF NOT EXISTS incomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'EUR',
                    description TEXT,
                    income_date TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS settings (
                    user_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (user_id, key)
                );

                CREATE TABLE IF NOT EXISTS message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)

            # Миграции баз, созданных прошлыми версиями бота
            await _ensure_column(db, "categories", "monthly_limit", "monthly_limit REAL")
            await _ensure_column(db, "subscriptions", "months", "months REAL DEFAULT 1")
            await _ensure_column(db, "subscriptions", "category_id", "category_id INTEGER")

            # Периодические платежи без явного периода считаем ежемесячными
            await db.execute("UPDATE subscriptions SET months = 1 WHERE months IS NULL")
            await db.commit()

    async def reset_user(self, user_id: int):
        """Полная очистка данных пользователя: траты, доходы, подписки, баланс, лог сообщений, настройки."""
        async with aiosqlite.connect(self.db_path) as db:
            for table in ("expenses", "incomes", "subscriptions", "balances", "message_log", "settings"):
                await db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            await db.commit()

    # --- Settings ---

    async def get_setting(self, user_id: int, key: str, default: str | None = None) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT value FROM settings WHERE user_id = ? AND key = ?", (user_id, key)
            ) as cur:
                row = await cur.fetchone()
                return row["value"] if row and row["value"] is not None else default

    async def get_setting_float(self, user_id: int, key: str, default: float) -> float:
        raw = await self.get_setting(user_id, key)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    async def set_setting(self, user_id: int, key: str, value):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (user_id, key, str(value)),
            )
            await db.commit()

    # --- Balances ---

    async def get_balance(self, user_id: int) -> float:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT amount FROM balances WHERE user_id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
                return row["amount"] if row else 0.0

    async def set_balance(self, user_id: int, amount: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO balances (user_id, amount) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET amount = ?",
                (user_id, amount, amount),
            )
            await db.commit()

    async def add_to_balance(self, user_id: int, amount: float):
        current = await self.get_balance(user_id)
        await self.set_balance(user_id, current + amount)

    # --- Categories ---

    async def get_categories(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM categories WHERE user_id = ? ORDER BY name", (user_id,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_category_by_name(self, user_id: int, name: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM categories WHERE user_id = ? AND name = ?", (user_id, name)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def add_category(self, user_id: int, name: str) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO categories (user_id, name) VALUES (?, ?) ON CONFLICT(user_id, name) DO NOTHING",
                (user_id, name),
            )
            await db.commit()
            return await self.get_category_by_name(user_id, name)

    async def rename_category(self, user_id: int, category_id: int, name: str):
        """Переименование категории: траты ссылаются по id, поэтому история не рвётся."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE categories SET name = ? WHERE user_id = ? AND id = ?", (name, user_id, category_id)
            )
            await db.commit()

    async def delete_category(self, user_id: int, category_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM categories WHERE user_id = ? AND id = ?", (user_id, category_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def set_category_limit(self, user_id: int, category_name: str, limit: float):
        """Недельный лимит (старый механизм, оставлен для совместимости)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE categories SET weekly_limit = ? WHERE user_id = ? AND name = ?",
                (limit, user_id, category_name),
            )
            await db.commit()

    async def set_category_monthly_plan(self, user_id: int, category_name: str, plan: float | None):
        """Месячный план категории. plan=None — режим «авто» (делит свободный остаток)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE categories SET monthly_limit = ? WHERE user_id = ? AND name = ?",
                (plan, user_id, category_name),
            )
            await db.commit()

    # --- Subscriptions (периодические платежи) ---

    async def get_subscriptions(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT s.*, c.name AS category_name FROM subscriptions s
                   LEFT JOIN categories c ON s.category_id = c.id
                   WHERE s.user_id = ? ORDER BY s.amount DESC, s.name""",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def add_subscription(self, user_id: int, name: str, amount: float, period: str,
                               next_charge: str | None = None, months: float = 1.0,
                               category_id: int | None = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO subscriptions (user_id, name, amount, period, months, category_id, next_charge_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, name, amount, period, months, category_id, next_charge),
            )
            await db.commit()

    async def delete_subscription(self, user_id: int, sub_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM subscriptions WHERE user_id = ? AND id = ?", (user_id, sub_id))
            await db.commit()
            return cursor.rowcount > 0

    async def update_subscription(self, user_id: int, sub_id: int, **fields):
        """Меняет поля платежа (name, amount, period, months, category_id)."""
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [user_id, sub_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE subscriptions SET {set_clause} WHERE user_id = ? AND id = ?", values)
            await db.commit()

    async def get_subscription(self, user_id: int, sub_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT s.*, c.name AS category_name FROM subscriptions s
                   LEFT JOIN categories c ON s.category_id = c.id
                   WHERE s.user_id = ? AND s.id = ?""",
                (user_id, sub_id),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    # --- Incomes ---

    async def add_income(self, user_id: int, amount: float, currency: str, description: str | None,
                         income_date: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO incomes (user_id, amount, currency, description, income_date, created_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))",
                (user_id, amount, currency, description, income_date),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_last_income(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM incomes WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (user_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_incomes(self, user_id: int, start_date: str | None = None, end_date: str | None = None,
                          limit: int = 20, offset: int = 0) -> list[dict]:
        query = "SELECT * FROM incomes WHERE user_id = ?"
        params: list = [user_id]
        if start_date:
            query += " AND income_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND income_date <= ?"
            params.append(end_date)
        query += " ORDER BY income_date DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_income_total(self, user_id: int, start_date: str | None = None,
                               end_date: str | None = None) -> float:
        query = "SELECT COALESCE(SUM(amount), 0) AS total FROM incomes WHERE user_id = ?"
        params: list = [user_id]
        if start_date:
            query += " AND income_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND income_date <= ?"
            params.append(end_date)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cur:
                row = await cur.fetchone()
                return float(row["total"]) if row else 0.0

    async def get_income_by_id(self, user_id: int, income_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM incomes WHERE user_id = ? AND id = ?", (user_id, income_id)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def delete_income(self, user_id: int, income_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM incomes WHERE user_id = ? AND id = ?", (user_id, income_id))
            await db.commit()
            return cursor.rowcount > 0

    # --- Expenses ---

    async def add_expense(self, user_id: int, amount: float, currency: str, category_id: int,
                           description: str | None, expense_date: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO expenses (user_id, amount, currency, category_id, description, expense_date, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
                (user_id, amount, currency, category_id, description, expense_date),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_last_expense(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT e.*, c.name as category_name FROM expenses e
                   JOIN categories c ON e.category_id = c.id
                   WHERE e.user_id = ? ORDER BY e.created_at DESC, e.id DESC LIMIT 1""",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_expense_by_id(self, user_id: int, expense_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT e.*, c.name as category_name FROM expenses e
                   JOIN categories c ON e.category_id = c.id
                   WHERE e.user_id = ? AND e.id = ?""",
                (user_id, expense_id),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def delete_expense(self, user_id: int, expense_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM expenses WHERE user_id = ? AND id = ?", (user_id, expense_id))
            await db.commit()
            return cursor.rowcount > 0

    async def update_expense(self, user_id: int, expense_id: int, **fields):
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [user_id, expense_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE expenses SET {set_clause} WHERE user_id = ? AND id = ?", values)
            await db.commit()

    async def get_expenses(self, user_id: int, start_date: str | None = None,
                           end_date: str | None = None, category_id: int | None = None,
                           limit: int = 50, offset: int = 0) -> list[dict]:
        query = """SELECT e.*, c.name as category_name FROM expenses e
                   JOIN categories c ON e.category_id = c.id WHERE e.user_id = ?"""
        params: list = [user_id]
        if start_date:
            query += " AND e.expense_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND e.expense_date <= ?"
            params.append(end_date)
        if category_id:
            query += " AND e.category_id = ?"
            params.append(category_id)
        query += " ORDER BY e.expense_date DESC, e.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_expenses_count(self, user_id: int, start_date: str | None = None,
                                  end_date: str | None = None,
                                  category_id: int | None = None) -> int:
        query = "SELECT COUNT(*) as cnt FROM expenses WHERE user_id = ?"
        params: list = [user_id]
        if start_date:
            query += " AND expense_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND expense_date <= ?"
            params.append(end_date)
        if category_id:
            query += " AND category_id = ?"
            params.append(category_id)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cur:
                row = await cur.fetchone()
                return row["cnt"] if row else 0

    async def get_spending_by_category(self, user_id: int, start_date: str, end_date: str) -> list[dict]:
        """Сумма трат по категориям за произвольный период."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.id AS category_id, c.name AS category_name,
                          COALESCE(SUM(e.amount), 0) AS spent
                   FROM categories c
                   LEFT JOIN expenses e ON e.category_id = c.id
                        AND e.expense_date >= ? AND e.expense_date <= ?
                   WHERE c.user_id = ?
                   GROUP BY c.id ORDER BY c.name""",
                (start_date, end_date, user_id),
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_weekly_spending_by_category(self, user_id: int) -> list[dict]:
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.name as category_name, c.weekly_limit, COALESCE(SUM(e.amount), 0) as spent
                   FROM categories c
                   LEFT JOIN expenses e ON e.category_id = c.id AND e.expense_date >= ?
                   WHERE c.user_id = ?
                   GROUP BY c.id ORDER BY c.name""",
                (week_ago, user_id),
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_monthly_spending_by_category(self, user_id: int) -> list[dict]:
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.name as category_name, COALESCE(SUM(e.amount), 0) as spent
                   FROM categories c
                   LEFT JOIN expenses e ON e.category_id = c.id AND e.expense_date >= ?
                   WHERE c.user_id = ?
                   GROUP BY c.id ORDER BY c.name""",
                (month_ago, user_id),
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    # --- Message Log ---

    async def log_message(self, user_id: int, chat_id: int, message_id: int, is_bot: int = 0):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO message_log (user_id, chat_id, message_id, is_bot, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
                (user_id, chat_id, message_id, is_bot),
            )
            await db.commit()

    # --- Отчёты по дням ---

    async def get_daily_spending(self, user_id: int, start_date: str, end_date: str) -> list[dict]:
        """Сумма трат по каждому дню периода: [{day, spent, cnt}, ...]."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT expense_date AS day, COALESCE(SUM(amount), 0) AS spent, COUNT(*) AS cnt
                   FROM expenses
                   WHERE user_id = ? AND expense_date >= ? AND expense_date <= ?
                   GROUP BY expense_date ORDER BY expense_date""",
                (user_id, start_date, end_date),
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    # --- Отчёты по месяцам ---

    async def get_monthly_totals(self, user_id: int, start_date: str) -> list[dict]:
        """Траты и доходы по месяцам: [{month: '2026-10', spent, cnt, income}, ...].

        start_date — граница окна (YYYY-MM-DD, включительно). Возвращаются только месяцы,
        в которых что-то было; пропуски достраивает вызывающая сторона, чтобы график
        не «схлопывал» пустые месяцы.
        """
        totals: dict[str, dict] = {}
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT substr(expense_date, 1, 7) AS month,
                          COALESCE(SUM(amount), 0) AS spent, COUNT(*) AS cnt
                   FROM expenses
                   WHERE user_id = ? AND expense_date >= ?
                   GROUP BY month ORDER BY month""",
                (user_id, start_date),
            ) as cur:
                for row in await cur.fetchall():
                    totals[row["month"]] = {
                        "month": row["month"], "spent": float(row["spent"]),
                        "cnt": int(row["cnt"]), "income": 0.0,
                    }
            async with db.execute(
                """SELECT substr(income_date, 1, 7) AS month,
                          COALESCE(SUM(amount), 0) AS income
                   FROM incomes
                   WHERE user_id = ? AND income_date >= ?
                   GROUP BY month ORDER BY month""",
                (user_id, start_date),
            ) as cur:
                for row in await cur.fetchall():
                    item = totals.setdefault(
                        row["month"],
                        {"month": row["month"], "spent": 0.0, "cnt": 0, "income": 0.0},
                    )
                    item["income"] = float(row["income"])
        return [totals[key] for key in sorted(totals)]

    async def get_message_ids(self, user_id: int, chat_id: int) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT message_id FROM message_log WHERE user_id = ? AND chat_id = ? ORDER BY message_id DESC LIMIT 200",
                (user_id, chat_id),
            ) as cur:
                rows = await cur.fetchall()
                return [r["message_id"] for r in rows]

    async def clear_message_log(self, user_id: int, chat_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM message_log WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            )
            await db.commit()
