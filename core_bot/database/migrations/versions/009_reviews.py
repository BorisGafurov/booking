VERSION = "009_reviews"
NAME = "Add paid visit flag, reviews, and review notifications"


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
    # The site can create the review tables before the bots run this migration.
    # Backfill old completed visits only when this migration introduces the
    # tables, otherwise new visits would silently become "paid".
    reviews_existed = False
    try:
        if conn.is_pg:
            row = conn.execute(
                "SELECT to_regclass('public.reviews') AS name"
            ).fetchone()
            reviews_existed = bool(row and row["name"])
        else:
            row = conn.raw_conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'reviews'"
            ).fetchone()
            reviews_existed = bool(row)
    except Exception:
        reviews_existed = False

    # 1. Payment confirmation is separate from the visit status: a master can
    #    complete a visit and only later confirm that it was paid.
    _add_column_if_missing(
        conn,
        "appointments",
        "payment_confirmed",
        "BOOLEAN NOT NULL DEFAULT FALSE",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(
        conn,
        "appointments",
        "paid_at",
        "TIMESTAMP WITH TIME ZONE",
        "TIMESTAMP",
    )

    # 2. Every review is a separate row, linked to the client and the visit.
    #    appointment_id is unique: one review per visit. Old reviews are never
    #    overwritten; a client can edit their own review for 24 hours.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            appointment_id INTEGER NOT NULL UNIQUE REFERENCES appointments(id) ON DELETE CASCADE,
            rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
            comment TEXT NOT NULL,
            source VARCHAR(16) NOT NULL DEFAULT 'site',
            status VARCHAR(16) NOT NULL DEFAULT 'published',
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # 3. Cross-process notification outbox. The site and bots share PostgreSQL;
    #    each bot worker picks up messages for its own platform.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_notifications (
            id SERIAL PRIMARY KEY,
            kind VARCHAR(32) NOT NULL,
            appointment_id INTEGER REFERENCES appointments(id) ON DELETE CASCADE,
            review_id INTEGER REFERENCES reviews(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            platform VARCHAR(16) NOT NULL,
            recipient_id BIGINT,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP WITH TIME ZONE
        );
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_appointment ON reviews(appointment_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_rating ON reviews(rating)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_notifications_pending "
        "ON review_notifications(status, platform, kind)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_review_invite_appointment "
        "ON review_notifications(appointment_id) WHERE kind = 'review_invite'"
    )

    # 4. Existing completed visits were paid in the past. Backfill the flag so
    #    clients can review those visits without the master clicking every card.
    if not reviews_existed:
        val_true = True if conn.is_pg else 1
        conn.execute(
            """
            UPDATE appointments
            SET payment_confirmed = %s,
                paid_at = COALESCE(paid_at, updated_at, appointment_time)
            WHERE status = 'completed'
              AND (payment_confirmed = %s OR payment_confirmed IS NULL)
            """,
            (val_true, False if conn.is_pg else 0),
        )

    conn.commit()


def rollback(conn) -> None:
    conn.execute("DROP TABLE IF EXISTS review_notifications")
    conn.execute("DROP TABLE IF EXISTS reviews")
    for column in ("paid_at", "payment_confirmed"):
        try:
            if conn.is_pg:
                conn.execute(f"ALTER TABLE appointments DROP COLUMN IF EXISTS {column}")
            else:
                conn.execute(f"ALTER TABLE appointments DROP COLUMN {column}")
        except Exception:
            # Older SQLite cannot drop a column; leaving it is safe.
            pass
    conn.commit()
