VERSION = "006_remove_guessed_placeholders"
NAME = "Remove guessed Masters placeholder appointments"


def apply(conn) -> None:
    """Delete placeholder appointments guessed from mst.link slot gaps.

    These rows (masters_id like 'mst_YYYY-MM-DD_HHMM', generic client name, no
    phone) were derived from "gaps" in the public schedule and often had the
    wrong time, blocking free slots and showing phantom visits.
    """
    try:
        conn.execute(
            """
            DELETE FROM appointments
            WHERE masters_id LIKE 'mst_%'
              AND (client_name IN ('Клиент', 'Клиент (Masters)') OR client_name IS NULL)
              AND (phone IS NULL OR phone = '')
            """
        )
        conn.commit()
    except Exception:
        pass


def rollback(conn) -> None:
    pass
