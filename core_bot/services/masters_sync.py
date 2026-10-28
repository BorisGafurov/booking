import datetime
import http.cookiejar
import json
import logging
import os
import re
import ssl
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo

from core_bot.database import get_connection
from core_bot.services.appointments import create_or_update_appointment
from core_bot.services.referrals import is_master_phone, normalize_phone

logger = logging.getLogger(__name__)

MASTERS_LOGIN_URL = "https://prod.mastersapp.ru/account/auth/login"
SARATOV_TZ = ZoneInfo("Europe/Saratov")
DEFAULT_MASTER_PHONE = os.getenv("MASTER_PHONE", "")

# In-memory storage for pending master login flows: admin_id -> login_state
pending_master_logins: dict[int, dict[str, Any]] = {}


def get_suggested_master_phone() -> str:
    """Return default or last used master phone for Masters CRM authentication."""
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "SELECT master_phone FROM masters_session WHERE master_phone IS NOT NULL AND master_phone != '' ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                phone = row["master_phone"] if isinstance(row, dict) else row[0]
                if phone:
                    return str(phone).strip()
    except Exception:
        pass
    return DEFAULT_MASTER_PHONE


def _get_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception as exc:
        if os.getenv("MASTERS_ALLOW_INSECURE_SSL", "").lower() in ("1", "true", "yes"):
            logger.warning("Could not create verified SSL context, using unverified context (allowed by env)")
            return ssl._create_unverified_context()
        raise RuntimeError("Cannot create a verified SSL context for Masters") from exc


def format_master_phone(raw_phone: str) -> str:
    """Format phone as (###) ###-##-## for prod.mastersapp.ru"""
    digits = re.sub(r"\D", "", raw_phone)
    if (digits.startswith("7") or digits.startswith("8")) and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:10]}"
    return raw_phone


def request_master_login(raw_phone: str) -> tuple[bool, str, dict[str, Any]]:
    """
    Step 1: Send master phone to prod.mastersapp.ru/account/auth/login to trigger SMS.
    Returns (success, message, session_data).
    """
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=_get_ssl_context()),
    )

    # 1. GET login page to get initial _csrf and PHPSESSID
    try:
        req_get = urllib.request.Request(
            MASTERS_LOGIN_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with opener.open(req_get, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.error("Error opening Masters login page: %s", e)
        return False, f"Ошибка соединения с Masters: {e}", {}

    csrf_match = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    csrf = csrf_match.group(1) if csrf_match else ""
    if not csrf:
        return False, "Не удалось получить CSRF токен Masters", {}

    phone_formatted = format_master_phone(raw_phone)

    # 2. POST phone
    post_params = {
        "_csrf": csrf,
        "type": "master",
        "code": "RU",
        "phone": phone_formatted,
    }
    data = urllib.parse.urlencode(post_params).encode("utf-8")

    try:
        req_post = urllib.request.Request(
            MASTERS_LOGIN_URL,
            data=data,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": MASTERS_LOGIN_URL,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with opener.open(req_post, timeout=15) as resp:
            post_html = resp.read().decode("utf-8", errors="ignore")
            final_url = resp.geturl()
    except Exception as e:
        logger.error("Error sending phone to Masters: %s", e)
        return False, f"Ошибка отправки номера в Masters: {e}", {}

    if "auth/code" in final_url or "/code" in final_url:
        cookie_dict = {c.name: c.value for c in cj}
        new_csrf_match = re.search(r'name="_csrf"\s+value="([^"]+)"', post_html)
        new_csrf = new_csrf_match.group(1) if new_csrf_match else csrf

        norm_phone = normalize_phone(raw_phone) or raw_phone
        session_data = {
            "phone": norm_phone,
            "phone_formatted": phone_formatted,
            "csrf": new_csrf,
            "cookies": cookie_dict,
            "verify_url": final_url,
        }
        return True, "СМС-код отправлен на указанный номер.", session_data

    # If stayed on login page, check for errors
    error_match = re.search(r'class="wrap-phone\s+error"[^>]*>.*?<div class="error-desc"[^>]*>(.*?)</div>', post_html, re.DOTALL)
    if error_match:
        err_text = error_match.group(1).strip()
        if err_text:
            return False, err_text, {}

    error_desc = re.search(r'class="error-desc"[^>]*>(.*?)</div>', post_html, re.DOTALL)
    if error_desc and error_desc.group(1).strip():
        return False, error_desc.group(1).strip(), {}

    return False, "Не удалось отправить проверочный код. Проверьте правильность номера.", {}


def confirm_master_login(session_data: dict[str, Any], sms_code: str) -> tuple[bool, str]:
    """
    Step 2: Submit 4-digit code to complete login and store authenticated session.
    """
    cj = http.cookiejar.CookieJar()
    # Restore cookies
    for name, value in session_data.get("cookies", {}).items():
        ck = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain="prod.mastersapp.ru",
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        cj.set_cookie(ck)

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=_get_ssl_context()),
    )
    verify_url = session_data.get("verify_url", "https://prod.mastersapp.ru/account/auth/code")
    csrf = session_data.get("csrf", "")

    # Post verification code
    post_params = {
        "_csrf": csrf,
        "type": "master",
        "code": sms_code.strip(),
    }
    data = urllib.parse.urlencode(post_params).encode("utf-8")

    try:
        req = urllib.request.Request(
            verify_url,
            data=data,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": verify_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with opener.open(req, timeout=15) as resp:
            resp_html = resp.read().decode("utf-8", errors="ignore")
            final_url = resp.geturl()
    except Exception as e:
        logger.error("Error verifying SMS code in Masters: %s", e)
        return False, f"Ошибка подтверждения кода: {e}"

    # Check for error in response
    if "wrap-phone error" in resp_html or ("auth/code" in final_url and "error-desc" in resp_html and "Неверный код" in resp_html):
        error_match = re.search(r'class="error-desc"[^>]*>(.*?)</div>', resp_html, re.DOTALL)
        err_text = error_match.group(1).strip() if error_match else "Неверный проверочный код."
        return False, err_text

    # Check resulting cookies
    cookie_dict = {c.name: c.value for c in cj}
    phpsessid = cookie_dict.get("PHPSESSID")
    if not phpsessid:
        return False, "Не удалось получить авторизационную куку PHPSESSID от Masters."

    # Save to database
    with get_connection() as conn:
        val_true = True if conn.is_pg else 1
        val_false = False if conn.is_pg else 0
        # Deactivate old sessions
        conn.execute("UPDATE masters_session SET is_active = %s", (val_false,))
        # Insert new active session
        conn.execute(
            """
            INSERT INTO masters_session (master_phone, cookie_phpsessid, cookie_csrf, is_active)
            VALUES (%s, %s, %s, %s)
            """,
            (session_data.get("phone", ""), phpsessid, csrf, val_true),
        )
        conn.commit()

    return True, "Авторизация в Masters успешно завершена! Синхронизация включена."


def get_active_masters_session() -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM masters_session WHERE is_active = TRUE ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def disconnect_masters_session() -> bool:
    with get_connection() as conn:
        val_false = False if conn.is_pg else 0
        conn.execute("UPDATE masters_session SET is_active = %s", (val_false,))
        conn.commit()
        return True


def _get_masters_opener(session: dict[str, Any]) -> tuple[urllib.request.OpenerDirector, str]:
    phpsessid = session.get("cookie_phpsessid", "")
    csrf = session.get("cookie_csrf", "")
    cj = http.cookiejar.CookieJar()
    ck = http.cookiejar.Cookie(
        version=0,
        name="PHPSESSID",
        value=phpsessid,
        port=None,
        port_specified=False,
        domain="prod.mastersapp.ru",
        domain_specified=True,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
    )
    cj.set_cookie(ck)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=_get_ssl_context()),
    )
    return opener, csrf


def find_masters_appointment_id(session: dict[str, Any], appointment: dict[str, Any]) -> str | None:
    """
    Attempts to find the corresponding Masters event ID by matching client phone and datetime.
    """
    if appointment.get("masters_id"):
        return str(appointment["masters_id"])

    target_phone = normalize_phone(appointment.get("phone", ""))
    target_dt = appointment.get("appointment_time")
    if isinstance(target_dt, str):
        try:
            target_dt = datetime.datetime.fromisoformat(target_dt)
        except Exception:
            target_dt = None

    opener, _ = _get_masters_opener(session)
    target_urls = [
        "https://prod.mastersapp.ru/account/calendar",
        "https://prod.mastersapp.ru/calendar",
        "https://prod.mastersapp.ru/account/records",
        "https://prod.mastersapp.ru/records",
    ]

    for url in target_urls:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            with opener.open(req, timeout=10) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
                if "login" in resp.geturl():
                    disconnect_masters_session()
                    return None

                events_match = re.search(r'(?:events|records|appointments)\s*=\s*(\[\{.*?\}\]);', content, re.DOTALL)
                if events_match:
                    events_data = json.loads(events_match.group(1))
                    for ev in events_data:
                        ev_id = str(ev.get("id") or "")
                        if not ev_id:
                            continue
                        ev_phone = normalize_phone(ev.get("phone") or ev.get("client_phone") or "")
                        if target_phone and ev_phone and target_phone == ev_phone:
                            ev_start = ev.get("start") or ev.get("datetime")
                            if ev_start and target_dt:
                                try:
                                    dt = datetime.datetime.fromisoformat(ev_start.replace("Z", "+00:00"))
                                    diff = abs((dt.timestamp() - target_dt.timestamp()))
                                    if diff <= 45 * 60:
                                        if appointment.get("id"):
                                            with get_connection() as conn:
                                                conn.execute(
                                                    "UPDATE appointments SET masters_id = %s WHERE id = %s",
                                                    (ev_id, appointment["id"]),
                                                )
                                                conn.commit()
                                        return ev_id
                                except Exception:
                                    pass
                            else:
                                if appointment.get("id"):
                                    with get_connection() as conn:
                                        conn.execute(
                                            "UPDATE appointments SET masters_id = %s WHERE id = %s",
                                            (ev_id, appointment["id"]),
                                        )
                                        conn.commit()
                                return ev_id
        except Exception as e:
            logger.debug("Failed searching masters ID on %s: %s", url, e)

    return None


def confirm_masters_appointment(appointment: dict[str, Any]) -> tuple[bool, str]:
    """
    Synchronizes client confirmation with Masters CRM.
    Returns (success, message).
    """
    session = get_active_masters_session()
    if not session or not session.get("cookie_phpsessid"):
        return False, "Синхронизация Masters не подключена. Статус подтверждён в боте."

    masters_id = appointment.get("masters_id")
    if not masters_id:
        masters_id = find_masters_appointment_id(session, appointment)

    if not masters_id:
        return False, "Не найден ID записи в Masters для подтверждения."

    opener, csrf = _get_masters_opener(session)
    candidates = [
        ("POST", f"https://prod.mastersapp.ru/account/record/confirm?id={masters_id}", {"_csrf": csrf, "id": masters_id, "status": "confirmed"}),
        ("GET", f"https://prod.mastersapp.ru/account/record/confirm?id={masters_id}", None),
        ("POST", f"https://prod.mastersapp.ru/account/record/change-status?id={masters_id}&status=confirmed", {"_csrf": csrf, "id": masters_id, "status": "confirmed"}),
        ("POST", "https://prod.mastersapp.ru/calendar/change-status", {"_csrf": csrf, "id": masters_id, "status": "confirmed"}),
        ("POST", f"https://prod.mastersapp.ru/account/event/confirm?id={masters_id}", {"_csrf": csrf, "id": masters_id}),
    ]

    for method, url, post_params in candidates:
        try:
            data = urllib.parse.urlencode(post_params).encode("utf-8") if post_params else None
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://prod.mastersapp.ru/calendar",
                    "X-Requested-With": "XMLHttpRequest" if post_params else "",
                },
            )
            with opener.open(req, timeout=10) as resp:
                if resp.status in (200, 302):
                    logger.info("Successfully confirmed Masters appointment %s via %s", masters_id, url)
                    return True, "Запись успешно подтверждена в сервисе Masters."
        except urllib.error.HTTPError as he:
            if he.code in (200, 302):
                return True, "Запись успешно подтверждена в сервисе Masters."
            logger.debug("Masters confirm attempt failed on %s: HTTP %s", url, he.code)
        except Exception as e:
            logger.debug("Masters confirm attempt exception on %s: %s", url, e)

    return False, "Не удалось подтвердить запись в сервисе Masters."


def cancel_masters_appointment(appointment: dict[str, Any]) -> tuple[bool, str]:
    """
    Synchronizes client cancellation with Masters CRM.
    Returns (success, message).
    """
    session = get_active_masters_session()
    if not session or not session.get("cookie_phpsessid"):
        return False, "Синхронизация Masters не подключена. Запись отменена в боте."

    masters_id = appointment.get("masters_id")
    if not masters_id:
        masters_id = find_masters_appointment_id(session, appointment)

    if not masters_id:
        return False, "Не найден ID записи в Masters для отмены."

    opener, csrf = _get_masters_opener(session)
    candidates = [
        ("POST", f"https://prod.mastersapp.ru/account/record/delete?id={masters_id}", {"_csrf": csrf, "id": masters_id}),
        ("GET", f"https://prod.mastersapp.ru/account/record/delete?id={masters_id}", None),
        ("POST", f"https://prod.mastersapp.ru/account/record/cancel?id={masters_id}", {"_csrf": csrf, "id": masters_id}),
        ("POST", f"https://prod.mastersapp.ru/account/record/change-status?id={masters_id}&status=cancelled", {"_csrf": csrf, "id": masters_id, "status": "cancelled"}),
        ("POST", "https://prod.mastersapp.ru/calendar/event-delete", {"_csrf": csrf, "id": masters_id}),
        ("POST", f"https://prod.mastersapp.ru/account/event/delete?id={masters_id}", {"_csrf": csrf, "id": masters_id}),
    ]

    for method, url, post_params in candidates:
        try:
            data = urllib.parse.urlencode(post_params).encode("utf-8") if post_params else None
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://prod.mastersapp.ru/calendar",
                    "X-Requested-With": "XMLHttpRequest" if post_params else "",
                },
            )
            with opener.open(req, timeout=10) as resp:
                if resp.status in (200, 302):
                    logger.info("Successfully cancelled Masters appointment %s via %s", masters_id, url)
                    return True, "Запись успешно отменена в сервисе Masters."
        except urllib.error.HTTPError as he:
            if he.code in (200, 302):
                return True, "Запись успешно отменена в сервисе Masters."
            logger.debug("Masters cancel attempt failed on %s: HTTP %s", url, he.code)
        except Exception as e:
            logger.debug("Masters cancel attempt exception on %s: %s", url, e)

    return False, "Не удалось отменить запись в сервисе Masters."


def complete_masters_appointment(appointment: dict[str, Any]) -> tuple[bool, str]:
    """
    Synchronizes completed appointment (пострижен / визит состоялся) with Masters CRM.
    Returns (success, message).
    """
    session = get_active_masters_session()
    if not session or not session.get("cookie_phpsessid"):
        return False, "Синхронизация Masters не подключена. Статус обновлён в боте."

    masters_id = appointment.get("masters_id")
    if not masters_id:
        masters_id = find_masters_appointment_id(session, appointment)

    if not masters_id:
        return False, "Не найден ID записи в Masters."

    opener, csrf = _get_masters_opener(session)
    candidates = [
        ("POST", f"https://prod.mastersapp.ru/account/record/change-status?id={masters_id}&status=completed", {"_csrf": csrf, "id": masters_id, "status": "completed"}),
        ("POST", f"https://prod.mastersapp.ru/account/record/change-status?id={masters_id}&status=done", {"_csrf": csrf, "id": masters_id, "status": "done"}),
        ("POST", "https://prod.mastersapp.ru/calendar/change-status", {"_csrf": csrf, "id": masters_id, "status": "completed"}),
        ("POST", f"https://prod.mastersapp.ru/account/event/done?id={masters_id}", {"_csrf": csrf, "id": masters_id}),
    ]

    for method, url, post_params in candidates:
        try:
            data = urllib.parse.urlencode(post_params).encode("utf-8") if post_params else None
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://prod.mastersapp.ru/calendar",
                    "X-Requested-With": "XMLHttpRequest" if post_params else "",
                },
            )
            with opener.open(req, timeout=10) as resp:
                if resp.status in (200, 302):
                    logger.info("Successfully completed Masters appointment %s via %s", masters_id, url)
                    return True, "Запись успешно отмечена выполненной в сервисе Masters."
        except urllib.error.HTTPError as he:
            if he.code in (200, 302):
                return True, "Запись успешно отмечена выполненной в сервисе Masters."
            logger.debug("Masters complete attempt failed on %s: HTTP %s", url, he.code)
        except Exception as e:
            logger.debug("Masters complete attempt exception on %s: %s", url, e)

    return False, "Не удалось отметить запись выполненной в сервисе Masters."


def reschedule_masters_appointment(appointment: dict[str, Any], new_datetime: datetime.datetime) -> tuple[bool, str]:
    """
    Synchronizes rescheduled appointment with Masters CRM.
    Returns (success, message).
    """
    session = get_active_masters_session()
    if not session or not session.get("cookie_phpsessid"):
        return False, "Синхронизация Masters не подключена. Запись перенесена в боте."

    masters_id = appointment.get("masters_id")
    if not masters_id:
        masters_id = find_masters_appointment_id(session, appointment)

    if not masters_id:
        return False, "Не найден ID записи в Masters."

    opener, csrf = _get_masters_opener(session)
    time_str = new_datetime.strftime("%Y-%m-%d %H:%M")
    iso_str = new_datetime.isoformat()

    candidates = [
        ("POST", f"https://prod.mastersapp.ru/account/record/update?id={masters_id}", {"_csrf": csrf, "id": masters_id, "start": iso_str, "datetime": time_str}),
        ("POST", "https://prod.mastersapp.ru/calendar/event-move", {"_csrf": csrf, "id": masters_id, "start": iso_str}),
        ("POST", f"https://prod.mastersapp.ru/account/event/reschedule?id={masters_id}", {"_csrf": csrf, "id": masters_id, "date": time_str}),
    ]

    for method, url, post_params in candidates:
        try:
            data = urllib.parse.urlencode(post_params).encode("utf-8") if post_params else None
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://prod.mastersapp.ru/calendar",
                    "X-Requested-With": "XMLHttpRequest" if post_params else "",
                },
            )
            with opener.open(req, timeout=10) as resp:
                if resp.status in (200, 302):
                    logger.info("Successfully rescheduled Masters appointment %s via %s", masters_id, url)
                    return True, "Запись успешно перенесена в сервисе Masters."
        except urllib.error.HTTPError as he:
            if he.code in (200, 302):
                return True, "Запись успешно перенесена в сервисе Masters."
            logger.debug("Masters reschedule attempt failed on %s: HTTP %s", url, he.code)
        except Exception as e:
            logger.debug("Masters reschedule attempt exception on %s: %s", url, e)

    return False, "Не удалось перенести запись в сервисе Masters."


def create_masters_appointment(
    client_name: str,
    phone: str,
    services: str,
    appointment_time: datetime.datetime,
    price: int,
) -> tuple[bool, str, str | None]:
    """
    Attempts to create a new appointment record in Masters CRM if master session is active.
    Returns (success, message, masters_id).
    """
    session = get_active_masters_session()
    if not session or not session.get("cookie_phpsessid"):
        return False, "Синхронизация Masters не подключена. Запись создана локально.", None

    opener, csrf = _get_masters_opener(session)
    dt_str = appointment_time.strftime("%Y-%m-%d %H:%M")
    post_params = {
        "_csrf": csrf,
        "client_name": client_name,
        "phone": phone,
        "service": services,
        "price": str(price),
        "datetime": dt_str,
    }

    create_urls = [
        "https://prod.mastersapp.ru/account/record/create",
        "https://prod.mastersapp.ru/calendar/event-create",
    ]

    for url in create_urls:
        try:
            data = urllib.parse.urlencode(post_params).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://prod.mastersapp.ru/calendar",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            with opener.open(req, timeout=10) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
                id_m = re.search(r'(?:id|event_id)\s*[:=]\s*["\']?([a-zA-Z0-9_-]+)', content)
                masters_id = id_m.group(1) if id_m else None
                return True, "Запись успешно создана в Masters.", masters_id
        except Exception as e:
            logger.debug("Failed creating record in Masters on %s: %s", url, e)

    return False, "Не удалось создать запись в Masters.", None


def sync_schedule_from_masters_link() -> tuple[bool, int, str]:
    """
    Validates guessed placeholder appointments (masters_id like 'mst_YYYY-MM-DD_HHMM')
    against the live public schedule from mst.link and removes stale ones.

    It intentionally does NOT create new appointments from "gaps" between available
    slots: gap analysis cannot know the real client, service or exact start time and
    previously produced wrong times (e.g. 14:00 instead of 15:00) and phantom busy
    slots. Real appointments arrive here only from bot bookings or from the
    authenticated web sync in sync_masters_appointments().
    """
    try:
        from core_bot.services.masters import get_schedule
        days = get_schedule(["beard"])
    except Exception as e:
        logger.error("Failed to get schedule from Masters: %s", e)
        return False, 0, f"Ошибка получения расписания: {e}"

    if not days:
        return False, 0, "Не удалось получить расписание из Masters (mst.link)"

    removed_count = 0
    now = datetime.datetime.now(SARATOV_TZ)
    today_key = now.strftime("%Y-%m-%d")

    for day_key, day_data in days.items():
        if day_key < today_key:
            continue

        avail_set = set(s["time"] for s in day_data.get("slots", []))

        with get_connection() as conn:
            d_start = f"{day_key} 00:00:00"
            d_end = f"{day_key} 23:59:59"
            p_start = datetime.datetime.fromisoformat(d_start).replace(tzinfo=SARATOV_TZ) if conn.is_pg else d_start
            p_end = datetime.datetime.fromisoformat(d_end).replace(tzinfo=SARATOV_TZ) if conn.is_pg else d_end

            m_appts = conn.execute(
                """
                SELECT id, masters_id, appointment_time, client_name, phone
                FROM appointments
                WHERE platform = 'masters'
                  AND (masters_id LIKE 'mst_%' OR masters_id IS NULL)
                  AND (status IS NULL OR status != 'cancelled')
                  AND appointment_time >= %s AND appointment_time <= %s
                """,
                (p_start, p_end),
            ).fetchall()

            for ma in m_appts:
                ma_id = ma["masters_id"]
                appt_dt = ma["appointment_time"]
                if isinstance(appt_dt, str):
                    appt_t = appt_dt[11:16]
                else:
                    appt_t = appt_dt.strftime("%H:%M")

                # A guessed placeholder becomes stale when its slot is free again on
                # mst.link (or when it was guessed for the middle of a gap).
                is_stale_placeholder = (
                    ma_id and ma_id.startswith("mst_") and appt_t in avail_set
                )
                if not is_stale_placeholder:
                    continue

                is_unmodified_placeholder = (
                    ma["client_name"] in ("Клиент", "Клиент (Masters)", None)
                    and not ma["phone"]
                )
                if is_unmodified_placeholder:
                    conn.execute("DELETE FROM appointments WHERE id = %s", (ma["id"],))
                else:
                    conn.execute(
                        "UPDATE appointments SET status = 'cancelled' WHERE id = %s",
                        (ma["id"],),
                    )
                conn.commit()
                removed_count += 1

    return True, removed_count, f"Очищено устаревших заглушек: {removed_count}"


def sync_masters_appointments() -> tuple[bool, int, str]:
    """
    Synchronizes records from Masters CRM:
    1. Synchronizes live schedule & booked appointments from mst.link
    2. If active master session exists, also queries prod.mastersapp.ru
    Returns (success, synced_count, message).
    """
    ok_link, count_link, msg_link = sync_schedule_from_masters_link()

    session = get_active_masters_session()
    if not session or not session.get("cookie_phpsessid"):
        return ok_link, count_link, f"Синхронизация Masters завершена: {count_link} записей."

    synced_web = 0
    opener, _ = _get_masters_opener(session)
    target_urls = [
        "https://prod.mastersapp.ru/account/calendar",
        "https://prod.mastersapp.ru/calendar",
        "https://prod.mastersapp.ru/account/records",
        "https://prod.mastersapp.ru/records",
    ]
    for url in target_urls:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            with opener.open(req, timeout=5) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
                if "login" in resp.geturl():
                    disconnect_masters_session()
                    break

                events_match = re.search(r'(?:events|records|appointments)\s*=\s*(\[\{.*?\}\]);', content, re.DOTALL)
                if events_match:
                    events_data = json.loads(events_match.group(1))
                    for ev in events_data:
                        ev_id = str(ev.get("id") or "")
                        ev_phone = ev.get("phone") or ev.get("client_phone") or ""
                        if is_master_phone(ev_phone):
                            ev_phone = ""
                        ev_name = ev.get("client_name") or ev.get("name") or "Клиент"
                        ev_start = ev.get("start") or ev.get("datetime")
                        ev_title = ev.get("title") or ev.get("service_name") or "Стрижка"
                        ev_price = ev.get("price") or 2300
                        if ev_start:
                            try:
                                dt = datetime.datetime.fromisoformat(ev_start.replace("Z", "+00:00"))
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=SARATOV_TZ)
                                else:
                                    dt = dt.astimezone(SARATOV_TZ)
                            except Exception:
                                from core_bot.services.appointments import parse_to_saratov
                                dt = parse_to_saratov(ev_start)
                            create_or_update_appointment(
                                phone=ev_phone,
                                appointment_time=dt,
                                services=ev_title,
                                total_price=int(ev_price),
                                client_name=ev_name,
                                platform="masters",
                                masters_id=ev_id if ev_id else None,
                            )
                            synced_web += 1
                    if synced_web > 0:
                        break
        except Exception as e:
            logger.debug("Web sync attempt on %s: %s", url, e)

    total_synced = count_link + synced_web
    return True, total_synced, f"Синхронизация Masters завершена: {total_synced} записей."
