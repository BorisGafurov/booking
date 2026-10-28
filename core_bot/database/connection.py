import logging
import os
import sqlite3
import time
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
ALLOW_SQLITE_FALLBACK = os.getenv("ALLOW_SQLITE_FALLBACK", "false").lower() in ("true", "1", "yes")

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
    # Look for shared_data folder if multiple bot repos exist
    parent_bot_dir = Path(__file__).resolve().parent.parent.parent.parent
    if (parent_bot_dir / "bot_booking").exists():
        DB_PATH = parent_bot_dir / "shared_data" / "bot.db"
    else:
        DB_PATH = Path(__file__).resolve().parent.parent.parent / "bot.db"

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

    def _transform_query(self, query: str) -> str:
        if self.is_pg:
            return query
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
        return query

    def execute(self, query: str, params: Any = None):
        if params is None:
            params = ()
        query = self._transform_query(query)
        return self.raw_conn.execute(query, params)

    def executescript(self, script: str):
        if not self.is_pg:
            script = self._transform_query(script)
            return self.raw_conn.executescript(script)
        else:
            return self.raw_conn.execute(script)

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
    db_file = DB_PATH
    try:
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_file, timeout=20.0)
    except Exception:
        db_file = Path(__file__).resolve().parent.parent.parent / "bot.db"
        conn = sqlite3.connect(db_file, timeout=20.0)
    conn.create_function("lower", 1, lambda s: s.lower() if s is not None else None)
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
    if _pg_unreachable:
        # Fail closed: never silently switch to SQLite when the fallback is disabled.
        if not ALLOW_SQLITE_FALLBACK:
            logger.critical(
                "PostgreSQL is marked unreachable and SQLite fallback is disabled; refusing connection"
            )
            raise RuntimeError(
                "Database connection error: PostgreSQL unavailable and SQLite fallback disabled"
            )
        if now - _last_pg_check > 60:
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
                if not ALLOW_SQLITE_FALLBACK:
                    logger.critical("PostgreSQL pool connection failed with ALLOW_SQLITE_FALLBACK=False: %s", pe)
                    raise RuntimeError(f"Database connection error: PostgreSQL unavailable and SQLite fallback disabled: {pe}") from pe
                logger.warning("PostgreSQL pool connection failed: %s; falling back to SQLite", pe)
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
                if not ALLOW_SQLITE_FALLBACK:
                    logger.critical("PostgreSQL direct connect failed with ALLOW_SQLITE_FALLBACK=False: %s", pe)
                    raise RuntimeError(f"Database connection error: PostgreSQL unavailable and SQLite fallback disabled: {pe}") from pe
                logger.warning("PostgreSQL direct connect failed: %s; falling back to SQLite", pe)

    with _get_sqlite_connection() as s_conn:
        yield s_conn


def init_db() -> None:
    """Initialize database schemas via migrations for SQLite and PostgreSQL."""
    from core_bot.database.migrations.runner import run_migrations

    # If Postgres is configured: run migrations on Postgres
    if IS_POSTGRES:
        pool = get_pg_pool()
        if pool is not None:
            try:
                with pool.connection(timeout=3.0) as conn:
                    run_migrations(DBConnection(conn, is_pg=True))
            except Exception as e:
                if not ALLOW_SQLITE_FALLBACK:
                    raise RuntimeError(f"Could not run PG migrations at startup: {e}") from e
                logger.warning("Could not run PG migrations at startup: %s", e)
        else:
            try:
                conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=3)
                try:
                    with conn:
                        run_migrations(DBConnection(conn, is_pg=True))
                finally:
                    conn.close()
            except Exception as e:
                if not ALLOW_SQLITE_FALLBACK:
                    raise RuntimeError(f"Could not run PG migrations at startup: {e}") from e
                logger.warning("Could not run PG migrations at startup: %s", e)

    # Initialize SQLite database if in SQLite mode or fallback is enabled
    if not IS_POSTGRES or ALLOW_SQLITE_FALLBACK:
        with _get_sqlite_connection() as conn:
            run_migrations(conn)
