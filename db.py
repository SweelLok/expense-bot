import aiosqlite
import os
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "expenses.db"))


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
                    UNIQUE(user_id, name)
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    amount REAL NOT NULL,
                    period TEXT NOT NULL,
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

                CREATE TABLE IF NOT EXISTS message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)
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

    async def set_category_limit(self, user_id: int, category_name: str, limit: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE categories SET weekly_limit = ? WHERE user_id = ? AND name = ?",
                (limit, user_id, category_name),
            )
            await db.commit()

    # --- Subscriptions ---

    async def get_subscriptions(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM subscriptions WHERE user_id = ? ORDER BY name", (user_id,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def add_subscription(self, user_id: int, name: str, amount: float, period: str, next_charge: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO subscriptions (user_id, name, amount, period, next_charge_date) VALUES (?, ?, ?, ?, ?)",
                (user_id, name, amount, period, next_charge),
            )
            await db.commit()

    async def delete_subscription(self, user_id: int, sub_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM subscriptions WHERE user_id = ? AND id = ?", (user_id, sub_id))
            await db.commit()
            return cursor.rowcount > 0

    # --- Expenses ---

    async def add_expense(self, user_id: int, amount: float, currency: str, category_id: int,
                           description: str | None, expense_date: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO expenses (user_id, amount, currency, category_id, description, expense_date) VALUES (?, ?, ?, ?, ?, ?)",
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
                                  end_date: str | None = None) -> int:
        query = "SELECT COUNT(*) as cnt FROM expenses WHERE user_id = ?"
        params: list = [user_id]
        if start_date:
            query += " AND expense_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND expense_date <= ?"
            params.append(end_date)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cur:
                row = await cur.fetchone()
                return row["cnt"] if row else 0

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
                "INSERT INTO message_log (user_id, chat_id, message_id, is_bot) VALUES (?, ?, ?, ?)",
                (user_id, chat_id, message_id, is_bot),
            )
            await db.commit()

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
