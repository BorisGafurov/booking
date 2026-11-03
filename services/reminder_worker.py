import asyncio
import datetime
import html
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
    record_reminder_delivery,
)
from services.reviews import (
    get_pending_notifications,
    mark_notification_failed,
    mark_notification_sent,
    mark_notification_skipped,
)

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


def _get_review_invite_keyboard(appointment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"⭐ {n}", callback_data=f"review:rate:{appointment_id}:{n}")
                for n in range(1, 6)
            ],
            [
                InlineKeyboardButton(
                    text="✂️ Записаться на следующий визит",
                    callback_data="menu:booking",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Оставить отзыв в группе ВКонтакте",
                    url="https://vk.ru/reviews-242475554",
                )
            ],
        ]
    )


MASTER_TELEGRAM_IDS = {
    int(x.strip()) for x in (os.getenv("MASTER_TELEGRAM_IDS") or "").split(",") if x.strip().isdigit()
}
MASTER_VK_IDS = {
    int(x.strip()) for x in (os.getenv("MASTER_VK_IDS") or "").split(",") if x.strip().isdigit()
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
    from services.referrals import is_master_phone, normalize_phone

    raw_phone = appt.get("phone") or ""
    norm_phone = normalize_phone(raw_phone)
    if is_master_phone(norm_phone):
        return None

    appt_platform = str(appt.get("platform") or "").lower()
    user_id = appt.get("platform_user_id")
    if user_id and user_id != 0 and appt_platform in ("", "web", "manual", platform):
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
            row = conn.execute(
                "SELECT id, phone, telegram_id, vk_id, max_user_id FROM users WHERE id = %s",
                (appt["user_id"],),
            ).fetchone()
        if not row and norm_phone:
            row = conn.execute(
                "SELECT id, phone, telegram_id, vk_id, max_user_id FROM users WHERE phone = %s",
                (norm_phone,),
            ).fetchone()

        if row:
            row_phone = normalize_phone(row["phone"] or "")
            if is_master_phone(row_phone):
                return None
            if row[col]:
                found_id = row[col]
                if _is_master_or_admin(found_id, platform):
                    return None
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
                if target_user_id == "reassigned":
                    continue
                if not target_user_id:
                    try:
                        from core_bot.services.voice import is_voice_configured, send_voice_reminder
                        if is_voice_configured():
                            send_voice_reminder(appt)
                            record_reminder_delivery(appt["id"], "evening", platform, "voice")
                            mark_reminder_sent(appt["id"], "evening")
                            continue
                    except Exception as v_err:
                        logger.warning("Voice reminder error: %s", v_err)
                    record_reminder_delivery(appt["id"], "evening", platform, "skipped", "no_recipient")
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
                    record_reminder_delivery(appt["id"], "evening", platform)
                    mark_reminder_sent(appt["id"], "evening")
                    logger.info("Отправлено вечернее напоминание клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки вечернего напоминания #%s: %s", appt["id"], send_err)

            # 2. Morning follow-up reminder (on visit day, >= 09:00, if not confirmed)
            morning_due = get_due_confirmation_followup_reminders(platform=platform)
            for appt in morning_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if target_user_id == "reassigned":
                    continue
                if not target_user_id:
                    try:
                        from core_bot.services.voice import is_voice_configured, send_voice_reminder
                        if is_voice_configured():
                            send_voice_reminder(appt)
                            record_reminder_delivery(appt["id"], "morning", platform, "voice")
                            mark_reminder_sent(appt["id"], "morning")
                            continue
                    except Exception as v_err:
                        logger.warning("Voice reminder error: %s", v_err)
                    record_reminder_delivery(appt["id"], "morning", platform, "skipped", "no_recipient")
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
                    record_reminder_delivery(appt["id"], "morning", platform)
                    mark_reminder_sent(appt["id"], "morning")
                    logger.info("Отправлено утреннее напоминание о подтверждении клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки утреннего напоминания #%s: %s", appt["id"], send_err)

                # Дополнительно совершаем робозвонок с подтверждением через Zvonok
                try:
                    from core_bot.services.voice import is_voice_configured, send_voice_reminder
                    if is_voice_configured():
                        send_voice_reminder(appt)
                        record_reminder_delivery(appt["id"], "morning", platform, "voice")
                except Exception as v_err:
                    logger.warning("Voice reminder error in morning followup: %s", v_err)

            # 3. 2h express reminder
            two_h_due = get_due_2h_reminders(platform=platform)
            for appt in two_h_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if target_user_id == "reassigned":
                    continue
                if not target_user_id:
                    record_reminder_delivery(appt["id"], "2h", platform, "skipped", "no_recipient")
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
                    record_reminder_delivery(appt["id"], "2h", platform)
                    mark_reminder_sent(appt["id"], "2h")
                    logger.info("Отправлено 2ч напоминание клиенту #%s (appt #%s, confirmed=%s)", target_user_id, appt["id"], is_confirmed)
                except Exception as send_err:
                    logger.warning("Ошибка отправки 2ч напоминания #%s: %s", appt["id"], send_err)

            # 4. 2.5 weeks retention reminder
            retention_due = get_due_retention_reminders(platform=platform)
            for appt in retention_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if target_user_id == "reassigned":
                    continue
                if not target_user_id:
                    record_reminder_delivery(appt["id"], "retention", platform, "skipped", "no_recipient")
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
                    record_reminder_delivery(appt["id"], "retention", platform)
                    mark_reminder_sent(appt["id"], "retention")
                    logger.info("Отправлено retention напоминание клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки retention напоминания #%s: %s", appt["id"], send_err)

            # 5. Review invitations (sent when master confirms payment in CRM)
            invites = get_pending_notifications(platform, "review_invite")
            for invite in invites:
                target_user_id = invite.get("recipient_id")
                if not target_user_id:
                    mark_notification_skipped(invite["id"], "missing_recipient")
                    continue

                if invite.get("existing_review_id"):
                    mark_notification_skipped(invite["id"], "already_reviewed")
                    continue

                services_name = invite.get("services") or "Стрижка"
                text = (
                    "✂️ <b>Спасибо, что посетил салон Срез!</b>\n\n"
                    f"Визит: <b>{html.escape(str(services_name))}</b>\n\n"
                    "Надеемся, стрижка принесла удовольствие и отличный настрой! 💈\n"
                    "Пожалуйста, оцени качество визита от 1 до 5 звёзд ниже.\n\n"
                    "📅 <b>Хочешь сразу запланировать следующий визит?</b>\n"
                    "Чтобы зафиксировать удобный день и время заранее, нажми кнопку ниже 👇"
                )
                try:
                    await bot.send_message(
                        chat_id=target_user_id,
                        text=text,
                        reply_markup=_get_review_invite_keyboard(invite["appointment_id"]),
                        parse_mode="HTML",
                    )
                    mark_notification_sent(invite["id"])
                    logger.info("Отправлено приглашение оставить отзыв клиенту #%s (appt #%s)", target_user_id, invite["appointment_id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки приглашения к отзыву #%s: %s", invite["id"], send_err)
                    mark_notification_failed(invite["id"], str(send_err))

            # 6. Master new review notifications (sent to admin/master when client reviews)
            master_alerts = get_pending_notifications(platform, "master_new_review")
            for alert in master_alerts:
                rating = alert.get("rating") or 5
                stars = "⭐" * int(rating)
                client_name = alert.get("first_name") or "Клиент"
                phone = alert.get("phone") or "—"
                services_str = alert.get("services") or "Визит"
                comment = alert.get("comment") or ""
                alert_text = (
                    f"⭐ <b>Новый отзыв: {rating}/5</b> ({stars})\n\n"
                    f"👤 <b>Клиент:</b> {html.escape(str(client_name))}\n"
                    f"📱 <b>Телефон:</b> {html.escape(str(phone))}\n"
                    f"✂️ <b>Услуга:</b> {html.escape(str(services_str))}\n\n"
                    f"«{html.escape(str(comment))}»\n\n"
                    "🌐 <i>Отзыв опубликован на сайте srezforma.ru</i>"
                )
                admin_recipients = set(ADMIN_IDS) | set(MASTER_TELEGRAM_IDS)
                for admin_id in admin_recipients:
                    try:
                        await bot.send_message(
                            chat_id=admin_id,
                            text=alert_text,
                            parse_mode="HTML",
                        )
                    except Exception as a_err:
                        logger.warning("Не удалось уведомить админа %s о новом отзыве: %s", admin_id, a_err)
                mark_notification_sent(alert["id"])

            # 7. Master alerts about bookings made in the other messengers.
            event_titles = {
                "master_booking_created": "🔥 <b>Новая онлайн-запись через бота!</b>",
                "master_booking_confirmed": "✅ <b>Клиент подтвердил запись!</b>",
                "master_booking_cancelled": "⚠️ <b>Клиент отменил запись!</b>",
                "master_booking_rescheduled": "🔄 <b>Клиент перенёс запись</b>",
            }
            for event_kind, event_title in event_titles.items():
                events = get_pending_notifications(platform, event_kind)
                for event in events:
                    client_name = " ".join(
                        part for part in (event.get("first_name"), event.get("last_name")) if part
                    ) or "Клиент"
                    event_text = (
                        f"{event_title}\n\n"
                        f"👤 <b>Клиент:</b> {html.escape(str(client_name))}\n"
                        f"📱 <b>Телефон:</b> {html.escape(str(event.get('phone') or '—'))}\n"
                        f"✂️ <b>Услуги:</b> {html.escape(str(event.get('services') or '—'))}\n"
                        f"📅 <b>Время:</b> {html.escape(str(event.get('appointment_time') or '—'))}\n"
                        f"💰 <b>Сумма:</b> {event.get('total_price') or '—'} ₽"
                    )
                    for admin_id in set(ADMIN_IDS) | set(MASTER_TELEGRAM_IDS):
                        try:
                            await bot.send_message(chat_id=admin_id, text=event_text, parse_mode="HTML")
                        except Exception as a_err:
                            logger.warning("Не удалось уведомить админа %s (%s): %s", admin_id, event_kind, a_err)
                    mark_notification_sent(event["id"])

        except Exception as e:
            logger.error("Ошибка в цикле reminder worker: %s", e)

        await asyncio.sleep(60)
