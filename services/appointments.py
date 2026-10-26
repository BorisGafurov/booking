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
                "SELECT id, client_name, phone FROM appointments WHERE masters_id = %s", (masters_id,)
            ).fetchone()
            if existing:
                eff_name = client_name or ""
                if eff_name in ("Клиент (Masters)", "Клиент", "") and existing["client_name"] and existing["client_name"] not in ("Клиент (Masters)", "Клиент"):
                    eff_name = existing["client_name"]
                eff_phone = norm_phone
                if norm_phone == "+79173225155" and existing["phone"] and existing["phone"] != "+79173225155":
                    eff_phone = existing["phone"]

                conn.execute(
                    """
                    UPDATE appointments
                    SET user_id = COALESCE(%s, user_id),
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
                        eff_name,
                        eff_phone,
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

    try:
        from services.masters_sync import cancel_masters_appointment
        sync_ok, sync_msg = cancel_masters_appointment(res)
        logger.info("Masters cancel sync for appt %s: %s (%s)", appointment_id, sync_ok, sync_msg)
    except Exception as e:
        logger.error("Error executing Masters cancel sync for appt %s: %s", appointment_id, e)

    return True, "Запись успешно отменена", res


def confirm_appointment_by_client(appointment_id: int) -> tuple[bool, str, dict | None]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = %s", (appointment_id,)
        ).fetchone()
        if not row:
            return False, "Запись не найдена", None
        if row["status"] == "cancelled":
            return False, "Запись уже отменена", dict(row)

        val = True if conn.is_pg else 1
        conn.execute(
            "UPDATE appointments SET client_confirmed = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (val, appointment_id),
        )
        conn.commit()
        res = dict(row)
        res["client_confirmed"] = True

    try:
        from services.masters_sync import confirm_masters_appointment
        sync_ok, sync_msg = confirm_masters_appointment(res)
        logger.info("Masters confirm sync for appt %s: %s (%s)", appointment_id, sync_ok, sync_msg)
    except Exception as e:
        logger.error("Error executing Masters confirm sync for appt %s: %s", appointment_id, e)

    return True, "Запись успешно подтверждена", res


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


def complete_appointment(appointment_id: int) -> tuple[bool, str, dict | None, dict | None]:
    """
    Marks appointment as completed (клиент пострижен / визит состоялся).
    If the client was invited by a referral, marks the referral visited and awards bonus.
    Synchronizes status with Masters CRM.
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
        from services.referrals import mark_referral_visited, normalize_phone
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

    # Sync to Masters CRM
    try:
        from services.masters_sync import complete_masters_appointment
        sync_ok, sync_msg = complete_masters_appointment(appt)
        logger.info("Masters complete sync for appt %s: %s (%s)", appointment_id, sync_ok, sync_msg)
    except Exception as e:
        logger.error("Error executing Masters complete sync for appt %s: %s", appointment_id, e)

    msg = "Визит успешно отмечен! Запись выполнена."
    if referral_notify_data:
        msg += f" Другу начислен бонус за рекомендацию: {referral_notify_data.get('amount', 300)} ₽!"

    return True, msg, appt, referral_notify_data


def reschedule_appointment(
    appointment_id: int,
    new_datetime: datetime.datetime,
) -> tuple[bool, str, dict | None]:
    """
    Reschedules appointment to a new date and time and resets reminder flags.
    Synchronizes with Masters CRM.
    """
    if new_datetime.tzinfo is None:
        new_datetime = new_datetime.replace(tzinfo=SARATOV_TZ)

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM appointments WHERE id = %s", (appointment_id,)).fetchone()
        if not row:
            return False, "Запись не найдена", None

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
        appt = dict(row)
        appt["appointment_time"] = new_datetime
        appt["status"] = "confirmed"

    # Sync to Masters CRM
    try:
        from services.masters_sync import reschedule_masters_appointment
        sync_ok, sync_msg = reschedule_masters_appointment(appt, new_datetime)
        logger.info("Masters reschedule sync for appt %s: %s (%s)", appointment_id, sync_ok, sync_msg)
    except Exception as e:
        logger.error("Error executing Masters reschedule sync for appt %s: %s", appointment_id, e)

    return True, "Запись успешно перенесена!", appt


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

        if counts["all"] == 0:
            try:
                from services.masters_sync import sync_masters_appointments
                sync_masters_appointments()
                r_tm = conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM appointments
                    WHERE (status IS NULL OR status != 'cancelled')
                      AND appointment_time >= %s AND appointment_time < %s
                    """,
                    (tm_start, tm_end),
                ).fetchone()
                counts["tomorrow"] = r_tm["c"] if r_tm else 0

                r_all = conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM appointments
                    WHERE (status IS NULL OR status != 'cancelled')
                      AND appointment_time >= %s
                    """,
                    (t_start,),
                ).fetchone()
                counts["all"] = r_all["c"] if r_all else 0
            except Exception as e:
                logger.debug("Auto-sync inside get_appointments_counts: %s", e)

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

        if total == 0 and date_filter in ("tomorrow", "all"):
            try:
                from services.masters_sync import sync_masters_appointments
                sync_masters_appointments()
                row = conn.execute(count_sql, tuple(exec_params)).fetchone()
                total = row["total"] if row else 0
            except Exception as e:
                logger.debug("Auto-sync inside get_master_appointments: %s", e)

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
) -> tuple[bool, str, int | None]:
    """
    Creates an appointment directly by master, with support for any custom time/price.
    Syncs to Masters CRM if session active.
    """
    if appointment_time.tzinfo is None:
        appointment_time = appointment_time.replace(tzinfo=SARATOV_TZ)

    appt_id = create_or_update_appointment(
        phone=phone,
        appointment_time=appointment_time,
        services=services,
        total_price=total_price,
        client_name=client_name or "Клиент",
        platform=platform,
    )

    # Sync to Masters CRM
    try:
        from services.masters_sync import create_masters_appointment
        create_masters_appointment(
            client_name=client_name or "Клиент",
            phone=phone,
            services=services,
            appointment_time=appointment_time,
            price=total_price,
        )
    except Exception as e:
        logger.debug("Masters create sync notice: %s", e)

    return True, "Запись успешно создана!", appt_id


def get_master_day_schedule(day_key: str) -> dict:
    """
    Returns schedule for day_key (YYYY-MM-DD).
    1. Checks DB master_schedule table for master overrides.
    2. Checks Masters CRM live schedule (via services.masters.get_schedule).
    3. Falls back to Boris Gafurov's standard schedule:
       Tuesdays (1) and Saturdays (5) are working days (10:00 - 19:00).
       All other days (Mon, Wed, Thu, Fri, Sun) are non-working days / days off.
    """
    # 1. Custom DB override
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM master_schedule WHERE day_key = %s", (day_key,)).fetchone()
        if row:
            r = dict(row)
            return {
                "day_key": day_key,
                "is_working": bool(r["is_working"]),
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "note": r.get("note"),
            }

    # 2. Check Masters CRM schedule
    try:
        from services.masters import get_schedule
        masters_map = get_schedule(["hair"])
        if masters_map and day_key in masters_map:
            slots = masters_map[day_key].get("slots", [])
            start_time = "10:00"
            end_time = "19:00"
            if slots:
                first_s = slots[0].get("time", "10:00")
                start_time = min("10:00", first_s)
                last_s = slots[-1].get("time", "17:30")
                try:
                    lh, lm = [int(p) for p in last_s.split(":")[:2]]
                    end_dt = datetime.datetime(2000, 1, 1, lh, lm) + datetime.timedelta(minutes=90)
                    end_time = end_dt.strftime("%H:%M")
                except Exception:
                    end_time = "19:00"
            return {
                "day_key": day_key,
                "is_working": True,
                "start_time": start_time,
                "end_time": end_time,
                "note": None,
            }
        elif masters_map:
            # Masters schedule was successfully fetched and this day has no slots -> day off in Masters
            return {
                "day_key": day_key,
                "is_working": False,
                "start_time": "10:00",
                "end_time": "19:00",
                "note": "Выходной в Masters",
            }
    except Exception as e:
        logger.debug("Masters schedule check skipped for %s: %s", day_key, e)

    # 3. Standard fallback: Tuesdays (1) and Saturdays (5) are working (10:00 - 19:00)
    try:
        d = datetime.date.fromisoformat(day_key)
        is_working_day = d.weekday() in (1, 5)
    except Exception:
        is_working_day = False

    return {
        "day_key": day_key,
        "is_working": is_working_day,
        "start_time": "10:00",
        "end_time": "19:00",
        "note": None if is_working_day else "Выходной день",
    }


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
            WHERE status = 'confirmed'
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
