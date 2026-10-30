VERSION = "007_client_extra_fields"
NAME = "Add extended client profile fields"

# Дополнительные поля карточки клиента: день рождения, источник регистрации,
# группа, статистика визитов, соцсети и контактные данные.
PG_COLUMNS = [
    "birthday VARCHAR(64)",
    "source_registered VARCHAR(32)",
    "client_group VARCHAR(128)",
    "visits_total INTEGER DEFAULT 0",
    "cancelled_total INTEGER DEFAULT 0",
    "revenue_total INTEGER DEFAULT 0",
    "social_instagram VARCHAR(255)",
    "social_facebook VARCHAR(255)",
    "social_vk VARCHAR(255)",
    "email VARCHAR(255)",
    "comment TEXT",
    "imported_at TIMESTAMP WITH TIME ZONE",
]

SQLITE_COLUMNS = [
    "birthday TEXT",
    "source_registered TEXT",
    "client_group TEXT",
    "visits_total INTEGER DEFAULT 0",
    "cancelled_total INTEGER DEFAULT 0",
    "revenue_total INTEGER DEFAULT 0",
    "social_instagram TEXT",
    "social_facebook TEXT",
    "social_vk TEXT",
    "email TEXT",
    "comment TEXT",
    "imported_at TIMESTAMP",
]


def apply(conn) -> None:
    if conn.is_pg:
        for col in PG_COLUMNS:
            conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col}")
    else:
        for col in SQLITE_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                # Column already exists
                pass
    conn.commit()


def rollback(conn) -> None:
    cols = [c.split()[0] for c in (PG_COLUMNS if conn.is_pg else SQLITE_COLUMNS)]
    for col in cols:
        try:
            if conn.is_pg:
                conn.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS {col}")
            else:
                conn.execute(f"ALTER TABLE users DROP COLUMN {col}")
        except Exception:
            pass
    conn.commit()
