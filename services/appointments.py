import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo

from database import get_connection
from services.referrals import normalize_phone

logger = logging.getLogger(__name__)

SARATOV_TZ = ZoneInfo("Europe/Saratov")


def get_current_saratov_time() -> datetime.datetime:
    return datetime.datetime.now(SARATOV_TZ)


def create_or_update_appointment(
    phone: str,
    appointment_time: datetime.datetime,
    services: str,
    total_price: int,
    client_name: str | None = None,
    platform: str = "telegram",
    platform_user_id: int = 0,
    masters_id: str | None = None,
) -> int:
    norm_phone = normalize_phone(phone)
    if appointment_time.tzinfo is None:
        appointment_time = appointment_time.replace(tzinfo=SARATOV_TZ)

    with get_connection() as conn:
        # Check if user already exists in users table by phone
        user_row = conn.execute(
            "SELECT id, telegram_id, vk_id, max_user_id, first_name, last_name FROM users WHERE phone = %s",
            (norm_phone,),
        ).fetchone()

        user_db_id = None
        if user_row:
            user_db_id = user_row["id"]
            if not client_name:
                parts = [p for p in (user_row["first_name"], user_row["last_name"]) if p]
                if parts:
                    client_name = " ".join(parts)
            if not platform_user_id or platform_user_id == 0:
                if platform == "telegram" and user_row["telegram_id"]:
                    platform_user_id = user_row["telegram_id"]
                elif platform == "vk" and user_row["vk_id"]:
                    platform_user_id = user_row["vk_id"]
                elif platform == "max" and user_row["max_user_id"]:
                    platform_user_id = user_row["max_user_id"]
                elif user_row["telegram_id"]:
                    platform = "telegram"
                    platform_user_id = user_row["telegram_id"]
                elif user_row["vk_id"]:
                    platform = "vk"
                    platform_user_id = user_row["vk_id"]
                elif user_row["max_user_id"]:
                    platform = "max"
                    platform_user_id = user_row["max_user_id"]

        # Check existing by masters_id if provided
        if masters_id:
            existing = conn.execute(
                "SELECT id FROM appointments WHERE masters_id = %s", (masters_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE appointments
                    SET user_id = %s,
                        platform = %s,
                        platform_user_id = %s,
                        client_name = %s,
                        phone = %s,
                        services = %s,
                        total_price = %s,
                        appointment_time = %s,
                        status = 'confirmed',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        user_db_id,
                        platform,
                        platform_user_id,
                        client_name or "",
                        norm_phone,
                        services,
                        total_price,
                        appointment_time if conn.is_pg else appointment_time.strftime("%Y-%m-%d %H:%M:%S"),
                        existing["id"],
                    ),
                )
                conn.commit()
                return existing["id"]

        # Insert new appointment
        if conn.is_pg:
            res = conn.execute(
                """
                INSERT INTO appointments (
                    user_id, platform, platform_user_id, client_name, phone,
                    services, total_price, appointment_time, status, masters_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', %s)
                RETURNING id
                """,
                (
                    user_db_id,
                    platform,
                    platform_user_id,
                    client_name or "",
                    norm_phone,
                    services,
                    total_price,
                    appointment_time,
                    masters_id,
                ),
            ).fetchone()
            conn.commit()
            return res["id"]
        else:
            conn.execute(
                """
                INSERT INTO appointments (
                    user_id, platform, platform_user_id, client_name, phone,
                    services, total_price, appointment_time, status, masters_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', %s)
                """,
                (
                    user_db_id,
                    platform,
                    platform_user_id,
                    client_name or "",
                    norm_phone,
                    services,
                    total_price,
                    appointment_time.strftime("%Y-%m-%d %H:%M:%S"),
                    masters_id,
                ),
            )
            cursor = conn.raw_conn.execute("SELECT last_insert_rowid()")
            row_id = cursor.fetchone()[0]
            conn.commit()
            return row_id


def cancel_appointment(appointment_id: int) -> tuple[bool, str, dict | None]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = %s", (appointment_id,)
        ).fetchone()
        if not row:
            return False, "Запись не найдена", None
        if row["status"] == "cancelled":
            return False, "Запись уже отменена", dict(row)

        conn.execute(
            "UPDATE appointments SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (appointment_id,),
        )
        conn.commit()
        res = dict(row)
        res["status"] = "cancelled"
        return True, "Запись успешно отменена", res


def get_appointment(appointment_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = %s", (appointment_id,)
        ).fetchone()
        return dict(row) if row else None


def get_user_upcoming_appointments(platform: str, platform_user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM appointments
            WHERE platform = %s AND platform_user_id = %s AND status = 'confirmed'
            ORDER BY appointment_time ASC
            """,
            (platform, platform_user_id),
        ).fetchall()
        return [dict(r) for r in rows]


def get_due_evening_reminders(platform: str | None = None) -> list[dict]:
    """
    Find appointments scheduled for tomorrow where reminder_evening_sent is False.
    Check is intended to fire in the evening (>= 18:00) of the day before visit.
    """
    now = get_current_saratov_time()
    if now.hour < 18:
        return []

    tomorrow_start = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_end = tomorrow_start + datetime.timedelta(days=1)

    with get_connection() as conn:
        query = """
            SELECT * FROM appointments
            WHERE status = 'confirmed'
              AND (reminder_evening_sent = FALSE OR reminder_evening_sent = 0)
              AND appointment_time >= %s AND appointment_time < %s
        """
        t_start = tomorrow_start if conn.is_pg else tomorrow_start.strftime("%Y-%m-%d %H:%M:%S")
        t_end = tomorrow_end if conn.is_pg else tomorrow_end.strftime("%Y-%m-%d %H:%M:%S")
        params = [t_start, t_end]
        if platform:
            query += " AND platform = %s"
            params.append(platform)
        query += " ORDER BY appointment_time ASC"

        rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def get_due_2h_reminders(platform: str | None = None) -> list[dict]:
    """
    Find appointments starting in <= 2 hours from now where reminder_2h_sent is False.
    """
    now = get_current_saratov_time()
    limit = now + datetime.timedelta(hours=2, minutes=5)

    with get_connection() as conn:
        query = """
            SELECT * FROM appointments
            WHERE status = 'confirmed'
              AND (reminder_2h_sent = FALSE OR reminder_2h_sent = 0)
              AND appointment_time > %s AND appointment_time <= %s
        """
        p_now = now if conn.is_pg else now.strftime("%Y-%m-%d %H:%M:%S")
        p_limit = limit if conn.is_pg else limit.strftime("%Y-%m-%d %H:%M:%S")
        params = [p_now, p_limit]
        if platform:
            query += " AND platform = %s"
            params.append(platform)
        query += " ORDER BY appointment_time ASC"

        rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def get_due_retention_reminders(platform: str | None = None) -> list[dict]:
    """
    Find appointments completed ~18 days ago (2.5 weeks) where retention_18d_sent is False.
    Window: 17 to 22 days ago, daytime (10:00 - 20:00 Saratov time).
    """
    now = get_current_saratov_time()
    if now.hour < 10 or now.hour >= 20:
        return []

    older_than = now - datetime.timedelta(days=17)
    newer_than = now - datetime.timedelta(days=22)

    with get_connection() as conn:
        query = """
            SELECT * FROM appointments
            WHERE status = 'confirmed'
              AND (retention_18d_sent = FALSE OR retention_18d_sent = 0)
              AND appointment_time <= %s AND appointment_time >= %s
        """
        p_older = older_than if conn.is_pg else older_than.strftime("%Y-%m-%d %H:%M:%S")
        p_newer = newer_than if conn.is_pg else newer_than.strftime("%Y-%m-%d %H:%M:%S")
        params = [p_older, p_newer]
        if platform:
            query += " AND platform = %s"
            params.append(platform)
        query += " ORDER BY appointment_time ASC"

        rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def mark_reminder_sent(appointment_id: int, reminder_type: str) -> None:
    column = {
        "evening": "reminder_evening_sent",
        "2h": "reminder_2h_sent",
        "retention": "retention_18d_sent",
    }.get(reminder_type)

    if not column:
        return

    with get_connection() as conn:
        val = True if conn.is_pg else 1
        conn.execute(
            f"UPDATE appointments SET {column} = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (val, appointment_id),
        )
        conn.commit()
