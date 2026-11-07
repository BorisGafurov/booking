VERSION = "015_booking_locks"
NAME = "Cross-process booking locks for SQLite deployments"

# PostgreSQL serializes concurrent bookings with pg_advisory_xact_lock, which
# SQLite does not have. Several bots (Telegram/VK/MAX), the website and the
# reminder worker may share one bot.db file, so the "check for overlaps, then
# INSERT" sequence could run twice concurrently and double-book a slot.
#
# A row in this table is used as a lock: SQLite grants only one writer at a
# time, so an INSERT/UPSERT here takes the RESERVED lock for the whole
# transaction and holds it until commit. Any other process that tries to take
# the same day waits (busy_timeout) and then re-reads the appointments table
# after the winner committed, which makes the overlap check reliable.


def apply(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_locks (
            lock_key VARCHAR(64) PRIMARY KEY,
            holder VARCHAR(64),
            acquired_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()


def rollback(conn) -> None:
    conn.execute("DROP TABLE IF EXISTS booking_locks")
    conn.commit()
