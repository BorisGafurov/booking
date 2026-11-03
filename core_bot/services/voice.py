import logging
import os
import urllib.parse
import urllib.request
import json

logger = logging.getLogger(__name__)

ZVONOK_API_URL = "https://zvonok.com/manager/cabapi_external/api/v1/phones/call/"
DEFAULT_PUBLIC_KEY = "07cd1e782fa3c265d1ce53c0dedd5036"
DEFAULT_CAMPAIGN_ID = "1852297522"


def is_voice_configured() -> bool:
    """Checks whether Zvonok.com credentials are set up in the environment or defaults."""
    public_key = os.getenv("ZVONOK_PUBLIC_KEY", DEFAULT_PUBLIC_KEY).strip()
    campaign_id = os.getenv("ZVONOK_CAMPAIGN_ID", DEFAULT_CAMPAIGN_ID).strip()
    return bool(public_key and campaign_id)


def send_voice_reminder(appointment: dict) -> bool:
    """
    Triggers an interactive voice robocall via Zvonok.com.
    Returns True if the call was successfully queued, False otherwise.
    Never raises exceptions, ensuring safe fallback.
    """
    public_key = os.getenv("ZVONOK_PUBLIC_KEY", DEFAULT_PUBLIC_KEY).strip()
    campaign_id = os.getenv("ZVONOK_CAMPAIGN_ID", DEFAULT_CAMPAIGN_ID).strip()
    enabled = os.getenv("VOICE_REMINDERS_ENABLED", "true").lower() in ("1", "true", "yes")

    if not enabled:
        logger.debug("Голосовые напоминания отключены настройкой VOICE_REMINDERS_ENABLED")
        return False

    if not public_key or not campaign_id:
        logger.debug("Zvonok не настроен (ZVONOK_PUBLIC_KEY или ZVONOK_CAMPAIGN_ID не заданы)")
        return False

    raw_phone = appointment.get("phone") or ""
    digits = "".join(ch for ch in str(raw_phone) if ch.isdigit())
    if len(digits) == 10:
        phone = "+7" + digits
    elif len(digits) == 11 and digits.startswith(("7", "8")):
        phone = "+7" + digits[1:]
    else:
        logger.warning("Некорректный номер для звонка Zvonok: %s", raw_phone)
        return False

    client_name = appointment.get("client_name") or "Клиент"
    time_str = ""
    day_word = "сегодня"
    dt = appointment.get("appointment_time")
    if dt:
        try:
            import datetime
            from zoneinfo import ZoneInfo
            saratov_tz = ZoneInfo("Europe/Saratov")
            now_local = datetime.datetime.now(saratov_tz).date()
            if isinstance(dt, str):
                dt = datetime.datetime.fromisoformat(dt)
            dt_local = dt.astimezone(saratov_tz) if getattr(dt, "tzinfo", None) else dt
            time_str = dt_local.strftime("%H:%M")
            if hasattr(dt_local, "date"):
                appt_date = dt_local.date()
                if appt_date == now_local:
                    day_word = "сегодня"
                elif appt_date == now_local + datetime.timedelta(days=1):
                    day_word = "завтра"
                else:
                    day_word = dt_local.strftime("%d.%m")
        except Exception:
            time_str = ""
            day_word = "сегодня"

    # Synthesized text for dynamic campaigns (with Zvonok stress markup)
    if time_str:
        text = (
            f"Здравствуйте, {client_name}! Вас приветствует голосовой ассистент Срез. "
            f"Напоминаем о вашей записи к мастеру Борису Гаф+урову {day_word} в {time_str}. "
            f"Пожалуйста, подтвердите визит. Нажмите один, если придёте. Нажмите два, если хотите отменить запись."
        )
    else:
        text = (
            f"Здравствуйте, {client_name}! Вас приветствует голосовой ассистент Срез. "
            f"Напоминаем о вашей записи к мастеру Борису Гаф+урову. "
            f"Пожалуйста, подтвердите визит. Нажмите один, если придёте. Нажмите два, если хотите отменить запись."
        )

    payload = {
        "public_key": public_key,
        "phone": phone,
        "campaign_id": campaign_id,
        "text": text,
    }

    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            ZVONOK_API_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            logger.info("Zvonok звонок успешно поставлен в очередь для %s: %s", phone, resp_body[:200])
            return True
    except Exception as err:
        logger.warning("Ошибка вызова API Zvonok для %s: %s", phone, err)
        return False
