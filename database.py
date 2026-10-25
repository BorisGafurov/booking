import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DATA_DIR = os.getenv("DATA_DIR")
if DATA_DIR:
    DB_PATH = Path(DATA_DIR) / "bot.db"
else:
    DB_PATH = Path(__file__).resolve().parent / "bot.db"

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cursor = conn.execute("PRAGMA table_info(users)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "last_name" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_name TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_user_id INTEGER NOT NULL,
                invited_user_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'started',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                visited_at TIMESTAMP,
                rewarded_at TIMESTAMP,
                FOREIGN KEY (referrer_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (invited_user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                referral_id INTEGER NOT NULL UNIQUE,
                amount INTEGER NOT NULL DEFAULT 300,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (referral_id) REFERENCES referrals(id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_user_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_referrals_invited ON referrals(invited_user_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rewards_user ON rewards(user_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rewards_status ON rewards(status)"
        )

