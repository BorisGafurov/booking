VERSION = "013_appointment_discounts"
NAME = "Add discount fields to appointments for percentage and ruble discounts"


def apply(conn) -> None:
    for col_def in [
        ("discount_amount", "INTEGER NOT NULL DEFAULT 0"),
        ("discount_type", "VARCHAR(16) DEFAULT NULL"),
        ("discount_value", "NUMERIC(8, 2) NOT NULL DEFAULT 0"),
        ("original_price", "INTEGER DEFAULT NULL"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE appointments ADD COLUMN IF NOT EXISTS {col_def[0]} {col_def[1]}"
            )
            conn.commit()
        except Exception:
            try:
                # SQLite fallback: does not support IF NOT EXISTS in ADD COLUMN
                conn.execute(f"ALTER TABLE appointments ADD COLUMN {col_def[0]} {col_def[1]}")
                conn.commit()
            except Exception:
                pass


def rollback(conn) -> None:
    pass
