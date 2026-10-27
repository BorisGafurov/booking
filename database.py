"""Database module facade.

Provides backward-compatible exports from core_bot.database.
"""

from core_bot.database.connection import (
    DATABASE_URL,
    DATA_DIR,
    DB_PATH,
    IS_POSTGRES,
    DBConnection,
    _get_sqlite_connection,
    get_connection,
    get_pg_pool,
    init_db,
)
from core_bot.database.migrations.runner import (
    ensure_migration_table,
    get_applied_migrations,
    get_migration_status,
    run_migrations,
)

# Backward-compatibility alias
_init_db_schema = run_migrations

__all__ = [
    "DATABASE_URL",
    "DATA_DIR",
    "DB_PATH",
    "IS_POSTGRES",
    "DBConnection",
    "_get_sqlite_connection",
    "get_connection",
    "get_pg_pool",
    "init_db",
    "_init_db_schema",
    "ensure_migration_table",
    "get_applied_migrations",
    "get_migration_status",
    "run_migrations",
]
