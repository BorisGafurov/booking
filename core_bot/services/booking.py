"""
Booking service for «Срез»: services catalog, shared schedule and slot availability.

The bots and srezforma.ru use the same PostgreSQL tables, so this module is the
single Python source of truth for:
  * the service catalog and prices;
  * the standard Tue/Sat slot grid;
  * per-date overrides from ``master_schedule``;
  * overlap-aware availability (a 135-minute combo blocks the next slot too).

No third-party booking service is involved.
"""

import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo

from core_bot.database import get_connection
from core_bot.services.referrals import is_master_phone, normalize_phone

logger = logging.getLogger(__name__)

SARATOV_TZ = ZoneInfo("Europe/Saratov")

SERVICES: dict[str, dict[str, Any]] = {
    "hair": {
        "key": "hair",
        "id": "hair",
        "name": "Мужская стрижка",
        "price": 2300,
        "duration_min": 75,
        "duration_text": "1 ч 15 мин",
    },
    "beard": {
        "key": "beard",
        "id": "beard",
        "name": "Оформление бороды",
        "price": 700,
        "duration_min": 60,
        "duration_text": "1 ч",
    },
    "combo": {
        "key": "combo",
        "id": "combo",
        "name": "Стрижка + Борода",
        "price": 3000,
        "duration_min": 135,
        "duration_text": "2 ч 15 мин",
    },
}

DEFAULT_SERVICE_KEYS = ["hair"]

# Standard slot grid: Tue/Sat, 10:00-19:00, 75-minute steps.
DEFAULT_SLOTS = ["10:00", "11:15", "12:30", "14:00", "15:15", "16:30", "17:45"]
WORKING_WEEKDAYS = {1, 5}  # Tuesday=1, Saturday=5
DEFAULT_START_TIME = "10:00"
DEFAULT_END_TIME = "19:00"

# In-memory UI selections. They are per-process and intentionally ephemeral.
user_selected_services: dict[int, set[str]] = {}
user_selected_day: dict[int, str] = {}

RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
RU_MONTHS = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _parse_dt(val: Any) -> datetime.datetime:
    """Normalizes a datetime/ISO string to Europe/Saratov."""
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=SARATOV_TZ)
        return val.astimezone(SARATOV_TZ)
    if isinstance(val, str):
        text = val.strip()
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=SARATOV_TZ)
            return parsed.astimezone(SARATOV_TZ)
        except ValueError:
            pass
    return datetime.datetime.now(SARATOV_TZ)


def _time_to_minutes(value: str) -> int:
    try:
        hours, minutes = [int(part) for part in str(value).split(":")[:2]]
        return hours * 60 + minutes
    except (TypeError, ValueError):
        return 0


def _day_bounds(day_key: str, start_time: str, end_time: str) -> tuple[datetime.datetime, datetime.datetime]:
    day = datetime.date.fromisoformat(day_key)
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=SARATOV_TZ) + datetime.timedelta(
        minutes=_time_to_minutes(start_time)
    )
    end = datetime.datetime.combine(day, datetime.time.min, tzinfo=SARATOV_TZ) + datetime.timedelta(
        minutes=_time_to_minutes(end_time)
    )
    return start, end


def service_duration_minutes(services: str | None) -> int:
    """Derives duration from the service text stored in appointments.services."""
    text = (services or "").lower()
    has_hair = "стрижк" in text
    has_beard = "бород" in text
    if "combo" in text or (has_hair and has_beard):
        return 135
    if has_beard:
        return 60
    return 75


def map_service_name_to_keys(services: str | None) -> list[str]:
    """Maps a free-form service name to catalog keys."""
    text = (services or "").lower()
    has_hair = "стрижк" in text
    has_beard = "бород" in text
    if "combo" in text or (has_hair and has_beard):
        return ["combo"]
    if has_beard:
        return ["beard"]
    if has_hair:
        return ["hair"]
    return list(DEFAULT_SERVICE_KEYS)


def calculate_services(selected_keys: list[str] | set[str] | None) -> dict[str, Any]:
    if not selected_keys:
        keys = list(DEFAULT_SERVICE_KEYS)
    else:
        keys = [key for key in selected_keys if key in SERVICES]
        if not keys:
            keys = list(DEFAULT_SERVICE_KEYS)

    if "combo" in keys:
        keys = ["combo"]
    elif set(keys) == {"hair", "beard"}:
        # Both services together are the same package as "combo".
        keys = ["hair", "beard"]

    total_price = sum(SERVICES[key]["price"] for key in keys)
    total_min = sum(SERVICES[key]["duration_min"] for key in keys)

    if total_min >= 120:
        duration_text = f"{total_min // 60} ч {total_min % 60} мин".replace(" 0 мин", "")
    elif total_min >= 60:
        duration_text = f"{total_min // 60} ч" + (f" {total_min % 60} мин" if total_min % 60 else "")
    else:
        duration_text = f"{total_min} мин"

    return {
        "keys": keys,
        "names": [SERVICES[key]["name"] for key in keys],
        "service_ids": [SERVICES[key]["id"] for key in keys],
        "total_price": total_price,
        "duration_minutes": total_min,
        "duration_text": duration_text,
    }


def get_service_options_for_creation() -> list[dict[str, Any]]:
    """Options shown in the admin "create appointment" flow."""
    return [
        {"key": "hair", "name": "Мужская стрижка", "price": 2300, "duration_text": "1 ч 15 мин"},
        {"key": "beard", "name": "Оформление бороды", "price": 700, "duration_text": "1 ч"},
        {"key": "combo", "name": "Стрижка + Борода", "price": 3000, "duration_text": "2 ч 15 мин"},
    ]


def _schedule_overrides(conn, start_day: str, end_day: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT day_key, is_working, start_time, end_time
        FROM master_schedule
        WHERE day_key >= %s AND day_key <= %s
        """,
        (start_day, end_day),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        result[str(item["day_key"])] = {
            "is_working": bool(item["is_working"]),
            "start_time": item.get("start_time") or DEFAULT_START_TIME,
            "end_time": item.get("end_time") or DEFAULT_END_TIME,
        }
    return result


def get_day_schedule(day_key: str) -> dict[str, Any]:
    """Returns the effective schedule for one date (override or default)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT day_key, is_working, start_time, end_time, note FROM master_schedule WHERE day_key = %s",
            (day_key,),
        ).fetchone()
        if row:
            item = dict(row)
            return {
                "day_key": day_key,
                "is_working": bool(item["is_working"]),
                "start_time": item.get("start_time") or DEFAULT_START_TIME,
                "end_time": item.get("end_time") or DEFAULT_END_TIME,
                "note": item.get("note"),
            }

    try:
        weekday = datetime.date.fromisoformat(day_key).weekday()
    except ValueError:
        weekday = -1
    is_working = weekday in WORKING_WEEKDAYS
    return {
        "day_key": day_key,
        "is_working": is_working,
        "start_time": DEFAULT_START_TIME,
        "end_time": DEFAULT_END_TIME,
        "note": None if is_working else "Выходной день",
    }


def _appointments_between(conn, start: datetime.datetime, end: datetime.datetime) -> list[dict[str, Any]]:
    p_start = start if conn.is_pg else start.strftime("%Y-%m-%d %H:%M:%S")
    p_end = end if conn.is_pg else end.strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """
        SELECT id, appointment_time, duration_min, status,
               client_name, phone, services, total_price
        FROM appointments
        WHERE appointment_time >= %s AND appointment_time <= %s
          AND (status IS NULL OR status != 'cancelled')
        """,
        (p_start, p_end),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["appointment_time"] = _parse_dt(item["appointment_time"])
        result.append(item)
    return result


def _overlaps(
    slot_start: datetime.datetime,
    slot_end: datetime.datetime,
    appointments: list[dict[str, Any]],
    exclude_appointment_id: int | None = None,
) -> dict[str, Any] | None:
    for appt in appointments:
        if exclude_appointment_id and int(appt["id"]) == int(exclude_appointment_id):
            continue
        appt_start = appt["appointment_time"]
        duration = int(appt.get("duration_min") or service_duration_minutes(None))
        appt_end = appt_start + datetime.timedelta(minutes=duration)
        if appt_start < slot_end and slot_start < appt_end:
            return appt
    return None


def find_overlapping_appointment(
    conn,
    slot_start: datetime.datetime,
    duration_min: int,
    exclude_appointment_id: int | None = None,
) -> dict[str, Any] | None:
    """Finds an overlapping appointment using an existing DB connection."""
    day_start = slot_start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + datetime.timedelta(days=1)
    appointments = _appointments_between(conn, day_start, day_end)
    slot_end = slot_start + datetime.timedelta(minutes=duration_min)
    return _overlaps(slot_start, slot_end, appointments, exclude_appointment_id)


def get_days_schedule(
    selected_keys: list[str] | set[str] | None = None,
    days_count: int = 14,
) -> list[dict[str, Any]]:
    """
    Returns the next ``days_count`` days with every standard slot and its
    availability. Day-offs and fully booked days are included with
    ``available=False`` so the website calendar can render them.
    """
    now_saratov = datetime.datetime.now(SARATOV_TZ)
    calc = calculate_services(selected_keys)
    duration = calc["duration_minutes"]

    start_date = now_saratov.date()
    end_date = start_date + datetime.timedelta(days=days_count - 1)
    range_start = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=SARATOV_TZ)
    range_end = datetime.datetime.combine(end_date, datetime.time.max, tzinfo=SARATOV_TZ)

    try:
        with get_connection() as conn:
            overrides = _schedule_overrides(conn, start_date.isoformat(), end_date.isoformat())
            appointments = _appointments_between(conn, range_start, range_end)
    except Exception as exc:
        logger.warning("Could not load the shared schedule from DB: %s", exc)
        overrides = {}
        appointments = []

    days: list[dict[str, Any]] = []
    for offset in range(days_count):
        cur_date = start_date + datetime.timedelta(days=offset)
        day_key = cur_date.isoformat()

        if day_key in overrides:
            is_working = overrides[day_key]["is_working"]
            start_time = overrides[day_key]["start_time"]
            end_time = overrides[day_key]["end_time"]
        else:
            is_working = cur_date.weekday() in WORKING_WEEKDAYS
            start_time = DEFAULT_START_TIME
            end_time = DEFAULT_END_TIME

        slots: list[dict[str, Any]] = []
        if is_working:
            day_start, day_end = _day_bounds(day_key, start_time, end_time)
            for time_str in DEFAULT_SLOTS:
                slot_start = datetime.datetime.combine(
                    cur_date,
                    datetime.time(hour=int(time_str[:2]), minute=int(time_str[3:])),
                    tzinfo=SARATOV_TZ,
                )
                slot_end = slot_start + datetime.timedelta(minutes=duration)
                if slot_start < day_start or slot_end > day_end:
                    continue
                is_available = slot_start > now_saratov and not _overlaps(
                    slot_start, slot_end, appointments
                )
                slots.append({"time": time_str, "available": is_available})

        days.append(
            {
                "day_key": day_key,
                "weekday": cur_date.weekday(),
                "is_working": is_working,
                "start_time": start_time,
                "end_time": end_time,
                "slots": slots,
            }
        )
    return days


def get_schedule(selected_keys: list[str] | set[str] | None = None) -> dict[str, Any]:
    """
    Returns the next 14 days that have at least one free slot for the selected
    services (bot keyboard view).
    """
    days_map: dict[str, Any] = {}
    for day in get_days_schedule(selected_keys):
        if not day["is_working"]:
            continue
        free_slots = [slot for slot in day["slots"] if slot["available"]]
        if not free_slots:
            continue

        cur_date = datetime.date.fromisoformat(day["day_key"])
        title = f"{RU_WEEKDAYS[cur_date.weekday()]}, {cur_date.day} {RU_MONTHS[cur_date.month]}"
        days_map[day["day_key"]] = {
            "day_key": day["day_key"],
            "title": title,
            "slots": [
                {
                    "timestamp": int(
                        datetime.datetime.combine(
                            cur_date,
                            datetime.time(hour=int(slot["time"][:2]), minute=int(slot["time"][3:])),
                            tzinfo=SARATOV_TZ,
                        ).timestamp()
                    ),
                    "time": slot["time"],
                }
                for slot in free_slots
            ],
        }
    return days_map


def is_slot_available(
    appointment_time: datetime.datetime,
    duration_min: int | None = None,
    exclude_appointment_id: int | None = None,
) -> tuple[bool, str]:
    """Checks the shared schedule and overlapping appointments before writing."""
    when = _parse_dt(appointment_time)
    day_key = when.date().isoformat()
    sched = get_day_schedule(day_key)

    if not sched["is_working"]:
        return False, "В этот день мастер не работает."

    day_start, day_end = _day_bounds(day_key, sched["start_time"], sched["end_time"])
    slot_end = when + datetime.timedelta(minutes=int(duration_min or 75))

    if when < day_start or slot_end > day_end:
        return False, "Это время вне рабочих часов мастера."
    if when <= datetime.datetime.now(SARATOV_TZ):
        return False, "Это время уже прошло."

    try:
        with get_connection() as conn:
            appointments = _appointments_between(conn, day_start, day_end)
    except Exception as exc:
        logger.warning("Could not check slot availability in DB: %s", exc)
        return False, "Не удалось проверить занятость слота. Попробуйте ещё раз."

    if _overlaps(when, slot_end, appointments, exclude_appointment_id):
        return False, "Это время уже занято. Пожалуйста, выберите другой слот."

    return True, "Слот свободен."


def get_day_time_slots_status(day_key: str) -> dict[str, Any]:
    """
    Admin day view: every standard slot with busy/free status.

    Uses the same overlap rules as client booking, so a 135-minute combo marks
    all overlapping 75-minute starts as busy.
    """
    sched = get_day_schedule(day_key)
    day_start, day_end = _day_bounds(day_key, sched["start_time"], sched["end_time"])

    try:
        with get_connection() as conn:
            appointments = _appointments_between(conn, day_start, day_end)
    except Exception as exc:
        logger.warning("Could not load day appointments: %s", exc)
        appointments = []

    busy_appts = []
    for appt in appointments:
        appt_dt = appt["appointment_time"]
        busy_appts.append(
            {
                "id": appt["id"],
                "time": appt_dt.strftime("%H:%M"),
                "client_name": appt.get("client_name") or "Клиент",
                "services": appt.get("services") or "Стрижка",
                "phone": appt.get("phone") or "",
                "price": appt.get("total_price") or 0,
            }
        )

    all_slots = []
    free_slots = []
    for time_str in DEFAULT_SLOTS:
        slot_start = datetime.datetime.combine(
            datetime.date.fromisoformat(day_key),
            datetime.time(hour=int(time_str[:2]), minute=int(time_str[3:])),
            tzinfo=SARATOV_TZ,
        )
        slot_end = slot_start + datetime.timedelta(minutes=75)
        is_inside = slot_start >= day_start and slot_end <= day_end
        busy = _overlaps(slot_start, slot_end, appointments) is not None

        if sched["is_working"] and is_inside and not busy:
            free_slots.append(time_str)
            all_slots.append({"time": time_str, "is_busy": False, "badge": "🟢"})
        else:
            all_slots.append({"time": time_str, "is_busy": True, "badge": "🔴"})

    return {
        "free_slots": free_slots,
        "busy_appts": busy_appts,
        "all_slots": all_slots,
        "sched": sched,
    }
