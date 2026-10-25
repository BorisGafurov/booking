import asyncio
import datetime
import logging
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMIN_IDS
from services.appointments import (
    SARATOV_TZ,
    get_due_2h_reminders,
    get_due_evening_reminders,
    get_due_retention_reminders,
    mark_reminder_sent,
)
from services.masters_sync import sync_masters_appointments

logger = logging.getLogger(__name__)


def _get_evening_reminder_keyboard(appointment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отменить запись",
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


def _resolve_platform_user_id(appt: dict, platform: str) -> int | None:
    user_id = appt.get("platform_user_id")
    if user_id and user_id != 0:
        return user_id

    from database import get_connection
    with get_connection() as conn:
        col = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_user_id"}.get(platform)
        if not col:
            return None
        row = None
        if appt.get("user_id"):
            row = conn.execute(f"SELECT {col} FROM users WHERE id = %s", (appt["user_id"],)).fetchone()
        if not row and appt.get("phone"):
            row = conn.execute(f"SELECT {col} FROM users WHERE phone = %s", (appt["phone"],)).fetchone()

        if row and row[col]:
            found_id = row[col]
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
                    "<i>Если твои планы изменились, пожалуйста, нажми кнопку ниже:</i>"
                )
                try:
                    await bot.send_message(
                        chat_id=target_user_id,
                        text=text,
                        reply_markup=_get_evening_reminder_keyboard(appt["id"]),
                        parse_mode="HTML",
                    )
                    mark_reminder_sent(appt["id"], "evening")
                    logger.info("Отправлено вечернее напоминание клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки вечернего напоминания #%s: %s", appt["id"], send_err)

            # 2. 2h express reminder
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

                text = (
                    "⏰ <b>До твоей записи осталось 2 часа!</b>\n\n"
                    f"Ждём тебя сегодня в <b>{time_str}</b>.\n"
                    f"✂️ <b>Услуги:</b> {appt['services']}\n"
                    "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
                    "<i>До скорой встречи!</i>"
                )
                try:
                    await bot.send_message(
                        chat_id=target_user_id,
                        text=text,
                        parse_mode="HTML",
                    )
                    mark_reminder_sent(appt["id"], "2h")
                    logger.info("Отправлено 2ч напоминание клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки 2ч напоминания #%s: %s", appt["id"], send_err)

            # 3. 2.5 weeks retention reminder
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

            # 4. Periodic Masters sync every ~10 minutes (10 ticks * 60s)
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
