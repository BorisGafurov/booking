VERSION = "007_masters_client_fields"
NAME = "Add Masters CRM client fields to users"

# Columns mirrored from the Masters CRM client export ("Клиенты" sheet):
# Дата рождения, Регистрация в Masters, Записей всего, Отмененных записей,
# Оказано услуг, Группы, Instagram, Facebook, VK, Email, Комментарий.
PG_COLUMNS = [
    "birthday VARCHAR(64)",
    "masters_registered VARCHAR(32)",
    "masters_group VARCHAR(128)",
    "masters_appointments_total INTEGER DEFAULT 0",
    "masters_cancelled_total INTEGER DEFAULT 0",
    "masters_revenue INTEGER DEFAULT 0",
    "social_instagram VARCHAR(255)",
    "social_facebook VARCHAR(255)",
    "social_vk VARCHAR(255)",
    "email VARCHAR(255)",
    "comment TEXT",
    "masters_imported_at TIMESTAMP WITH TIME ZONE",
]

SQLITE_COLUMNS = [
    "birthday TEXT",
    "masters_registered TEXT",
    "masters_group TEXT",
    "masters_appointments_total INTEGER DEFAULT 0",
    "masters_cancelled_total INTEGER DEFAULT 0",
    "masters_revenue INTEGER DEFAULT 0",
    "social_instagram TEXT",
    "social_facebook TEXT",
    "social_vk TEXT",
    "email TEXT",
    "comment TEXT",
    "masters_imported_at TIMESTAMP",
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
