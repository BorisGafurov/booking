import datetime
import html as html_module
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

_last_services_fetch_time = 0.0
_cached_masters_services: list[dict[str, Any]] | None = None


def get_service_options_for_creation() -> list[dict[str, Any]]:
    """
    Returns actual services from Masters for appointment creation.
    Defaults to Boris Gafurov's real services on Masters:
    - Стрижка (МУЖСКАЯ) — 2 300 ₽
    - Оформление бороды — 700 ₽
    - Стрижка + Борода — 3 000 ₽
    """
    global _last_services_fetch_time, _cached_masters_services
    now = time.time()
    if _cached_masters_services and (now - _last_services_fetch_time < 300):
        return _cached_masters_services

    opts = [
        {"name": "Стрижка (МУЖСКАЯ)", "price": 2300, "duration_text": "1 ч 15 мин"},
        {"name": "Оформление бороды", "price": 700, "duration_text": "1 ч"},
        {"name": "Стрижка + Борода", "price": 3000, "duration_text": "2 ч 15 мин"},
    ]
    try:
        req = urllib.request.Request(
            f"{BASE_MASTER_URL}/price",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, context=_get_ssl_context(), timeout=4) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            pattern = r'<div class="service[^"]*"[^>]*data-id="([^"]+)".*?<div class="name">(.*?)</div>.*?<div class="info">(.*?)</div>'
            parsed = []
            for m in re.finditer(pattern, html, re.DOTALL):
                s_name = m.group(2).strip()
                info = m.group(3).strip()
                pm = re.search(r'([\d\s]+)\s*₽', info)
                price = int(re.sub(r'\D', '', pm.group(1))) if pm else 2300
                dm = re.search(r'<span>([^<]+(?:час|мин)[^<]*)</span>', info)
                dur_text = dm.group(1).strip() if dm else ""
                parsed.append({"name": s_name, "price": price, "duration_text": dur_text})
            if parsed:
                opts = parsed
                hair = next((s for s in parsed if "стрижк" in s["name"].lower()), None)
                beard = next((s for s in parsed if "бород" in s["name"].lower()), None)
                if hair and beard:
                    opts.append({
                        "name": f"{hair['name']} + {beard['name']}",
                        "price": hair["price"] + beard["price"],
                        "duration_text": "2 ч 15 мин",
                    })
    except Exception as e:
        logger.debug("Live services fetch from Masters skipped: %s", e)

    _cached_masters_services = opts
    _last_services_fetch_time = now
    return opts

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


def _submit_masters_comment_form(
    opener: urllib.request.OpenerDirector,
    comment_url: str,
    client_name: str = "",
) -> tuple[bool, str, str | None]:
    """Fetch the comment page, fill client details if needed, and submit to finalize the booking."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = opener.open(urllib.request.Request(comment_url, headers=headers), timeout=10)
        page_html = res.read().decode("utf-8")
        current_url = res.geturl()

        # If already at finish page
        if "/finish" in current_url:
            parsed = urllib.parse.urlparse(current_url)
            params = urllib.parse.parse_qs(parsed.query)
            appt_id = params.get("id", [None])[0]
            return True, "Запись успешно подтверждена!", appt_id

        # Extract CSRF token
        csrf_m = (
            re.search(r'name=["\']_csrf["\']\s+value=["\']([^"\']+)["\']', page_html)
            or re.search(r'value=["\']([^"\']+)["\']\s+name=["\']_csrf["\']', page_html)
            or re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', page_html)
        )
        csrf = csrf_m.group(1) if csrf_m else ""

        # Extract existing names from form inputs if prefilled by CRM
        fn_m = (
            re.search(r'name=[\'"]OnlineInfoForm\[firstname\][\'"][^>]*value=[\'"]([^\'"]*)[\'"]', page_html)
            or re.search(r'value=[\'"]([^\'"]*)[\'"][^>]*name=[\'"]OnlineInfoForm\[firstname\][\'"]', page_html)
        )
        ln_m = (
            re.search(r'name=[\'"]OnlineInfoForm\[lastname\][\'"][^>]*value=[\'"]([^\'"]*)[\'"]', page_html)
            or re.search(r'value=[\'"]([^\'"]*)[\'"][^>]*name=[\'"]OnlineInfoForm\[lastname\][\'"]', page_html)
        )

        fn = html_module.unescape(fn_m.group(1).strip()) if fn_m else ""
        ln = html_module.unescape(ln_m.group(1).strip()) if ln_m else ""

        if not fn:
            if client_name:
                parts = client_name.strip().split(maxsplit=1)
                fn = parts[0]
                ln = parts[1] if len(parts) > 1 else (ln or "")
            else:
                fn = "Клиент"

        # Determine submit URL
        action_m = re.search(r'<form[^>]*id=[\'"]online-info-form[\'"][^>]*action=[\'"]([^\'"]+)[\'"]', page_html)
        if action_m and action_m.group(1):
            action = action_m.group(1)
            post_url = f"https://mst.link{action}" if action.startswith("/") else action
        else:
            post_url = current_url

        payload = {
            "_csrf": csrf,
            "OnlineInfoForm[firstname]": fn,
            "OnlineInfoForm[lastname]": ln,
            "OnlineInfoForm[comment]": "Запись через чат-бота",
            "OnlineInfoForm[email]": "",
            "OnlineInfoForm[isMasterPersonalData]": "1",
        }

        post_data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            post_url,
            data=post_data,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": current_url,
                "Origin": "https://mst.link",
            },
        )
        res_post = opener.open(req, timeout=10)
        final_url = res_post.geturl()
        final_html = res_post.read().decode("utf-8")

        parsed = urllib.parse.urlparse(final_url)
        params = urllib.parse.parse_qs(parsed.query)
        appt_id = params.get("id", [None])[0]

        if not appt_id:
            id_match = re.search(r'(?:appointment_id|record_id|order_id|id)\s*[:=]\s*["\']?([a-zA-Z0-9_-]+)', final_html)
            if id_match:
                appt_id = id_match.group(1)

        return True, "Запись успешно подтверждена!", appt_id
    except Exception as e:
        logger.error("Error submitting Masters comment form: %s", e)
        return False, f"Ошибка при оформлении записи в Masters: {e}", None


def initiate_booking(
    phone: str,
    selected_keys: list[str] | set[str] | None,
    timestamp: int,
    client_name: str = "",
) -> tuple[bool, str, dict[str, Any] | None]:
    clean_digits = "".join(ch for ch in phone if ch.isdigit())
    if len(clean_digits) == 11 and clean_digits.startswith(("7", "8")):
        clean_digits = clean_digits[1:]

    if clean_digits in ("9173225155", "79173225155"):
        return (
            False,
            "Вы не можете сделать запись на номер самого мастера (+79173225155). Masters запрещает мастеру записывать себя на свой же номер телефона. Для тестирования введите любой другой номер клиента (например: +79991234567).",
            None,
        )

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

        # Case A: Existing client recognized by Masters (moves directly to comment page, NO SMS needed)
        if data.get("move_to_url"):
            comment_path = data["move_to_url"]
            comment_url = f"https://mst.link{comment_path}" if comment_path.startswith("/") else comment_path
            ok, res_msg, appt_id = _submit_masters_comment_form(opener, comment_url, client_name=client_name)
            if not ok:
                return False, res_msg, None
            session_data = {
                "needs_sms": False,
                "phone": phone,
                "timestamp": timestamp,
                "calc": calc,
                "appointment_id": appt_id,
            }
            return True, "Запись успешно подтверждена!", session_data

        # Case B: SMS verification required
        if data.get("success") and data.get("verify_url"):
            verify_path = data["verify_url"]
            verify_url = f"https://mst.link{verify_path}" if verify_path.startswith("/") else verify_path

            # Load verify page to fetch CSRF token
            v_res = opener.open(urllib.request.Request(verify_url, headers=headers), timeout=10)
            v_html = v_res.read().decode("utf-8")
            csrf_m = (
                re.search(r'name=["\']_csrf["\']\s+value=["\']([^"\']+)["\']', v_html)
                or re.search(r'value=["\']([^"\']+)["\']\s+name=["\']_csrf["\']', v_html)
            )
            csrf = csrf_m.group(1) if csrf_m else ""

            session_data = {
                "needs_sms": True,
                "opener": opener,
                "verify_url": verify_url,
                "csrf": csrf,
                "phone": phone,
                "timestamp": timestamp,
                "calc": calc,
                "client_name": client_name,
            }
            return True, "Код подтверждения отправлен по СМС.", session_data

        # Case C: Error returned by Masters
        err_msg = data.get("error") or data.get("message") or data.get("msg")
        if not err_msg and isinstance(data.get("errors"), dict):
            err_msg = "; ".join(str(v) for v in data["errors"].values())
        if not err_msg:
            err_msg = "Не удалось отправить проверочный код. Попробуйте записаться через сайт."
        return False, err_msg, None
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
        final_url = res.geturl()
        html = res.read().decode("utf-8")

        if "неверный код" in html.lower() or "ошибка" in html.lower():
            return False, "Неверный код из СМС. Попробуйте ввести ещё раз."

        # If redirected to comment page, submit the final comment form
        if "/comment" in final_url:
            ok, msg, appt_id = _submit_masters_comment_form(
                opener,
                final_url,
                client_name=session_data.get("client_name", ""),
            )
            if not ok:
                return False, msg
            session_data["appointment_id"] = appt_id
            return True, "Запись успешно подтверждена!"

        # Extract appointment_id from final URL or HTML response
        parsed_url = urllib.parse.urlparse(final_url)
        params = urllib.parse.parse_qs(parsed_url.query)
        if "id" in params and params["id"]:
            session_data["appointment_id"] = params["id"][0]
        else:
            id_match = re.search(r'(?:appointment_id|record_id|order_id|id)\s*[:=]\s*["\']?([a-zA-Z0-9_-]+)', html)
            if id_match:
                session_data["appointment_id"] = id_match.group(1)

        return True, "Запись успешно подтверждена!"
    except Exception as e:
        logger.error("Error confirming SMS code in Masters: %s", e)
        return False, f"Ошибка при подтверждении кода: {e}"
