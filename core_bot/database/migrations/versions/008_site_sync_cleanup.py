VERSION = "008_site_sync_cleanup"
NAME = "Unify schedule/booking with the site and remove legacy CRM fields"

# Legacy client profile columns -> neutral names.
USER_COLUMN_RENAMES = [
    ("masters_registered", "source_registered"),
    ("masters_group", "client_group"),
    ("masters_appointments_total", "visits_total"),
    ("masters_cancelled_total", "cancelled_total"),
    ("masters_revenue", "revenue_total"),
    ("masters_imported_at", "imported_at"),
]

APPOINTMENT_COLUMNS = [
    "id",
    "user_id",
    "platform",
    "platform_user_id",
    "client_name",
    "phone",
    "services",
    "total_price",
    "appointment_time",
    "duration_min",
    "notes",
    "status",
    "client_confirmed",
    "reminder_evening_sent",
    "reminder_morning_sent",
    "reminder_2h_sent",
    "retention_18d_sent",
    "created_at",
    "updated_at",
]


def _pg_columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def _sqlite_columns(conn, table: str) -> set[str]:
    rows = conn.raw_conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _table_exists(conn, table: str) -> bool:
    if conn.is_pg:
        row = conn.execute("SELECT to_regclass(%s) AS name", (f"public.{table}",)).fetchone()
        return bool(row and row["name"])
    row = conn.raw_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return bool(row)


def _rename_or_merge_user_columns(conn) -> None:
    columns = _pg_columns(conn, "users") if conn.is_pg else _sqlite_columns(conn, "users")
    for old, new in USER_COLUMN_RENAMES:
        if old not in columns:
            continue
        if new in columns:
            conn.execute(
                f"UPDATE users SET {new} = {old}"
                f" WHERE (CAST({new} AS TEXT) IS NULL OR CAST({new} AS TEXT) IN ('', '0'))"
                f" AND {old} IS NOT NULL"
            )
            try:
                conn.execute(f"ALTER TABLE users DROP COLUMN {old}")
            except Exception:
                # Older SQLite cannot drop a column; leaving it is safe.
                pass
        else:
            conn.execute(f"ALTER TABLE users RENAME COLUMN {old} TO {new}")
        columns.discard(old)
        columns.add(new)


def _add_and_backfill_duration(conn) -> None:
    columns = _pg_columns(conn, "appointments") if conn.is_pg else _sqlite_columns(conn, "appointments")
    if "duration_min" not in columns:
        conn.execute("ALTER TABLE appointments ADD COLUMN duration_min INTEGER")
    if "notes" not in columns:
        conn.execute("ALTER TABLE appointments ADD COLUMN notes TEXT")

    rows = conn.execute("SELECT id, services FROM appointments").fetchall()
    for row in rows:
        text = (row["services"] or "").lower()
        has_hair = "стрижк" in text
        has_beard = "бород" in text
        if "combo" in text or (has_hair and has_beard):
            duration = 135
        elif has_beard:
            duration = 60
        else:
            duration = 75
        conn.execute(
            "UPDATE appointments SET duration_min = %s WHERE id = %s",
            (duration, row["id"]),
        )

    if conn.is_pg:
        conn.execute("ALTER TABLE appointments ALTER COLUMN duration_min SET DEFAULT 75")
        conn.execute("UPDATE appointments SET duration_min = 75 WHERE duration_min IS NULL")
        conn.execute("ALTER TABLE appointments ALTER COLUMN duration_min SET NOT NULL")
    else:
        conn.execute("UPDATE appointments SET duration_min = 75 WHERE duration_min IS NULL")


def _rebuild_sqlite_appointments(conn) -> None:
    """Removes the legacy external id column on SQLite via a table rebuild."""
    existing = _sqlite_columns(conn, "appointments")
    if "masters_id" not in existing:
        return

    common = [name for name in APPOINTMENT_COLUMNS if name in existing or name == "duration_min"]
    column_list = ", ".join(common)
    conn.execute("ALTER TABLE appointments RENAME TO appointments_legacy_008")
    conn.execute(
        """
        CREATE TABLE appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            platform TEXT NOT NULL DEFAULT 'telegram',
            platform_user_id INTEGER NOT NULL DEFAULT 0,
            client_name TEXT,
            phone TEXT NOT NULL,
            services TEXT NOT NULL,
            total_price INTEGER NOT NULL DEFAULT 0,
            appointment_time TIMESTAMP NOT NULL,
            duration_min INTEGER NOT NULL DEFAULT 75,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'confirmed',
            client_confirmed INTEGER NOT NULL DEFAULT 0,
            reminder_evening_sent INTEGER NOT NULL DEFAULT 0,
            reminder_morning_sent INTEGER NOT NULL DEFAULT 0,
            reminder_2h_sent INTEGER NOT NULL DEFAULT 0,
            retention_18d_sent INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        f"INSERT INTO appointments ({column_list}) SELECT {column_list} FROM appointments_legacy_008"
    )
    conn.execute("DROP TABLE appointments_legacy_008")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_time ON appointments(appointment_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments(phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments(status)")


def apply(conn) -> None:
    # 1. Client profile fields keep their data under neutral names.
    if _table_exists(conn, "users"):
        _rename_or_merge_user_columns(conn)

    # 2. Appointments get a duration so availability is shared with the site.
    if _table_exists(conn, "appointments"):
        _add_and_backfill_duration(conn)

        if conn.is_pg:
            columns = _pg_columns(conn, "appointments")
            if "masters_id" in columns:
                conn.execute("ALTER TABLE appointments DROP COLUMN masters_id")
        else:
            _rebuild_sqlite_appointments(conn)

        conn.execute("UPDATE appointments SET platform = 'manual' WHERE platform = 'masters'")

    # 3. Legacy session storage is not used anymore.
    if _table_exists(conn, "masters_session"):
        conn.execute("DROP TABLE masters_session")

    # 4. Forget the superseded migration records.
    if _table_exists(conn, "schema_migrations"):
        conn.execute(
            "DELETE FROM schema_migrations WHERE version IN (%s, %s)",
            ("002_masters_session", "007_masters_client_fields"),
        )

    conn.commit()


def rollback(conn) -> None:
    """Best-effort rollback: restore legacy column names."""
    if not _table_exists(conn, "users"):
        return
    columns = _pg_columns(conn, "users") if conn.is_pg else _sqlite_columns(conn, "users")
    for old, new in USER_COLUMN_RENAMES:
        if new not in columns or old in columns:
            continue
        conn.execute(f"ALTER TABLE users RENAME COLUMN {new} TO {old}")
    conn.commit()
