"""Cross-platform notification service for Telegram, VK, and MAX bots.

Allows any bot or backend service to deliver client and master notifications
across all connected messengers (Telegram, VKontakte, and MAX), rather than
limiting delivery only to the messenger where an action was initiated.
"""

import json
import logging
import os
import re
import secrets
import ssl
import urllib.parse
import urllib.request
from typing import Any

from core_bot.database.connection import get_connection

logger = logging.getLogger(__name__)

# Insecure/permissive SSL context for environments lacking Ministry of Digital Development certs
_INSECURE_SSL_CTX = ssl.create_default_context()
_INSECURE_SSL_CTX.check_hostname = False
_INSECURE_SSL_CTX.verify_mode = ssl.CERT_NONE


def strip_html(text: str) -> str:
    """Removes HTML tags and normalizes spaces for plain-text messengers."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", "", text)
    return clean.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def send_telegram_direct(chat_id: int | str, text: str, parse_mode: str = "HTML") -> bool:
    """Directly dispatches a Telegram message via the Bot API."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as err:
        logger.warning("Telegram direct notify failed for %s: %s", chat_id, err)
        return False


def send_vk_direct(user_id: int | str, text: str) -> bool:
    """Directly dispatches a VK message via messages.send."""
    token = (os.getenv("VK_BOT_TOKEN") or os.getenv("VK_TOKEN") or "").strip()
    if not token or not user_id:
        return False
    url = "https://api.vk.com/method/messages.send"
    params = {
        "user_id": int(user_id),
        "random_id": secrets.randbits(31),
        "message": strip_html(text),
        "access_token": token,
        "v": "5.199",
    }
    try:
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return "response" in body
    except Exception as err:
        logger.warning("VK direct notify failed for %s: %s", user_id, err)
        return False


def send_max_direct(user_id: int | str, text: str) -> bool:
    """Directly dispatches a MAX messenger message via platform-api2."""
    token = (os.getenv("MAX_BOT_TOKEN") or "").strip()
    if not token or not user_id:
        return False
    url = f"https://platform-api2.max.ru/messages?user_id={urllib.parse.quote(str(user_id))}"
    payload = {
        "text": text,
        "format": "html",
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status in (200, 201)
        except urllib.error.URLError as ssl_err:
            if "certificate verify failed" in str(ssl_err).lower():
                with urllib.request.urlopen(req, timeout=5, context=_INSECURE_SSL_CTX) as resp:
                    return resp.status in (200, 201)
            raise
    except Exception as err:
        logger.warning("MAX direct notify failed for %s: %s", user_id, err)
        return False


def resolve_client_user(user_or_id_or_phone: Any) -> dict[str, Any] | None:
    """Resolves the user dict from users table by dict, int ID or phone string."""
    if isinstance(user_or_id_or_phone, dict) and "id" in user_or_id_or_phone:
        return user_or_id_or_phone

    from core_bot.services.referrals import normalize_phone

    user_id = None
    phone = None

    if isinstance(user_or_id_or_phone, int) or (isinstance(user_or_id_or_phone, str) and user_or_id_or_phone.isdigit()):
        user_id = int(user_or_id_or_phone)
    elif isinstance(user_or_id_or_phone, str):
        phone = normalize_phone(user_or_id_or_phone)
    elif isinstance(user_or_id_or_phone, dict):
        user_id = user_or_id_or_phone.get("user_id") or user_or_id_or_phone.get("id")
        raw_phone = user_or_id_or_phone.get("phone")
        if raw_phone:
            phone = normalize_phone(raw_phone)

    with get_connection() as conn:
        row = None
        if user_id:
            row = conn.execute(
                "SELECT id, phone, first_name, last_name, telegram_id, vk_id, max_user_id FROM users WHERE id = %s",
                (user_id,),
            ).fetchone()
        if not row and phone:
            row = conn.execute(
                "SELECT id, phone, first_name, last_name, telegram_id, vk_id, max_user_id FROM users WHERE phone = %s",
                (phone,),
            ).fetchone()
        return dict(row) if row else None


def notify_client_crossplatform(user_or_target: Any, text_html: str) -> int:
    """
    Delivers a message to all messengers linked to the user account
    (Telegram, VK, and MAX). Returns the count of successfully delivered channels.
    """
    user = resolve_client_user(user_or_target)
    if not user:
        # If target dict has explicit platform IDs directly:
        delivered = 0
        if isinstance(user_or_target, dict):
            tg_id = user_or_target.get("telegram_id") or (user_or_target.get("platform_user_id") if user_or_target.get("platform") == "telegram" else None)
            vk_id = user_or_target.get("vk_id") or (user_or_target.get("platform_user_id") if user_or_target.get("platform") == "vk" else None)
            max_id = user_or_target.get("max_user_id") or (user_or_target.get("platform_user_id") if user_or_target.get("platform") == "max" else None)
            if tg_id and send_telegram_direct(tg_id, text_html):
                delivered += 1
            if vk_id and send_vk_direct(vk_id, text_html):
                delivered += 1
            if max_id and send_max_direct(max_id, text_html):
                delivered += 1
        return delivered

    delivered = 0
    tg_id = user.get("telegram_id")
    vk_id = user.get("vk_id")
    max_id = user.get("max_user_id")

    if tg_id:
        if send_telegram_direct(tg_id, text_html):
            delivered += 1
    if vk_id:
        if send_vk_direct(vk_id, text_html):
            delivered += 1
    if max_id:
        if send_max_direct(max_id, text_html):
            delivered += 1

    return delivered


def notify_referral_reward(ref_notify: dict[str, Any]) -> None:
    """
    Notifies both the referrer and the invited friend across ALL their connected
    messengers (TG, VK, MAX), regardless of which platform the visit was completed from.
    """
    if not ref_notify:
        return

    invited_name = ref_notify.get("invited_first_name") or "друг"
    amount = ref_notify.get("amount", 300)
    ref_bal = ref_notify.get("new_balance", 300)
    inv_bal = ref_notify.get("invited_new_balance", 300)

    # 1. Message to referrer
    referrer_msg = (
        "🎉 <b>Отличные новости!</b>\n"
        f"Твой друг <b>{invited_name}</b> посетил салон!\n"
        f"Тебе начислен бонус: <b>{amount} ₽</b>.\n\n"
        f"💰 Доступный баланс бонусов: <b>{ref_bal} ₽</b>.\n"
        "Ты можешь списать их при следующем визите к мастеру!"
    )
    if ref_notify.get("referrer_telegram_id"):
        send_telegram_direct(ref_notify["referrer_telegram_id"], referrer_msg)
    if ref_notify.get("referrer_vk_id"):
        send_vk_direct(ref_notify["referrer_vk_id"], referrer_msg)
    if ref_notify.get("referrer_max_id"):
        send_max_direct(ref_notify["referrer_max_id"], referrer_msg)

    # 2. Message to invited friend
    invited_msg = (
        "🎁 <b>Спасибо за визит!</b>\n"
        f"Так как ты пришёл по рекомендации друга, тебе начислено <b>{amount} ₽</b> бонусов на повторный визит!\n\n"
        f"💰 Твой баланс бонусов: <b>{inv_bal} ₽</b>.\n"
        "Ты сможешь списать их при следующей стрижке!"
    )
    if ref_notify.get("invited_telegram_id"):
        send_telegram_direct(ref_notify["invited_telegram_id"], invited_msg)
    if ref_notify.get("invited_vk_id"):
        send_vk_direct(ref_notify["invited_vk_id"], invited_msg)
    if ref_notify.get("invited_max_id"):
        send_max_direct(ref_notify["invited_max_id"], invited_msg)


def notify_appointment_cancelled_by_master(appt: dict[str, Any]) -> None:
    """Broadcasts cancellation notification to all channels connected to the client."""
    text = (
        "❌ <b>Ваша запись была отменена мастером.</b>\n\n"
        "Если у вас возникли вопросы или вы хотите подобрать другое удобное время, "
        "пожалуйста, свяжитесь с мастером или оформите новую запись."
    )
    notify_client_crossplatform(appt, text)


def notify_appointment_rescheduled_by_master(appt: dict[str, Any], formatted_dt: str) -> None:
    """Broadcasts reschedule notification to all channels connected to the client."""
    service_str = appt.get("services") or "Стрижка"
    price_str = f"{appt.get('total_price')} ₽" if appt.get("total_price") else ""
    price_line = f"💰 Стоимость: {price_str}\n\n" if price_str else "\n"
    text = (
        "🗓 <b>Ваша запись перенесена мастером!</b>\n\n"
        f"⏰ Новое время: <b>{formatted_dt}</b>\n"
        f"✂️ Услуга: {service_str}\n"
        f"{price_line}"
        "Ждём вас в салоне «Срез»! 💈"
    )
    notify_client_crossplatform(appt, text)
