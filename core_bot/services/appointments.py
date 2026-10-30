import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo

from core_bot.database import get_connection
from core_bot.services.referrals import is_master_phone, normalize_phone

logger = logging.getLogger(__name__)

SARATOV_TZ = ZoneInfo("Europe/Saratov")


def get_current_saratov_time() -> datetime.datetime:
    return datetime.datetime.now(SARATOV_TZ)


def parse_to_saratov(val: Any) -> datetime.datetime:
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=SARATOV_TZ)
        return val.astimezone(SARATOV_TZ)
    if isinstance(val, str):
        val_str = val.strip()
        try:
            if "Z" in val_str or "+" in val_str or (len(val_str) > 10 and "-" in val_str[10:]):
                dt = datetime.datetime.fromisoformat(val_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=SARATOV_TZ)
                return dt.astimezone(SARATOV_TZ)
        except Exception:
            pass

        val_clean = val_str.replace("T", " ")
        if "." in val_clean:
            val_clean = val_clean.split(".")[0]
        try:
            dt = datetime.datetime.strptime(val_clean[:19], "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=SARATOV_TZ)
        except Exception:
            pass
    return get_current_saratov_time()


class SlotUnavailableError(Exception):
    """Raised when a requested time is outside the schedule or already taken."""


def create_or_update_appointment(
    phone: str,
    appointment_time: datetime.datetime,
    services: str,
    total_price: int,
    client_name: str | None = None,
    platform: str = "telegram",
    platform_user_id: int = 0,
    duration_min: int | None = None,
) -> int:
    """
    Creates a booking in the shared «Срез» database.

    The same date is serialized with a PostgreSQL advisory lock so two clients
    (or a client and the website) cannot take an overlapping slot at the same
    time. Existing appointments that overlap the requested interval are
    rejected with :class:`SlotUnavailableError`.
    """
    from core_bot.services.booking import (
        find_overlapping_appointment,
        service_duration_minutes,
    )

    norm_phone = normalize_phone(phone) if phone else ""
    if appointment_time.tzinfo is None:
        appointment_time = appointment_time.replace(tzinfo=SARATOV_TZ)
    else:
        appointment_time = appointment_time.astimezone(SARATOV_TZ)

    duration = int(duration_min or service_duration_minutes(services))

    with get_connection() as conn:
        # Serialize all writers for this date across bots and the website.
        if conn.is_pg:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"appointments:{appointment_time.date().isoformat()}",),
            )

        dt_value = appointment_time if conn.is_pg else appointment_time.strftime("%Y-%m-%d %H:%M:%S")

        user_row = None
        if norm_phone:
            user_row = conn.execute(
                "SELECT id, telegram_id, vk_id, max_user_id, first_name, last_name FROM users WHERE phone = %s",
                (norm_phone,),
            ).fetchone()
        if not user_row and platform_user_id and platform_user_id != 0:
            col = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_user_id"}.get(platform)
            if col:
                user_row = conn.execute(
                    f"SELECT id, telegram_id, vk_id, max_user_id, first_name, last_name FROM users WHERE {col} = %s",
                    (platform_user_id,),
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

        # Idempotency: the same client pressing the slot twice updates the
        # existing record instead of creating a duplicate.
        if norm_phone and platform_user_id:
            identity_cond = "(phone = %s OR (platform = %s AND platform_user_id = %s))"
            identity_params = (norm_phone, platform, platform_user_id)
        elif norm_phone:
            identity_cond = "phone = %s"
            identity_params = (norm_phone,)
        elif platform_user_id:
            identity_cond = "(platform = %s AND platform_user_id = %s)"
            identity_params = (platform, platform_user_id)
        else:
            identity_cond = "1 = 0"
            identity_params = ()

        existing_row = conn.execute(
            f"""
            SELECT id, status, client_name FROM appointments
            WHERE appointment_time = %s
              AND (status IS NULL OR status != 'cancelled')
              AND {identity_cond}
            ORDER BY id ASC
            LIMIT 1
            """,
            (dt_value, *identity_params),
        ).fetchone()

        if existing_row:
            # A completed visit is history; never reopen it on a repeated click.
            if existing_row["status"] == "completed":
                conn.commit()
                return existing_row["id"]

            effective_name = client_name or ""
            if not effective_name or effective_name in ("Клиент", "Гость", "Гость сайта"):
                effective_name = existing_row["client_name"] or effective_name

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
                    duration_min = %s,
                    appointment_time = %s,
                    status = 'confirmed',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    user_db_id,
                    platform,
                    platform_user_id,
                    effective_name,
                    norm_phone,
                    services,
                    total_price,
                    duration,
                    dt_value,
                    existing_row["id"],
                ),
            )
            conn.commit()
            return existing_row["id"]

        overlap = find_overlapping_appointment(conn, appointment_time, duration)
        if overlap:
            conn.raw_conn.rollback()
            raise SlotUnavailableError(
                "Это время уже занято. Пожалуйста, выберите другой слот."
            )

        if conn.is_pg:
            res = conn.execute(
                """
                INSERT INTO appointments (
                    user_id, platform, platform_user_id, client_name, phone,
                    services, total_price, appointment_time, duration_min, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed')
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
                    dt_value,
                    duration,
                ),
            ).fetchone()
            conn.commit()
            return res["id"]

        conn.execute(
            """
            INSERT INTO appointments (
                user_id, platform, platform_user_id, client_name, phone,
                services, total_price, appointment_time, duration_min, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed')
            """,
            (
                user_db_id,
                platform,
                platform_user_id,
                client_name or "",
                norm_phone,
                services,
                total_price,
                dt_value,
                duration,
            ),
        )
        cursor = conn.raw_conn.execute("SELECT last_insert_rowid()")
        row_id = cursor.fetchone()[0]
        conn.commit()
        return row_id


def _check_appointment_access(
    appt: dict[str, Any],
    platform: str | None = None,
    platform_user_id: int | None = None,
) -> bool:
    """
    Checks if caller is authorized to modify the appointment.
    If platform and platform_user_id are omitted, caller is internal system / worker.
    """
    if platform is None or platform_user_id is None:
        return True

    try:
        from core_bot.services.referrals import is_master_user, get_user_by_platform_id
        if is_master_user(platform_user_id, platform):
            return True

        if appt.get("platform") == platform and appt.get("platform_user_id") == platform_user_id:
            return True

    except Exception as e:
        logger.error("Error in _check_appointment_access: %s", e)
        return False

    return False


def cancel_appointment(
    appointment_id: int,
    platform: str | None = None,
    platform_user_id: int | None = None,
) -> tuple[bool, str, dict | None]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = %s", (appointment_id,)
        ).fetchone()
        if not row:
            return False, "Запись не найдена", None
        appt = dict(row)
        if not _check_appointment_access(appt, platform, platform_user_id):
            return False, "У вас нет прав для изменения этой записи.", None
        if appt["status"] == "cancelled":
            return False, "Запись уже отменена", appt

        conn.execute(
            "UPDATE appointments SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (appointment_id,),
        )
        conn.commit()
        res = dict(appt)
        res["status"] = "cancelled"

    return True, "Запись успешно отменена", res


def confirm_appointment_by_client(
    appointment_id: int,
    platform: str | None = None,
    platform_user_id: int | None = None,
) -> tuple[bool, str, dict | None]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = %s", (appointment_id,)
        ).fetchone()
        if not row:
            return False, "Запись не найдена", None
        appt = dict(row)
        if not _check_appointment_access(appt, platform, platform_user_id):
            return False, "У вас нет прав для изменения этой записи.", None
        if appt["status"] == "cancelled":
            return False, "Запись уже отменена", appt

        val = True if conn.is_pg else 1
        conn.execute(
            "UPDATE appointments SET client_confirmed = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (val, appointment_id),
        )
        conn.commit()
        res = dict(appt)
        res["client_confirmed"] = True

    return True, "Запись успешно подтверждена", res


def get_appointment(appointment_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = %s", (appointment_id,)
        ).fetchone()
        return dict(row) if row else None


def get_user_upcoming_appointments(platform: str, platform_user_id: int) -> list[dict]:
    now = get_current_saratov_time()
    col = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_user_id"}.get(platform)
    with get_connection() as conn:
        params: list[Any] = []
        if conn.is_pg:
            params.append(now)
        else:
            params.append(now.strftime("%Y-%m-%d %H:%M:%S"))
        params.extend([platform, platform_user_id])

        ownership = "platform = %s AND platform_user_id = %s"
        if col:
            ownership += (
                " OR (user_id IS NOT NULL AND user_id = ("
                f"SELECT id FROM users WHERE {col} = %s LIMIT 1"
                "))"
            )
            params.append(platform_user_id)

        rows = conn.execute(
            f"""
            SELECT * FROM appointments
            WHERE status = 'confirmed'
              AND appointment_time >= %s
              AND ({ownership})
            ORDER BY appointment_time ASC
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def complete_appointment(appointment_id: int) -> tuple[bool, str, dict | None, dict | None]:
    """
    Marks appointment as completed (клиент пострижен / визит состоялся).
    If the client was invited by a referral, marks the referral visited and awards bonus.
    Synchronizes status across all platforms in the unified database.
    Returns (success, message, appointment_dict, referral_notify_data).
    """
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM appointments WHERE id = %s", (appointment_id,)).fetchone()
        if not row:
            return False, "Запись не найдена", None, None

        if row["status"] == "completed":
            return False, "Визит уже был отмечен как выполненный ранее", dict(row), None

        conn.execute(
            "UPDATE appointments SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (appointment_id,),
        )
        conn.commit()
        appt = dict(row)
        appt["status"] = "completed"

    # Check for pending referral for this client
    referral_notify_data = None
    try:
        from core_bot.services.referrals import mark_referral_visited, normalize_phone
        norm_phone = normalize_phone(appt.get("phone", ""))
        with get_connection() as conn:
            user_id = appt.get("user_id")
            ref_row = None
            if user_id:
                ref_row = conn.execute(
                    "SELECT id FROM referrals WHERE invited_user_id = %s AND status = 'started' LIMIT 1",
                    (user_id,),
                ).fetchone()
            if not ref_row and norm_phone:
                ref_row = conn.execute(
                    """
                    SELECT r.id FROM referrals r
                    JOIN users u ON u.id = r.invited_user_id
                    WHERE u.phone = %s AND r.status = 'started'
                    LIMIT 1
                    """,
                    (norm_phone,),
                ).fetchone()

            if ref_row:
                ref_ok, ref_msg, notify_data = mark_referral_visited(ref_row["id"])
                if ref_ok and notify_data:
                    referral_notify_data = notify_data
                    logger.info("Auto-credited referral bonus for completed appointment %s: %s", appointment_id, notify_data)
    except Exception as ref_err:
        logger.error("Error checking referral for completed appt %s: %s", appointment_id, ref_err)

    msg = "Визит успешно отмечен! Запись выполнена."
    if referral_notify_data:
        msg += f" Другу начислен бонус за рекомендацию: {referral_notify_data.get('amount', 300)} ₽!"

    return True, msg, appt, referral_notify_data


def reschedule_appointment(
    appointment_id: int,
    new_datetime: datetime.datetime,
    platform: str | None = None,
    platform_user_id: int | None = None,
) -> tuple[bool, str, dict | None]:
    """
    Moves an appointment to a new date/time in the shared «Срез» schedule.

    The new interval must fit the working hours and must not overlap another
    booking (taken into account service duration). The current appointment is
    excluded from the overlap check.
    """
    from core_bot.services.booking import (
        _time_to_minutes,
        find_overlapping_appointment,
        get_day_schedule,
        service_duration_minutes,
    )

    if new_datetime.tzinfo is None:
        new_datetime = new_datetime.replace(tzinfo=SARATOV_TZ)
    else:
        new_datetime = new_datetime.astimezone(SARATOV_TZ)

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM appointments WHERE id = %s", (appointment_id,)).fetchone()
        if not row:
            return False, "Запись не найдена", None
        appt = dict(row)
        if not _check_appointment_access(appt, platform, platform_user_id):
            return False, "У вас нет прав для изменения этой записи.", None

        duration = int(appt.get("duration_min") or service_duration_minutes(appt.get("services")))
        day_key = new_datetime.date().isoformat()
        sched = get_day_schedule(day_key)
        if not sched["is_working"]:
            return False, "В этот день мастер не работает.", None

        day_start = datetime.datetime.combine(
            new_datetime.date(), datetime.time.min, tzinfo=SARATOV_TZ
        ) + datetime.timedelta(minutes=_time_to_minutes(sched["start_time"]))
        day_end = datetime.datetime.combine(
            new_datetime.date(), datetime.time.min, tzinfo=SARATOV_TZ
        ) + datetime.timedelta(minutes=_time_to_minutes(sched["end_time"]))
        slot_end = new_datetime + datetime.timedelta(minutes=duration)
        if new_datetime < day_start or slot_end > day_end:
            return False, "Это время вне рабочих часов мастера.", None
        if new_datetime <= datetime.datetime.now(SARATOV_TZ):
            return False, "Это время уже прошло.", None

        if conn.is_pg:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"appointments:{day_key}",),
            )

        overlap = find_overlapping_appointment(
            conn, new_datetime, duration, exclude_appointment_id=appointment_id
        )
        if overlap:
            return False, "Это время уже занято. Пожалуйста, выберите другой слот.", None

        val_false = False if conn.is_pg else 0
        dt_val = new_datetime if conn.is_pg else new_datetime.strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            UPDATE appointments
            SET appointment_time = %s,
                status = 'confirmed',
                reminder_evening_sent = %s,
                reminder_morning_sent = %s,
                reminder_2h_sent = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (dt_val, val_false, val_false, val_false, appointment_id),
        )
        conn.commit()
        appt["appointment_time"] = new_datetime
        appt["status"] = "confirmed"

    return True, "Запись успешно перенесена!", appt


def update_appointment_price_and_service(
    appt_id: int,
    total_price: int,
    services: str | None = None,
) -> tuple[bool, str, dict | None]:
    with get_connection() as conn:
        appt = conn.execute("SELECT * FROM appointments WHERE id = %s", (appt_id,)).fetchone()
        if not appt:
            return False, "Запись не найдена.", None

        eff_service = services.strip() if services and services.strip() else (appt["services"] or "Стрижка")
        conn.execute(
            """
            UPDATE appointments
            SET total_price = %s, services = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (int(total_price), eff_service, appt_id),
        )
        conn.commit()
        updated_row = conn.execute("SELECT * FROM appointments WHERE id = %s", (appt_id,)).fetchone()
        return True, f"Стоимость записи #{appt_id} обновлена: {total_price} ₽ ({eff_service})", dict(updated_row) if updated_row else None


def get_appointments_counts() -> dict[str, int]:
    """
    Returns appointment counts for filter badges: today, tomorrow, pending, all.
    """
    now = get_current_saratov_time()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + datetime.timedelta(days=1)
    tomorrow_start = today_end
    tomorrow_end = tomorrow_start + datetime.timedelta(days=1)

    counts = {"today": 0, "tomorrow": 0, "pending": 0, "all": 0}

    with get_connection() as conn:
        t_start = today_start if conn.is_pg else today_start.strftime("%Y-%m-%d %H:%M:%S")
        t_end = today_end if conn.is_pg else today_end.strftime("%Y-%m-%d %H:%M:%S")
        tm_start = tomorrow_start if conn.is_pg else tomorrow_start.strftime("%Y-%m-%d %H:%M:%S")
        tm_end = tomorrow_end if conn.is_pg else tomorrow_end.strftime("%Y-%m-%d %H:%M:%S")
        val_false = False if conn.is_pg else 0

        # today
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM appointments
            WHERE (status IS NULL OR status != 'cancelled')
              AND appointment_time >= %s AND appointment_time < %s
            """,
            (t_start, t_end),
        ).fetchone()
        counts["today"] = row["c"] if row else 0

        # tomorrow
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM appointments
            WHERE (status IS NULL OR status != 'cancelled')
              AND appointment_time >= %s AND appointment_time < %s
            """,
            (tm_start, tm_end),
        ).fetchone()
        counts["tomorrow"] = row["c"] if row else 0

        # pending confirmation
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM appointments
            WHERE (status IS NULL OR status != 'cancelled')
              AND (client_confirmed IS NULL OR client_confirmed = %s)
              AND appointment_time >= %s
            """,
            (val_false, t_start),
        ).fetchone()
        counts["pending"] = row["c"] if row else 0

        # all upcoming
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM appointments
            WHERE (status IS NULL OR status != 'cancelled')
              AND appointment_time >= %s
            """,
            (t_start,),
        ).fetchone()
        counts["all"] = row["c"] if row else 0

    return counts


def get_master_appointments(
    date_filter: str | None = None,
    limit: int = 15,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """
    Fetches master appointments with flexible filtering ('today', 'tomorrow', 'pending', 'all', or YYYY-MM-DD).
    Returns (list_of_appointments, total_count).
    """
    now = get_current_saratov_time()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + datetime.timedelta(days=1)
    tomorrow_start = today_end
    tomorrow_end = tomorrow_start + datetime.timedelta(days=1)

    where_clauses = ["(status IS NULL OR status != 'cancelled')"]
    params: list[Any] = []

    if date_filter == "today":
        where_clauses.append("appointment_time >= %s AND appointment_time < %s")
        params.extend([today_start, today_end])
    elif date_filter == "tomorrow":
        where_clauses.append("appointment_time >= %s AND appointment_time < %s")
        params.extend([tomorrow_start, tomorrow_end])
    elif date_filter == "pending":
        where_clauses.append("(client_confirmed IS NULL OR client_confirmed = %s) AND appointment_time >= %s")
        params.extend([False, today_start])
    elif date_filter and date_filter not in ("all", "future"):
        try:
            d = datetime.date.fromisoformat(date_filter)
            dt_from = datetime.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=SARATOV_TZ)
            dt_to = dt_from + datetime.timedelta(days=1)
            where_clauses.append("appointment_time >= %s AND appointment_time < %s")
            params.extend([dt_from, dt_to])
        except Exception:
            where_clauses.append("appointment_time >= %s")
            params.append(today_start)
    else:
        # Default: all future from today
        where_clauses.append("appointment_time >= %s")
        params.append(today_start)

    where_sql = " AND ".join(where_clauses)

    with get_connection() as conn:
        exec_params = []
        for p in params:
            if isinstance(p, datetime.datetime) and not conn.is_pg:
                exec_params.append(p.strftime("%Y-%m-%d %H:%M:%S"))
            elif isinstance(p, bool) and not conn.is_pg:
                exec_params.append(1 if p else 0)
            else:
                exec_params.append(p)

        count_sql = f"SELECT COUNT(*) AS total FROM appointments WHERE {where_sql}"
        row = conn.execute(count_sql, tuple(exec_params)).fetchone()
        total = row["total"] if row else 0

        list_sql = f"""
            SELECT * FROM appointments
            WHERE {where_sql}
            ORDER BY appointment_time ASC
            LIMIT %s OFFSET %s
        """
        list_params = tuple(exec_params + [limit, offset])
        rows = conn.execute(list_sql, list_params).fetchall()
        return [dict(r) for r in rows], total


def create_manual_appointment(
    phone: str,
    appointment_time: datetime.datetime,
    services: str,
    total_price: int,
    client_name: str | None = None,
    platform: str = "telegram",
    duration_min: int | None = None,
) -> tuple[bool, str, int | None]:
    """
    Creates an appointment directly by the master, with custom time/price.
    Saves immediately to the shared «Срез» database and rejects overlaps.
    """
    if appointment_time.tzinfo is None:
        appointment_time = appointment_time.replace(tzinfo=SARATOV_TZ)
    else:
        appointment_time = appointment_time.astimezone(SARATOV_TZ)

    try:
        appt_id = create_or_update_appointment(
            phone=phone,
            appointment_time=appointment_time,
            services=services,
            total_price=total_price,
            client_name=client_name or "Клиент",
            platform=platform,
            duration_min=duration_min,
        )
    except SlotUnavailableError as exc:
        return False, str(exc), None
    return True, "✅ Запись успешно создана в единой базе «Срез»!", appt_id


def get_master_day_schedule(day_key: str) -> dict:
    """Returns the effective schedule for one date from the shared service."""
    from core_bot.services.booking import get_day_schedule

    return get_day_schedule(day_key)


def get_day_time_slots_status(day_key: str) -> dict[str, Any]:
    """Admin day view backed by the same availability rules as client booking."""
    from core_bot.services.booking import get_day_time_slots_status as _status

    return _status(day_key)


def set_master_day_schedule(
    day_key: str,
    is_working: bool,
    start_time: str = "10:00",
    end_time: str = "20:00",
    note: str | None = None,
) -> bool:
    with get_connection() as conn:
        val_work = is_working if conn.is_pg else (1 if is_working else 0)
        existing = conn.execute("SELECT id FROM master_schedule WHERE day_key = %s", (day_key,)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE master_schedule
                SET is_working = %s, start_time = %s, end_time = %s, note = %s, updated_at = CURRENT_TIMESTAMP
                WHERE day_key = %s
                """,
                (val_work, start_time, end_time, note or "", day_key),
            )
        else:
            conn.execute(
                """
                INSERT INTO master_schedule (day_key, is_working, start_time, end_time, note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (day_key, val_work, start_time, end_time, note or ""),
            )
        conn.commit()
        return True


def adjust_master_day_hours(
    day_key: str,
    start_delta_hours: int = 0,
    end_delta_hours: int = 0,
) -> dict:
    sched = get_master_day_schedule(day_key)
    start_h, start_m = [int(p) for p in sched["start_time"].split(":")[:2]]
    end_h, end_m = [int(p) for p in sched["end_time"].split(":")[:2]]

    new_start_h = max(0, min(23, start_h + start_delta_hours))
    new_end_h = max(new_start_h + 1, min(24, end_h + end_delta_hours))

    new_start = f"{new_start_h:02d}:{start_m:02d}"
    new_end = f"{new_end_h:02d}:{end_m:02d}" if new_end_h < 24 else "23:59"

    set_master_day_schedule(day_key, sched["is_working"], new_start, new_end)
    return get_master_day_schedule(day_key)


def get_upcoming_days_schedule(days_ahead: int = 14) -> list[dict]:
    now = get_current_saratov_time()
    ru_weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    months_ru = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

    result = []
    for i in range(days_ahead):
        d = now + datetime.timedelta(days=i)
        day_key = d.strftime("%Y-%m-%d")
        sched = get_master_day_schedule(day_key)
        wd = ru_weekdays[d.weekday()]
        title = f"{wd}, {d.day} {months_ru[d.month]}"
        if i == 0:
            title += " (Сегодня)"
        elif i == 1:
            title += " (Завтра)"
        sched["title"] = title
        result.append(sched)
    return result


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
              AND reminder_evening_sent = FALSE
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


def get_due_confirmation_followup_reminders(platform: str | None = None) -> list[dict]:
    """
    Find appointments scheduled for today where evening reminder was sent,
    but client has not confirmed yet, morning follow-up has not been sent,
    it is morning (>= 09:00 Saratov time), and visit is at least 2 hours away.
    """
    now = get_current_saratov_time()
    if now.hour < 9:
        return []

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + datetime.timedelta(days=1)
    min_time = now + datetime.timedelta(hours=2)

    with get_connection() as conn:
        query = """
            SELECT * FROM appointments
            WHERE status = 'confirmed'
              AND reminder_evening_sent = TRUE
              AND reminder_morning_sent = FALSE
              AND client_confirmed = FALSE
              AND appointment_time >= %s AND appointment_time < %s
              AND appointment_time > %s
        """
        t_start = today_start if conn.is_pg else today_start.strftime("%Y-%m-%d %H:%M:%S")
        t_end = today_end if conn.is_pg else today_end.strftime("%Y-%m-%d %H:%M:%S")
        t_min = min_time if conn.is_pg else min_time.strftime("%Y-%m-%d %H:%M:%S")
        params = [t_start, t_end, t_min]
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
              AND reminder_2h_sent = FALSE
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
            WHERE status = 'completed'
              AND retention_18d_sent = FALSE
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
        "morning": "reminder_morning_sent",
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
