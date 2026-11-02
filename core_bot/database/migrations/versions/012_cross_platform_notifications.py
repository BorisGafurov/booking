VERSION = "012_cross_platform_notifications"
NAME = "Per-platform reminder deliveries and multi-platform review invites"


def apply(conn) -> None:
    # Each bot worker sends reminders only through its own messenger. The old
    # appointment-level flags were shared, so the first worker to send blocked
    # the other platforms. This table records delivery per appointment/kind/
    # platform instead.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminder_deliveries (
            id SERIAL PRIMARY KEY,
            appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
            kind VARCHAR(24) NOT NULL,
            platform VARCHAR(16) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'sent',
            last_error TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP WITH TIME ZONE
        );
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_reminder_delivery "
        "ON reminder_deliveries(appointment_id, kind, platform)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminder_deliveries_lookup "
        "ON reminder_deliveries(kind, platform, status)"
    )

    # Review invitations were unique per appointment, so only the platform
    # stored in appointments.platform ever received an invite. Allow one row
    # per linked messenger.
    conn.execute("DROP INDEX IF EXISTS uq_review_invite_appointment")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_review_invite_appointment_platform "
        "ON review_notifications(appointment_id, platform) WHERE kind = 'review_invite'"
    )
    conn.commit()


def rollback(conn) -> None:
    conn.execute("DROP TABLE IF EXISTS reminder_deliveries")
    conn.execute("DROP INDEX IF EXISTS uq_review_invite_appointment_platform")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_review_invite_appointment "
        "ON review_notifications(appointment_id) WHERE kind = 'review_invite'"
    )
    conn.commit()
