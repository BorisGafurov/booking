VERSION = "014_reminder_claim_backoff"
NAME = "Atomic per-platform reminder claims with retry backoff"

# The worker used to mark a reminder as sent only after delivery, so two
# processes sharing the database could both send it, and a permanently failing
# recipient (blocked bot, deleted VK/MAX account) was retried every minute.
# These columns let one worker atomically claim a reminder per platform and
# record attempts plus the next allowed retry time.

PG_COLUMNS = [
    "attempts INTEGER NOT NULL DEFAULT 0",
    "next_attempt_at TIMESTAMP WITH TIME ZONE",
    "claimed_at TIMESTAMP WITH TIME ZONE",
    "claimed_by VARCHAR(64)",
]

SQLITE_COLUMNS = [
    "attempts INTEGER NOT NULL DEFAULT 0",
    "next_attempt_at TIMESTAMP",
    "claimed_at TIMESTAMP",
    "claimed_by TEXT",
]


def apply(conn) -> None:
    if conn.is_pg:
        for col in PG_COLUMNS:
            conn.execute(
                f"ALTER TABLE reminder_deliveries ADD COLUMN IF NOT EXISTS {col}"
            )
    else:
        for col in SQLITE_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE reminder_deliveries ADD COLUMN {col}")
            except Exception:
                # Column already exists
                pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_retry "
        "ON reminder_deliveries(status, next_attempt_at)"
    )
    conn.commit()


def rollback(conn) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_reminder_deliveries_retry")
    for col in ("attempts", "next_attempt_at", "claimed_at", "claimed_by"):
        try:
            if conn.is_pg:
                conn.execute(
                    f"ALTER TABLE reminder_deliveries DROP COLUMN IF EXISTS {col}"
                )
            else:
                conn.execute(f"ALTER TABLE reminder_deliveries DROP COLUMN {col}")
        except Exception:
            pass
    conn.commit()
