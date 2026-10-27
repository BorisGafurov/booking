import asyncio
import datetime
import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_IDS, BOOKING_URL
from services.appointments import (
    SARATOV_TZ,
    cancel_appointment,
    confirm_appointment_by_client,
    create_or_update_appointment,
)
from keyboards.booking import (
    get_booking_keyboard,
    get_days_keyboard,
    get_request_phone_keyboard,
    get_services_keyboard,
    get_slots_keyboard,
    get_sms_keyboard,
)
from keyboards.menu import get_main_menu
from services.masters import (
    SERVICES,
    calculate_services,
    confirm_sms_code,
    get_schedule,
    initiate_booking,
    user_pending_booking,
    user_selected_day,
    user_selected_services,
)
from services.referrals import get_user_phone, is_phone_number, link_user_phone, normalize_phone

logger = logging.getLogger(__name__)
router = Router()
user_selected_slot: dict[int, int] = {}


def _format_services_text(selected_keys: list[str] | set[str]) -> str:
    calc = calculate_services(selected_keys)
    names_str = " + ".join(calc["names"])
    return (
        "✂️ <b>Онлайн-запись к мастеру Борису Гафурову</b>\n\n"
        "💡 <b>Как выбрать услуги:</b>\n"
        "• Нажмите на услугу один раз — появится галочка ✅ (услуга выбрана)\n"
        "• Нажмите ещё раз — галочка снимется ⚪️ (услуга удалена)\n"
        "<i>Можно выбрать одну услугу или сразу обе.</i>\n\n"
        f"📋 <b>Выбрано:</b> {names_str}\n"
        f"💰 <b>Итого:</b> {calc['total_price']} ₽ ({calc['duration_text']})\n\n"
        "<i>После выбора нажмите кнопку «📅 Выбрать время»:</i>"
    )


@router.message(Command("book", "booking"))
@router.message(F.text.in_({"✂️ Записаться", "Записаться"}))
@router.callback_query(F.data.in_({"menu:booking", "book:start"}))
async def booking_start(event: Message | CallbackQuery):
    from_user = event.from_user
    if not from_user:
        return

    user_id = from_user.id
    user_selected_services[user_id] = {"hair"}
    text = _format_services_text(user_selected_services[user_id])
    kb = get_services_keyboard(user_selected_services[user_id])

    if isinstance(event, CallbackQuery):
        if event.message:
            try:
                await event.message.answer(
                    text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception:
                pass
        await event.answer()
    else:
        await event.answer(
            text,
            reply_markup=kb,
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("book:toggle:"))
async def toggle_service(callback: CallbackQuery):
    if not callback.from_user:
        return

    user_id = callback.from_user.id
    key = callback.data.split(":")[2]

    current = user_selected_services.get(user_id, {"hair"})
    alert_msg = None
    if key in current:
        if len(current) > 1:
            current.remove(key)
        else:
            alert_msg = "Нужно выбрать хотя бы одну услугу"
    else:
        current.add(key)
    user_selected_services[user_id] = current

    try:
        if callback.message:
            await callback.message.edit_text(
                _format_services_text(current),
                reply_markup=get_services_keyboard(current),
                parse_mode="HTML",
            )
    except TelegramBadRequest:
        pass
    await callback.answer(alert_msg)


@router.callback_query(F.data == "book:services")
async def back_to_services(callback: CallbackQuery):
    if not callback.from_user:
        return

    user_id = callback.from_user.id
    current = user_selected_services.get(user_id, {"hair"})

    try:
        if callback.message:
            await callback.message.edit_text(
                _format_services_text(current),
                reply_markup=get_services_keyboard(current),
                parse_mode="HTML",
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "book:choose_day")
async def choose_day(callback: CallbackQuery):
    if not callback.from_user:
        return

    user_id = callback.from_user.id
    current_services = user_selected_services.get(user_id, {"hair"})
    schedule = await asyncio.to_thread(get_schedule, current_services)

    if not schedule:
        if callback.message:
            await callback.message.edit_text(
                "⚠️ Не удалось загрузить расписание в реальном времени.\n\n"
                "Вы можете выбрать удобное время напрямую на сайте Masters:",
                reply_markup=get_booking_keyboard(),
            )
        await callback.answer()
        return

    calc = calculate_services(current_services)
    try:
        if callback.message:
            await callback.message.edit_text(
                f"📅 <b>Выберите удобный день записи:</b>\n\n"
                f"Услуги: <b>{', '.join(calc['names'])}</b>\n"
                f"Длительность: <b>{calc['duration_text']}</b>\n"
                f"Стоимость: <b>{calc['total_price']} ₽</b>",
                reply_markup=get_days_keyboard(schedule),
                parse_mode="HTML",
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("book:day:"))
async def choose_time_slot(callback: CallbackQuery):
    if not callback.from_user:
        return

    user_id = callback.from_user.id
    day_key = callback.data.split(":")[2]
    user_selected_day[user_id] = day_key

    current_services = user_selected_services.get(user_id, {"hair"})
    schedule = await asyncio.to_thread(get_schedule, current_services)
    day_data = schedule.get(day_key)
    slots = day_data["slots"] if day_data else []

    title = day_data["title"] if day_data else day_key
    try:
        if callback.message:
            await callback.message.edit_text(
                f"🕒 <b>Свободные окна на {title}:</b>\n\n"
                "Выберите удобное время начала стрижки:",
                reply_markup=get_slots_keyboard(slots),
                parse_mode="HTML",
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


async def _finalize_successful_booking(
    bot,
    user_id: int,
    session_data: dict[str, Any],
    client_name: str,
    target_msg_or_cb,
):
    user_pending_booking.pop(user_id, None)
    user_selected_slot.pop(user_id, None)
    calc = session_data["calc"]
    dt = datetime.datetime.fromtimestamp(session_data["timestamp"], tz=SARATOV_TZ)
    phone = session_data["phone"]

    appt_id = create_or_update_appointment(
        phone=phone,
        appointment_time=dt,
        services=", ".join(calc["names"]),
        total_price=calc["total_price"],
        client_name=client_name,
        platform="telegram",
        platform_user_id=user_id,
        masters_id=session_data.get("appointment_id"),
    )

    cancel_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отменить запись",
                    callback_data=f"appt:cancel:{appt_id}",
                )
            ]
        ]
    )

    text = (
        f"🎉 <b>Запись успешно подтверждена!</b>\n\n"
        f"✂️ <b>Услуги:</b> {', '.join(calc['names'])}\n"
        f"💰 <b>Стоимость:</b> {calc['total_price']} ₽ ({calc['duration_text']})\n"
        f"📅 <b>Дата и время:</b> {dt.strftime('%d.%m.%Y в %H:%M')}\n"
        f"📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
        "Ждём вас! Мы пришлём напоминание накануне вечером и за 2 часа до визита."
    )

    if isinstance(target_msg_or_cb, CallbackQuery):
        try:
            if target_msg_or_cb.message:
                await target_msg_or_cb.message.edit_text(
                    text,
                    reply_markup=cancel_markup,
                    parse_mode="HTML",
                )
        except TelegramBadRequest:
            pass
        await target_msg_or_cb.answer()
    else:
        await target_msg_or_cb.answer(
            text,
            reply_markup=cancel_markup,
            parse_mode="HTML",
        )

    c_name_esc = html.escape(str(client_name))
    srv_names_esc = html.escape(", ".join(calc["names"]))
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=(
                    "🔥 <b>Новая онлайн-запись через бота!</b>\n\n"
                    f"👤 <b>Клиент:</b> {c_name_esc}\n"
                    f"📱 <b>Телефон:</b> {phone}\n"
                    f"✂️ <b>Услуги:</b> {srv_names_esc}\n"
                    f"📅 <b>Время:</b> {dt.strftime('%d.%m.%Y в %H:%M')}\n"
                    f"💰 <b>Сумма:</b> {calc['total_price']} ₽"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Failed to notify admin %s: %s", admin_id, e)


@router.callback_query(F.data.startswith("book:slot:"))
async def initiate_slot_booking(callback: CallbackQuery):
    if not callback.from_user:
        return

    user_id = callback.from_user.id
    timestamp = int(callback.data.split(":")[2])
    user_selected_slot[user_id] = timestamp
    phone = get_user_phone(user_id, platform="telegram")

    if not phone:
        if callback.message:
            await callback.message.answer(
                "📱 Для завершения записи подтвердите номер телефона:\n\n"
                "Нажмите кнопку ниже или просто отправьте номер ответным сообщением (например, <code>+79991234567</code>):",
                reply_markup=get_request_phone_keyboard(),
                parse_mode="HTML",
            )
        await callback.answer()
        return

    try:
        if callback.message:
            await callback.message.edit_text("⏳ Бронирую слот в Masters...")
    except TelegramBadRequest:
        pass

    current_services = user_selected_services.get(user_id, {"hair"})
    client_name = callback.from_user.full_name or "Клиент"
    success, msg, session_data = await asyncio.to_thread(
        initiate_booking, phone, current_services, timestamp, client_name=client_name
    )

    if not success or not session_data:
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"⚠️ {msg}\n\nВы можете завершить бронирование напрямую через сайт:",
                    reply_markup=get_booking_keyboard(),
                )
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

    if not session_data.get("needs_sms", True):
        await _finalize_successful_booking(callback.bot, user_id, session_data, client_name, callback)
        return

    user_pending_booking[user_id] = session_data
    dt = datetime.datetime.fromtimestamp(timestamp, tz=SARATOV_TZ)

    try:
        if callback.message:
            await callback.message.edit_text(
                f"📩 <b>Подтверждение записи</b>\n\n"
                f"Вы выбрали: <b>{dt.strftime('%d.%m.%Y в %H:%M')}</b>\n"
                f"На ваш номер <code>{phone}</code> отправлено СМС-сообщение с кодом от Masters.\n\n"
                "👉 <b>Напишите 4 цифры из СМС в ответном сообщении:</b>",
                reply_markup=get_sms_keyboard(),
                parse_mode="HTML",
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "book:cancel")
async def cancel_booking(callback: CallbackQuery):
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    user_pending_booking.pop(user_id, None)
    user_selected_slot.pop(user_id, None)

    try:
        if callback.message:
            await callback.message.edit_text("Запись отменена.")
    except TelegramBadRequest:
        pass
    await callback.answer("Запись отменена.")


async def _process_phone_provided(message: Message, user_id: int, phone_raw: str):
    from handlers.referrals import _has_active_admin_draft

    if _has_active_admin_draft(message):
        return

    success, _ = link_user_phone(user_id, phone_raw, platform="telegram")
    if not success:
        await message.answer("Пожалуйста, введите корректный номер телефона (например, +79991234567).")
        return

    pending_ts = user_selected_slot.pop(user_id, None)
    if pending_ts:
        eff_phone = get_user_phone(user_id, platform="telegram") or normalize_phone(phone_raw)
        current_services = user_selected_services.get(user_id, {"hair"})
        dt = datetime.datetime.fromtimestamp(pending_ts, tz=SARATOV_TZ)

        await message.answer(
            f"⏳ Номер <code>{eff_phone}</code> сохранён. Связываюсь с Masters для записи на <b>{dt.strftime('%d.%m.%Y в %H:%M')}</b>...",
            parse_mode="HTML",
        )
        client_name = message.from_user.full_name if message.from_user else "Клиент"
        success_init, msg_init, session_data = await asyncio.to_thread(
            initiate_booking, eff_phone, current_services, pending_ts, client_name=client_name
        )
        if not success_init or not session_data:
            await message.answer(
                f"⚠️ {msg_init}\n\nВы можете завершить бронирование напрямую через сайт:",
                reply_markup=get_booking_keyboard(),
            )
            return

        if not session_data.get("needs_sms", True):
            await _finalize_successful_booking(message.bot, user_id, session_data, client_name, message)
            return

        user_pending_booking[user_id] = session_data
        await message.answer(
            f"📩 <b>Подтверждение записи</b>\n\n"
            f"Вы выбрали: <b>{dt.strftime('%d.%m.%Y в %H:%M')}</b>\n"
            f"На ваш номер <code>{eff_phone}</code> отправлено СМС-сообщение с кодом от Masters.\n\n"
            "👉 <b>Напишите 4 цифры из СМС в ответном сообщении:</b>",
            reply_markup=get_sms_keyboard(),
            parse_mode="HTML",
        )
        return

    user_selected_services[user_id] = {"hair"}
    await message.answer(
        "✅ <b>Номер телефона успешно сохранен!</b>\n\n"
        "Теперь выберите услуги для записи:",
        reply_markup=get_services_keyboard(user_selected_services[user_id]),
        parse_mode="HTML",
    )
    is_admin_user = user_id in ADMIN_IDS
    await message.answer("Главное меню:", reply_markup=get_main_menu(is_admin_user))


@router.message(F.contact)
async def handle_contact(message: Message):
    if not message.contact or not message.from_user:
        return
    await _process_phone_provided(message, message.from_user.id, message.contact.phone_number)


@router.message(F.text == "◀️ Назад в меню")
async def back_to_menu(message: Message):
    if not message.from_user:
        return
    is_admin_user = message.from_user.id in ADMIN_IDS
    await message.answer("Главное меню:", reply_markup=get_main_menu(is_admin_user))


@router.message(
    lambda msg: bool(
        msg.from_user
        and msg.from_user.id in user_pending_booking
        and msg.text
        and msg.text.strip().isdigit()
        and len(msg.text.strip()) == 4
    )
)
async def handle_sms_code(message: Message):
    if not message.from_user or not message.text:
        return

    user_id = message.from_user.id
    session_data = user_pending_booking.get(user_id)

    if not session_data:
        return  # Regular message, not waiting for SMS code

    code = message.text.strip()
    success, msg = await asyncio.to_thread(confirm_sms_code, session_data, code)

    if not success:
        await message.answer(
            f"❌ <b>{html.escape(msg)}</b>\n\n"
            "Попробуйте ввести 4 цифры ещё раз или завершите запись на сайте:",
            reply_markup=get_sms_keyboard(),
            parse_mode="HTML",
        )
        return

    # Successfully booked!
    client_name = message.from_user.full_name or "Клиент"
    await _finalize_successful_booking(message.bot, user_id, session_data, client_name, message)


@router.callback_query(F.data.startswith("appt:confirm:"))
async def handle_confirm_appointment(callback: CallbackQuery):
    if not callback.from_user or not callback.data:
        return

    parts = callback.data.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await callback.answer("Некорректная запись")
        return

    appt_id = int(parts[2])
    success, msg, appt = confirm_appointment_by_client(
        appt_id, platform="telegram", platform_user_id=callback.from_user.id
    )

    if not success:
        await callback.answer(msg, show_alert=True)
        return

    dt_str = ""
    if appt and appt.get("appointment_time"):
        dt = appt["appointment_time"]
        if isinstance(dt, str):
            dt = datetime.datetime.fromisoformat(dt)
        dt_local = dt.astimezone(SARATOV_TZ) if dt.tzinfo else dt
        dt_str = dt_local.strftime("%d.%m.%Y в %H:%M")

    try:
        if callback.message:
            await callback.message.edit_text(
                "✅ <b>Запись подтверждена!</b>\n\n"
                f"Спасибо за подтверждение! Ждём вас <b>{dt_str}</b> ✂️\n"
                "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="📍 Как добраться",
                                url="https://yandex.ru/maps/-/CDu~v-7x",
                            ),
                            InlineKeyboardButton(
                                text="❌ Отменить запись",
                                callback_data=f"appt:cancel:{appt_id}",
                            ),
                        ]
                    ]
                ),
                parse_mode="HTML",
            )
    except TelegramBadRequest:
        pass

    await callback.answer("Запись успешно подтверждена! Ждём вас.")

    # Notify master
    if appt:
        c_name = html.escape(str(appt.get('client_name') or 'Клиент'))
        c_srv = html.escape(str(appt.get('services') or ''))
        for admin_id in ADMIN_IDS:
            try:
                await callback.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "✅ <b>Клиент подтвердил запись!</b>\n\n"
                        f"👤 <b>Клиент:</b> {c_name}\n"
                        f"📱 <b>Телефон:</b> {appt.get('phone')}\n"
                        f"✂️ <b>Услуги:</b> {c_srv}\n"
                        f"📅 <b>Время:</b> {dt_str}\n"
                        "Клиент точно придёт к назначенному времени."
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("Failed to notify admin %s: %s", admin_id, e)


@router.callback_query(F.data.startswith("appt:cancel:"))
async def handle_cancel_appointment(callback: CallbackQuery):
    if not callback.from_user or not callback.data:
        return

    parts = callback.data.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await callback.answer("Некорректная запись")
        return

    appt_id = int(parts[2])
    success, msg, appt = cancel_appointment(
        appt_id, platform="telegram", platform_user_id=callback.from_user.id
    )

    if not success:
        await callback.answer(msg, show_alert=True)
        return

    try:
        if callback.message:
            await callback.message.edit_text(
                "❌ <b>Запись успешно отменена</b>\n\n"
                "Будем рады видеть вас в другой раз! Чтобы выбрать новую дату, нажмите «✂️ Записаться».",
                parse_mode="HTML",
            )
    except TelegramBadRequest:
        pass

    await callback.answer("Запись отменена.")

    # Notify master
    if appt:
        dt_str = str(appt.get("appointment_time", ""))
        c_name = html.escape(str(appt.get('client_name') or 'Клиент'))
        c_srv = html.escape(str(appt.get('services') or ''))
        for admin_id in ADMIN_IDS:
            try:
                await callback.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "⚠️ <b>Клиент отменил запись!</b>\n\n"
                        f"👤 <b>Клиент:</b> {c_name}\n"
                        f"📱 <b>Телефон:</b> {appt.get('phone')}\n"
                        f"✂️ <b>Услуги:</b> {c_srv}\n"
                        f"📅 <b>Время:</b> {dt_str}\n"
                        "Слот снова свободен для других клиентов."
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("Failed to notify admin %s: %s", admin_id, e)


@router.message(lambda msg: bool(msg.text and is_phone_number(msg.text)))
async def handle_text_phone(message: Message):
    if not message.from_user or not message.text:
        return
    await _process_phone_provided(message, message.from_user.id, message.text)
