import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

try:
    from psycopg_pool import ConnectionPool
except ImportError:
    ConnectionPool = None

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_POSTGRES = bool(
    DATABASE_URL
    and (
        DATABASE_URL.startswith("postgres://")
        or DATABASE_URL.startswith("postgresql://")
    )
)

if IS_POSTGRES:
    if "sslmode=allow" in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("sslmode=allow", "sslmode=prefer")
    elif "sslmode=" not in DATABASE_URL:
        sep = "&" if "?" in DATABASE_URL else "?"
        DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=prefer"

DATA_DIR = os.getenv("DATA_DIR")
if DATA_DIR:
    DB_PATH = Path(DATA_DIR) / "bot.db"
else:
    parent_bot_dir = Path(__file__).resolve().parent.parent
    if (parent_bot_dir / "bot_booking").exists():
        DB_PATH = parent_bot_dir / "shared_data" / "bot.db"
    else:
        DB_PATH = Path(__file__).resolve().parent / "bot.db"

import time

_pg_pool = None
_pg_unreachable = False
_last_pg_check = 0.0


def get_pg_pool():
    global _pg_pool
    if _pg_pool is None and IS_POSTGRES and ConnectionPool is not None and not _pg_unreachable:
        try:
            _pg_pool = ConnectionPool(
                conninfo=DATABASE_URL,
                min_size=0,
                max_size=10,
                max_idle=300,
                kwargs={"row_factory": dict_row, "connect_timeout": 3},
                open=True,
            )
        except Exception as e:
            logger.warning("Could not create PG connection pool: %s", e)
    return _pg_pool


class DBConnection:
    """Unified wrapper around SQLite and PostgreSQL connections."""

    def __init__(self, raw_conn, is_pg: bool = False):
        self.raw_conn = raw_conn
        self.is_pg = is_pg

    def execute(self, query: str, params: Any = None):
        if params is None:
            params = ()
        if not self.is_pg:
            query = query.replace("%s", "?")
            query = query.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
            query = query.replace("BIGINT", "INTEGER")
            query = query.replace("VARCHAR(128)", "TEXT")
            query = query.replace("VARCHAR(64)", "TEXT")
            query = query.replace("VARCHAR(32)", "TEXT")
            query = query.replace("VARCHAR(16)", "TEXT")
            query = query.replace("VARCHAR(8)", "TEXT")
            query = query.replace("BOOLEAN NOT NULL DEFAULT FALSE", "INTEGER NOT NULL DEFAULT 0")
            query = query.replace("BOOLEAN NOT NULL DEFAULT TRUE", "INTEGER NOT NULL DEFAULT 1")
            query = query.replace("= FALSE", "= 0")
            query = query.replace("= TRUE", "= 1")
            query = query.replace("IS FALSE", "= 0")
            query = query.replace("IS TRUE", "= 1")
            query = query.replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP")
            query = query.replace("TIMESTAMP WITHOUT TIME ZONE", "TIMESTAMP")
        return self.raw_conn.execute(query, params)

    def commit(self):
        self.raw_conn.commit()

    def close(self):
        self.raw_conn.close()

    def __enter__(self):
        self.raw_conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.raw_conn.__exit__(exc_type, exc_val, exc_tb)


@contextmanager
def _get_sqlite_connection() -> Generator[DBConnection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        with conn:
            yield DBConnection(conn, is_pg=False)
    finally:
        conn.close()


@contextmanager
def get_connection() -> Generator[DBConnection, None, None]:
    global _pg_unreachable, _last_pg_check
    now = time.time()
    if _pg_unreachable and (now - _last_pg_check > 60):
        _pg_unreachable = False
        _last_pg_check = now

    if IS_POSTGRES and not _pg_unreachable:
        pool = get_pg_pool()
        if pool is not None:
            try:
                with pool.connection(timeout=1.5) as conn:
                    yield DBConnection(conn, is_pg=True)
                    return
            except Exception as pe:
                _pg_unreachable = True
                _last_pg_check = now
                logger.debug("PostgreSQL pool connection failed: %s; falling back to SQLite", pe)
        else:
            try:
                conn = psycopg.connect(
                    DATABASE_URL,
                    row_factory=dict_row,
                    connect_timeout=2,
                )
                try:
                    with conn:
                        yield DBConnection(conn, is_pg=True)
                        return
                finally:
                    conn.close()
            except Exception as pe:
                _pg_unreachable = True
                _last_pg_check = now
                logger.debug("PostgreSQL direct connect failed: %s; falling back to SQLite", pe)

    with _get_sqlite_connection() as s_conn:
        yield s_conn


def init_db() -> None:
    # 1. Always initialize SQLite database to ensure fallback is ready
    with _get_sqlite_connection() as conn:
        _init_db_schema(conn)

    # 2. If Postgres is enabled, also try initializing Postgres schema
    if IS_POSTGRES and not _pg_unreachable:
        try:
            pool = get_pg_pool()
            if pool is not None:
                with pool.connection(timeout=3.0) as conn:
                    _init_db_schema(DBConnection(conn, is_pg=True))
        except Exception as e:
            logger.debug("Could not initialize PG schema at startup: %s", e)


def _init_db_schema(conn: DBConnection) -> None:
        if conn.is_pg:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE,
                    vk_id BIGINT UNIQUE,
                    max_user_id BIGINT UNIQUE,
                    phone VARCHAR(32) UNIQUE,
                    username VARCHAR(128),
                    first_name VARCHAR(128),
                    last_name VARCHAR(128),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS referrals (
                    id SERIAL PRIMARY KEY,
                    referrer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    invited_user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                    status VARCHAR(32) NOT NULL DEFAULT 'started',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    visited_at TIMESTAMP WITH TIME ZONE,
                    rewarded_at TIMESTAMP WITH TIME ZONE
                );

                CREATE TABLE IF NOT EXISTS rewards (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    referral_id INTEGER NOT NULL UNIQUE REFERENCES referrals(id) ON DELETE CASCADE,
                    amount INTEGER NOT NULL DEFAULT 300,
                    status VARCHAR(32) NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    used_at TIMESTAMP WITH TIME ZONE
                );

                CREATE TABLE IF NOT EXISTS appointments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    platform VARCHAR(32) NOT NULL DEFAULT 'telegram',
                    platform_user_id BIGINT NOT NULL DEFAULT 0,
                    client_name VARCHAR(128),
                    phone VARCHAR(32) NOT NULL,
                    services TEXT NOT NULL,
                    total_price INTEGER NOT NULL DEFAULT 0,
                    appointment_time TIMESTAMP WITH TIME ZONE NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'confirmed',
                    client_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
                    reminder_evening_sent BOOLEAN NOT NULL DEFAULT FALSE,
                    reminder_morning_sent BOOLEAN NOT NULL DEFAULT FALSE,
                    reminder_2h_sent BOOLEAN NOT NULL DEFAULT FALSE,
                    retention_18d_sent BOOLEAN NOT NULL DEFAULT FALSE,
                    masters_id VARCHAR(64) UNIQUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );

                ALTER TABLE appointments ADD COLUMN IF NOT EXISTS client_confirmed BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reminder_morning_sent BOOLEAN NOT NULL DEFAULT FALSE;

                CREATE TABLE IF NOT EXISTS masters_session (
                    id SERIAL PRIMARY KEY,
                    master_phone VARCHAR(32),
                    cookie_phpsessid TEXT,
                    cookie_csrf TEXT,
                    logged_in_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                );

                CREATE TABLE IF NOT EXISTS user_roles (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    platform VARCHAR(32) NOT NULL DEFAULT 'telegram',
                    platform_user_id BIGINT NOT NULL DEFAULT 0,
                    role VARCHAR(32) NOT NULL DEFAULT 'master',
                    permissions TEXT NOT NULL DEFAULT 'all',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS master_schedule (
                    id SERIAL PRIMARY KEY,
                    day_key VARCHAR(16) NOT NULL UNIQUE,
                    is_working BOOLEAN NOT NULL DEFAULT TRUE,
                    start_time VARCHAR(8) NOT NULL DEFAULT '10:00',
                    end_time VARCHAR(8) NOT NULL DEFAULT '20:00',
                    note TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
                CREATE INDEX IF NOT EXISTS idx_users_vk_id ON users(vk_id);
                CREATE INDEX IF NOT EXISTS idx_users_max_user_id ON users(max_user_id);
                CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
                CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_user_id);
                CREATE INDEX IF NOT EXISTS idx_referrals_invited ON referrals(invited_user_id);
                CREATE INDEX IF NOT EXISTS idx_rewards_user ON rewards(user_id);
                CREATE INDEX IF NOT EXISTS idx_rewards_status ON rewards(status);
                CREATE INDEX IF NOT EXISTS idx_appointments_time ON appointments(appointment_time);
                CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments(phone);
                CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments(status);
                CREATE INDEX IF NOT EXISTS idx_appointments_client_confirmed ON appointments(client_confirmed);
                CREATE INDEX IF NOT EXISTS idx_user_roles_platform ON user_roles(platform, platform_user_id);
                CREATE INDEX IF NOT EXISTS idx_master_schedule_day ON master_schedule(day_key);
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE,
                    vk_id INTEGER UNIQUE,
                    max_user_id INTEGER UNIQUE,
                    phone TEXT UNIQUE,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Ensure columns exist if older sqlite table
            cursor = conn.raw_conn.execute("PRAGMA table_info(users)")
            columns = [row["name"] for row in cursor.fetchall()]
            for col in ["telegram_id", "vk_id", "max_user_id", "phone", "last_name", "updated_at"]:
                if col not in columns:
                    conn.raw_conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")

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
                """
                CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    platform TEXT NOT NULL DEFAULT 'telegram',
                    platform_user_id INTEGER NOT NULL DEFAULT 0,
                    client_name TEXT,
                    phone TEXT NOT NULL,
                    services TEXT NOT NULL,
                    total_price INTEGER NOT NULL DEFAULT 0,
                    appointment_time TIMESTAMP NOT NULL,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    client_confirmed INTEGER NOT NULL DEFAULT 0,
                    reminder_evening_sent INTEGER NOT NULL DEFAULT 0,
                    reminder_morning_sent INTEGER NOT NULL DEFAULT 0,
                    reminder_2h_sent INTEGER NOT NULL DEFAULT 0,
                    retention_18d_sent INTEGER NOT NULL DEFAULT 0,
                    masters_id TEXT UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cursor = conn.raw_conn.execute("PRAGMA table_info(appointments)")
            app_cols = [row["name"] for row in cursor.fetchall()]
            for col in ["client_confirmed", "reminder_morning_sent"]:
                if col not in app_cols:
                    conn.raw_conn.execute(f"ALTER TABLE appointments ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS masters_session (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    master_phone TEXT,
                    cookie_phpsessid TEXT,
                    cookie_csrf TEXT,
                    logged_in_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_roles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL DEFAULT 'telegram',
                    platform_user_id INTEGER NOT NULL DEFAULT 0,
                    role TEXT NOT NULL DEFAULT 'master',
                    permissions TEXT NOT NULL DEFAULT 'all',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS master_schedule (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day_key TEXT NOT NULL UNIQUE,
                    is_working INTEGER NOT NULL DEFAULT 1,
                    start_time TEXT NOT NULL DEFAULT '10:00',
                    end_time TEXT NOT NULL DEFAULT '20:00',
                    note TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_vk_id ON users(vk_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_max_user_id ON users(max_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_invited ON referrals(invited_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rewards_user ON rewards(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rewards_status ON rewards(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_time ON appointments(appointment_time)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments(phone)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_client_confirmed ON appointments(client_confirmed)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user_roles_platform ON user_roles(platform, platform_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_master_schedule_day ON master_schedule(day_key)")
