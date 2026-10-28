import os

VERSION = "004_data_cleanups"
NAME = "Clean up master placeholder phone numbers in appointments"


def _master_phones() -> list[str]:
    raw = os.getenv("MASTER_PHONES") or os.getenv("MASTER_PHONE") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def apply(conn) -> None:
    phones = _master_phones()
    if not phones:
        return
    try:
        placeholders = ", ".join(["%s"] * len(phones))
        conn.execute(
            f"""
            UPDATE appointments
            SET phone = '', platform_user_id = 0
            WHERE phone IN ({placeholders})
              AND (client_name LIKE '%Клиент%' OR platform = 'masters')
            """,
            tuple(phones),
        )
        conn.commit()
    except Exception:
        pass


def rollback(conn) -> None:
    pass
