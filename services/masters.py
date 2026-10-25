import datetime
import http.cookiejar
import json
import logging
import re
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any

from config import BOOKING_URL

logger = logging.getLogger(__name__)

BASE_MASTER_URL = "https://mst.link/boris_gafurov"


def _get_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl._create_unverified_context()

SERVICES = {
    "hair": {
        "key": "hair",
        "id": "692482aa433e53a6300c6fd8",
        "name": "Стрижка (МУЖСКАЯ)",
        "price": 2300,
        "duration_min": 75,
        "duration_text": "1 ч 15 мин",
    },
    "beard": {
        "key": "beard",
        "id": "6924832636e2911887000156",
        "name": "Оформление бороды",
        "price": 700,
        "duration_min": 60,
        "duration_text": "1 ч",
    },
}

DEFAULT_SERVICE_KEYS = ["hair"]

# In-memory storage for user selections and pending bookings
user_selected_services: dict[int, set[str]] = {}
user_selected_day: dict[int, str] = {}
user_pending_booking: dict[int, dict[str, Any]] = {}

# TTL cache for schedules: cache_key -> (expires_at, days_map)
_schedule_cache: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_TTL = 90.0  # 1.5 minutes


def calculate_services(selected_keys: list[str] | set[str] | None) -> dict[str, Any]:
    if not selected_keys:
        keys = list(DEFAULT_SERVICE_KEYS)
    else:
        keys = [k for k in selected_keys if k in SERVICES]
        if not keys:
            keys = list(DEFAULT_SERVICE_KEYS)

    total_price = sum(SERVICES[k]["price"] for k in keys)
    total_min = sum(SERVICES[k]["duration_min"] for k in keys)
    hours = total_min // 60
    mins = total_min % 60
    dur_str = f"{hours} ч" if hours else ""
    if mins:
        dur_str = f"{dur_str} {mins} мин".strip()

    return {
        "keys": keys,
        "names": [SERVICES[k]["name"] for k in keys],
        "service_ids": [SERVICES[k]["id"] for k in keys],
        "total_price": total_price,
        "duration_minutes": total_min,
        "duration_text": dur_str,
    }


def get_schedule(selected_keys: list[str] | set[str] | None) -> dict[str, Any]:
    calc = calculate_services(selected_keys)
    cache_key = ",".join(sorted(calc["service_ids"]))
    now = time.time()

    cached = _schedule_cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    days_map: dict[str, Any] = {}
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=_get_ssl_context()),
        )
        headers = {"User-Agent": "Mozilla/5.0"}

        # Step 1: visit price page to acquire session and CID
        res1 = opener.open(urllib.request.Request(f"{BASE_MASTER_URL}/price", headers=headers), timeout=10)
        html1 = res1.read().decode("utf-8")
        cid_m = re.search(r'var ONLINE_CID = "([^"]+)"', html1)
        cid = cid_m.group(1) if cid_m else ""

        # Step 2: request price page with selected services
        srv_str = ",".join(calc["service_ids"])
        url_slots = f"{BASE_MASTER_URL}/price?services={srv_str}&{cid}"
        res2 = opener.open(urllib.request.Request(url_slots, headers=headers), timeout=10)
        html2 = res2.read().decode("utf-8")

        slots = re.findall(r'<span\s+data-date=[\'"](\d+)[\'"][^>]*>([^<]+)</span>', html2)
        ru_weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        months_ru = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

        for ts_str, time_str in slots:
            ts = int(ts_str)
            dt = datetime.datetime.fromtimestamp(ts)
            day_key = dt.strftime("%Y-%m-%d")
            wd = ru_weekdays[dt.weekday()]
            full_title = f"{wd}, {dt.day} {months_ru[dt.month]}"

            if day_key not in days_map:
                days_map[day_key] = {"day_key": day_key, "title": full_title, "slots": []}
            days_map[day_key]["slots"].append({"timestamp": ts, "time": time_str.strip()})

        _schedule_cache[cache_key] = (now + CACHE_TTL, days_map)
    except Exception as e:
        logger.error("Failed to fetch schedule from Masters: %s", e)

    return days_map


def initiate_booking(phone: str, selected_keys: list[str] | set[str] | None, timestamp: int) -> tuple[bool, str, dict[str, Any] | None]:
    clean_digits = "".join(ch for ch in phone if ch.isdigit())
    if len(clean_digits) == 11 and clean_digits.startswith(("7", "8")):
        clean_digits = clean_digits[1:]

    calc = calculate_services(selected_keys)
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=_get_ssl_context()),
        )
        headers = {"User-Agent": "Mozilla/5.0"}

        # 1. Price
        res1 = opener.open(urllib.request.Request(f"{BASE_MASTER_URL}/price", headers=headers), timeout=10)
        html1 = res1.read().decode("utf-8")
        cid_m = re.search(r'var ONLINE_CID = "([^"]+)"', html1)
        cid = cid_m.group(1) if cid_m else ""

        # 2. Select services
        srv_str = ",".join(calc["service_ids"])
        url_srv = f"{BASE_MASTER_URL}/price?services={srv_str}&{cid}"
        opener.open(urllib.request.Request(url_srv, headers=headers), timeout=10)

        # 3. Select time slot
        url_time = f"{BASE_MASTER_URL}/times?date={timestamp}&{cid}"
        res3 = opener.open(urllib.request.Request(url_time, headers=headers), timeout=10)
        html3 = res3.read().decode("utf-8")

        action_m = re.search(r'<form[^>]*action=[\'"]([^\'"]+)[\'"]', html3)
        action = action_m.group(1) if action_m else f"/boris_gafurov/phone?{cid}"
        full_action = f"https://mst.link{action}" if action.startswith("/") else action

        post_data = urllib.parse.urlencode({"code": "RU", "phone": clean_digits, "consent_auth": "1"}).encode("utf-8")
        req_phone = urllib.request.Request(
            full_action,
            data=post_data,
            headers={
                "User-Agent": "Mozilla/5.0",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": res3.geturl(),
            },
        )
        res_phone = opener.open(req_phone, timeout=10)
        data = json.loads(res_phone.read().decode("utf-8"))

        if not data.get("success"):
            err_msg = data.get("error") or "Не удалось отправить проверочный код. Попробуйте записаться через сайт."
            return False, err_msg, None

        verify_url = "https://mst.link" + data["verify_url"]

        # Load verify page to fetch CSRF token
        v_res = opener.open(urllib.request.Request(verify_url, headers=headers), timeout=10)
        v_html = v_res.read().decode("utf-8")
        csrf_m = re.search(r'name="_csrf"\s+value="([^"]+)"', v_html)
        csrf = csrf_m.group(1) if csrf_m else ""

        session_data = {
            "opener": opener,
            "verify_url": verify_url,
            "csrf": csrf,
            "phone": phone,
            "timestamp": timestamp,
            "calc": calc,
        }
        return True, "Код подтверждения отправлен по СМС.", session_data
    except Exception as e:
        logger.error("Error initiating Masters booking: %s", e)
        return False, f"Ошибка при подключении к сервису записи: {e}", None


def confirm_sms_code(session_data: dict[str, Any], code: str) -> tuple[bool, str]:
    opener = session_data.get("opener")
    verify_url = session_data.get("verify_url")
    csrf = session_data.get("csrf", "")

    if not opener or not verify_url:
        return False, "Сессия записи устарела. Пожалуйста, начните запись заново."

    clean_code = "".join(ch for ch in code if ch.isdigit())
    if len(clean_code) < 4:
        return False, "Код из СМС должен содержать 4 цифры."

    try:
        post_data = urllib.parse.urlencode({"_csrf": csrf, "code": clean_code}).encode("utf-8")
        req = urllib.request.Request(
            verify_url,
            data=post_data,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": verify_url,
            },
        )
        res = opener.open(req, timeout=10)
        html = res.read().decode("utf-8")

        if "неверный код" in html.lower() or "ошибка" in html.lower():
            return False, "Неверный код из СМС. Попробуйте ввести ещё раз."

        return True, "Запись успешно подтверждена!"
    except Exception as e:
        logger.error("Error confirming SMS code in Masters: %s", e)
        return False, f"Ошибка при подтверждении кода: {e}"
