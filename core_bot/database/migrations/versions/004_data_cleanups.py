VERSION = "004_data_cleanups"
NAME = "Clean up master placeholder phone numbers in appointments"


def apply(conn) -> None:
    try:
        conn.execute(
            """
            UPDATE appointments
            SET phone = '', platform_user_id = 0
            WHERE phone IN ('+79173225155', '79173225155', '89173225155')
              AND (client_name LIKE '%Клиент%' OR platform = 'masters')
            """
        )
        conn.commit()
    except Exception:
        pass


def rollback(conn) -> None:
    pass
