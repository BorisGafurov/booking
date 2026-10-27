import asyncio
import datetime
import logging
import os
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMIN_IDS
from services.appointments import (
    SARATOV_TZ,
    get_due_2h_reminders,
    get_due_confirmation_followup_reminders,
    get_due_evening_reminders,
    get_due_retention_reminders,
    mark_reminder_sent,
)
from services.masters_sync import sync_masters_appointments

logger = logging.getLogger(__name__)


def _get_confirmation_keyboard(appointment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтверждаю запись",
                    callback_data=f"appt:confirm:{appointment_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"appt:cancel:{appointment_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📍 Как добраться",
                    url="https://yandex.ru/maps/-/CDu~v-7x",
                ),
            ],
        ]
    )


def _get_confirmed_2h_keyboard(appointment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📍 Как добраться",
                    url="https://yandex.ru/maps/-/CDu~v-7x",
                ),
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"appt:cancel:{appointment_id}",
                ),
            ],
        ]
    )


def _get_retention_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✂️ Записаться онлайн",
                    callback_data="menu:booking",
                ),
            ],
        ]
    )


MASTER_PHONES = {
    p.strip() for p in os.getenv("MASTER_PHONES", "+79173225155,79173225155,89173225155").split(",") if p.strip()
}
MASTER_TELEGRAM_IDS = {
    int(x.strip()) for x in (os.getenv("MASTER_TELEGRAM_IDS") or "1010373026,503925574").split(",") if x.strip().isdigit()
}
MASTER_VK_IDS = {
    int(x.strip()) for x in (os.getenv("MASTER_VK_IDS") or "90599236").split(",") if x.strip().isdigit()
}
MASTER_MAX_IDS = {
    int(x.strip()) for x in (os.getenv("MASTER_MAX_IDS") or "").split(",") if x.strip().isdigit()
}
MASTER_USER_IDS = {
    int(x.strip()) for x in (os.getenv("MASTER_USER_IDS") or "").split(",") if x.strip().isdigit()
}


def _is_master_or_admin(user_id: int, platform: str) -> bool:
    if not user_id:
        return False
    if user_id in ADMIN_IDS:
        return True
    if platform == "telegram" and user_id in MASTER_TELEGRAM_IDS:
        return True
    if platform == "vk" and user_id in MASTER_VK_IDS:
        return True
    if platform == "max" and user_id in MASTER_MAX_IDS:
        return True
    if user_id in MASTER_USER_IDS:
        return True
    try:
        from services.referrals import is_master_user, is_admin_user
        if is_master_user(user_id, platform=platform) or is_admin_user(user_id, platform=platform):
            return True
    except Exception:
        pass
    return False


def _resolve_platform_user_id(appt: dict, platform: str) -> int | None:
    from services.referrals import normalize_phone

    raw_phone = appt.get("phone") or ""
    norm_phone = normalize_phone(raw_phone)
    if norm_phone in MASTER_PHONES or norm_phone == "+79173225155":
        return None

    user_id = appt.get("platform_user_id")
    if user_id and user_id != 0:
        if _is_master_or_admin(user_id, platform):
            return None
        return user_id

    from database import get_connection
    with get_connection() as conn:
        col = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_user_id"}.get(platform)
        if not col:
            return None
        row = None
        if appt.get("user_id"):
            row = conn.execute(f"SELECT id, phone, {col} FROM users WHERE id = %s", (appt["user_id"],)).fetchone()
        if not row and norm_phone:
            row = conn.execute(f"SELECT id, phone, {col} FROM users WHERE phone = %s", (norm_phone,)).fetchone()

        if row:
            row_phone = normalize_phone(row["phone"] or "")
            if row_phone in MASTER_PHONES or row_phone == "+79173225155":
                return None
            if row[col]:
                found_id = row[col]
                if _is_master_or_admin(found_id, platform):
                    return None
                conn.execute(
                    "UPDATE appointments SET platform_user_id = %s, platform = %s WHERE id = %s",
                    (found_id, platform, appt["id"]),
                )
                conn.commit()
                return found_id
    return None


async def run_reminder_worker(bot: Bot, platform: str = "telegram"):
    logger.info("Запущен фоновый воркер напоминаний (%s)", platform)
    sync_counter = 0

    while True:
        try:
            # 1. Evening reminder (day before visit, >= 18:00)
            evening_due = get_due_evening_reminders(platform=platform)
            for appt in evening_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if not target_user_id:
                    mark_reminder_sent(appt["id"], "evening")
                    continue

                dt = appt["appointment_time"]
                if isinstance(dt, str):
                    dt = datetime.datetime.fromisoformat(dt)
                dt_local = dt.astimezone(SARATOV_TZ) if dt.tzinfo else dt
                date_str = dt_local.strftime("%d.%m.%Y")
                time_str = dt_local.strftime("%H:%M")
                weekdays = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
                weekday_str = weekdays[dt_local.weekday()]

                text = (
                    "🔔 <b>Напоминание о записи к мастеру Борису Гафурову</b>\n\n"
                    f"Завтра в {weekday_str} (<b>{date_str}</b>) в <b>{time_str}</b> ждём тебя на стрижку!\n"
                    f"✂️ <b>Услуги:</b> {appt['services']}\n"
                    f"💰 <b>Стоимость:</b> {appt['total_price']} ₽\n"
                    "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
                    "👉 <b>Пожалуйста, подтверди свой визит</b> или отмени запись, если твои планы изменились:"
                )
                try:
                    await bot.send_message(
                        chat_id=target_user_id,
                        text=text,
                        reply_markup=_get_confirmation_keyboard(appt["id"]),
                        parse_mode="HTML",
                    )
                    mark_reminder_sent(appt["id"], "evening")
                    logger.info("Отправлено вечернее напоминание клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки вечернего напоминания #%s: %s", appt["id"], send_err)

            # 2. Morning follow-up reminder (on visit day, >= 09:00, if not confirmed)
            morning_due = get_due_confirmation_followup_reminders(platform=platform)
            for appt in morning_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if not target_user_id:
                    mark_reminder_sent(appt["id"], "morning")
                    continue

                dt = appt["appointment_time"]
                if isinstance(dt, str):
                    dt = datetime.datetime.fromisoformat(dt)
                dt_local = dt.astimezone(SARATOV_TZ) if dt.tzinfo else dt
                time_str = dt_local.strftime("%H:%M")

                text = (
                    "⚠️ <b>Подтверждение записи на сегодня</b>\n\n"
                    f"Ты записан сегодня в <b>{time_str}</b>, но ещё <b>не подтвердил</b> свой визит.\n\n"
                    f"✂️ <b>Услуги:</b> {appt['services']}\n"
                    f"💰 <b>Стоимость:</b> {appt['total_price']} ₽\n"
                    "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
                    "👉 Пожалуйста, подтверди запись, чтобы мастер держал за тобой время, или отмени её, если не сможешь прийти:"
                )
                try:
                    await bot.send_message(
                        chat_id=target_user_id,
                        text=text,
                        reply_markup=_get_confirmation_keyboard(appt["id"]),
                        parse_mode="HTML",
                    )
                    mark_reminder_sent(appt["id"], "morning")
                    logger.info("Отправлено утреннее напоминание о подтверждении клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки утреннего напоминания #%s: %s", appt["id"], send_err)

            # 3. 2h express reminder
            two_h_due = get_due_2h_reminders(platform=platform)
            for appt in two_h_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if not target_user_id:
                    mark_reminder_sent(appt["id"], "2h")
                    continue

                dt = appt["appointment_time"]
                if isinstance(dt, str):
                    dt = datetime.datetime.fromisoformat(dt)
                dt_local = dt.astimezone(SARATOV_TZ) if dt.tzinfo else dt
                time_str = dt_local.strftime("%H:%M")

                is_confirmed = bool(appt.get("client_confirmed"))
                if is_confirmed:
                    text = (
                        "⏰ <b>До твоей записи осталось 2 часа!</b>\n\n"
                        f"Ждём тебя сегодня в <b>{time_str}</b>.\n"
                        f"✂️ <b>Услуги:</b> {appt['services']}\n"
                        "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
                        "<i>Твоя запись подтверждена, мастер готов и ждёт тебя! ✂️</i>"
                    )
                    reply_markup = _get_confirmed_2h_keyboard(appt["id"])
                else:
                    text = (
                        "⏰ <b>До твоей записи осталось 2 часа!</b>\n\n"
                        f"Ждём тебя сегодня в <b>{time_str}</b>.\n"
                        f"✂️ <b>Услуги:</b> {appt['services']}\n"
                        "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
                        "⚠️ <b>Внимание: запись всё ещё не подтверждена!</b>\n"
                        "Пожалуйста, подтверди визит или отмени запись, чтобы мастер не ждал впустую:"
                    )
                    reply_markup = _get_confirmation_keyboard(appt["id"])

                try:
                    await bot.send_message(
                        chat_id=target_user_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                    mark_reminder_sent(appt["id"], "2h")
                    logger.info("Отправлено 2ч напоминание клиенту #%s (appt #%s, confirmed=%s)", target_user_id, appt["id"], is_confirmed)
                except Exception as send_err:
                    logger.warning("Ошибка отправки 2ч напоминания #%s: %s", appt["id"], send_err)

            # 4. 2.5 weeks retention reminder
            retention_due = get_due_retention_reminders(platform=platform)
            for appt in retention_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if not target_user_id:
                    mark_reminder_sent(appt["id"], "retention")
                    continue

                text = (
                    "✂️ <b>Привет! Прошло 2,5 недели с твоей стрижки</b>\n\n"
                    "Волосы уже отросли, и форма начинает теряться. Самое время освежить стрижку и бороду! 💈\n\n"
                    "Нажми кнопку ниже, чтобы занять удобное время на этой неделе:"
                )
                try:
                    await bot.send_message(
                        chat_id=target_user_id,
                        text=text,
                        reply_markup=_get_retention_keyboard(),
                        parse_mode="HTML",
                    )
                    mark_reminder_sent(appt["id"], "retention")
                    logger.info("Отправлено retention напоминание клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки retention напоминания #%s: %s", appt["id"], send_err)

            # 5. Periodic Masters sync every ~10 minutes (10 ticks * 60s)
            sync_counter += 1
            if sync_counter >= 10:
                sync_counter = 0
                if platform == "telegram":
                    try:
                        ok, count, msg = sync_masters_appointments()
                        if ok and count > 0:
                            logger.info("Фоновая синхронизация Masters: %s", msg)
                    except Exception as sync_err:
                        logger.warning("Ошибка фоновой синхронизации Masters: %s", sync_err)

        except Exception as e:
            logger.error("Ошибка в цикле reminder worker: %s", e)

        await asyncio.sleep(60)
