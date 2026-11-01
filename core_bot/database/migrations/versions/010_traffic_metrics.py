VERSION = "010_traffic_metrics"
NAME = "Add traffic sources, visitor event logs and appointment attribution"


def _columns(conn, table: str) -> set[str]:
    if conn.is_pg:
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        ).fetchall()
        return {row["column_name"] for row in rows}
    rows = conn.raw_conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _add_column_if_missing(conn, table: str, column: str, pg_type: str, sqlite_type: str) -> None:
    if column in _columns(conn, table):
        return
    col_type = pg_type if conn.is_pg else sqlite_type
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def apply(conn) -> None:
    # 1. Track which traffic source led to each booking
    _add_column_if_missing(
        conn,
        "appointments",
        "traffic_source",
        "VARCHAR(64)",
        "TEXT",
    )

    # 2. Registered traffic sources (managed by master or auto-registered on incoming clean links)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS traffic_sources (
            id SERIAL PRIMARY KEY,
            slug VARCHAR(64) UNIQUE NOT NULL,
            title VARCHAR(128) NOT NULL,
            description TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # 3. Lightweight, privacy-friendly event tracking log
    # event_type:
    #   'visit'       - initial landing page load
    #   'book_start'  - clicked on booking wizard / button
    #   'book_success'- successfully created an appointment
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS traffic_events (
            id SERIAL PRIMARY KEY,
            source_slug VARCHAR(64) NOT NULL,
            event_type VARCHAR(32) NOT NULL DEFAULT 'visit',
            visitor_hash VARCHAR(64),
            device VARCHAR(32),
            browser VARCHAR(32),
            ip_hash VARCHAR(64),
            appointment_id INTEGER REFERENCES appointments(id) ON DELETE SET NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_sources_slug ON traffic_sources(slug)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_events_source ON traffic_events(source_slug)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_events_created ON traffic_events(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_events_type ON traffic_events(event_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_events_visitor ON traffic_events(visitor_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_traffic_source ON appointments(traffic_source)")

    # 4. Seed default recognizable traffic sources
    seed_sources = [
        ("tg", "Telegram-канал", "Переходы из Telegram постов и шапки профиля"),
        ("vk", "ВКонтакте", "Переходы из группы и личных сообщений VK"),
        ("inst", "Instagram", "Переходы из Stories и ссылки в шапке профиля"),
        ("maps", "Карты (Яндекс / 2ГИС)", "Карточки организации на картах"),
        ("card", "Визитка / QR на стойке", "Сканирование физического QR-кода в барбершопе"),
        ("direct", "Прямой переход", "Прямой ввод адреса сайта или закладки"),
    ]

    for slug, title, desc in seed_sources:
        try:
            conn.execute(
                """
                INSERT INTO traffic_sources (slug, title, description)
                VALUES (%s, %s, %s)
                ON CONFLICT (slug) DO NOTHING;
                """,
                (slug, title, desc),
            )
        except Exception:
            # SQLite fallback for tests
            conn.execute(
                """
                INSERT OR IGNORE INTO traffic_sources (slug, title, description)
                VALUES (?, ?, ?)
                """,
                (slug, title, desc),
            )

    conn.commit()


def rollback(conn) -> None:
    conn.execute("DROP TABLE IF EXISTS traffic_events")
    conn.execute("DROP TABLE IF EXISTS traffic_sources")
    try:
        if conn.is_pg:
            conn.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS traffic_source")
        else:
            conn.execute("ALTER TABLE appointments DROP COLUMN traffic_source")
    except Exception:
        pass
    conn.commit()
