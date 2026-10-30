import asyncio
import datetime
import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from handlers.ui import edit_or_send as _edit_or_send

from config import ADMIN_IDS, BOOKING_URL, WEB_APP_URL
from services.appointments import (
    SARATOV_TZ,
    SlotUnavailableError,
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
)
from keyboards.menu import get_main_menu
from services.booking import (
    calculate_services,
    get_schedule,
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


@router.message(Command("site", "website", "app"))
@router.message(F.text.in_({"🌐 Наш сайт / Запись", "🌐 Наш сайт", "Наш сайт", "Сайт"}))
async def open_site_handler(message: Message):
    url = WEB_APP_URL or BOOKING_URL
    text = (
        "🌐 <b>Онлайн-запись и сайт мастера Бориса Гафурова</b>\n\n"
        "Вы можете записаться через наш сайт, выбрать удобное время или использовать удобный Мини-Апп:\n"
        f"🔗 {url}"
    )
    await message.answer(text, reply_markup=get_booking_keyboard(), parse_mode="HTML")


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
            await _edit_or_send(callback.message, 
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
            await _edit_or_send(callback.message, 
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
            await _edit_or_send(
                callback.message, 
                "⚠️ Не удалось загрузить расписание.\n\n"
                "Вы можете выбрать удобное время на сайте «Срез»:",
                reply_markup=get_booking_keyboard(),
            )
        await callback.answer()
        return

    calc = calculate_services(current_services)
    try:
        if callback.message:
            await _edit_or_send(
                callback.message, 
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
            await _edit_or_send(
                callback.message, 
                f"🕒 <b>Свободные окна на {title}:</b>\n\n"
                "Выберите удобное время начала стрижки:",
                reply_markup=get_slots_keyboard(slots),
                parse_mode="HTML",
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


async def _create_booking_and_notify(
    bot,
    user_id: int,
    phone: str,
    timestamp: int,
    selected_services: list[str] | set[str],
    client_name: str,
    target_msg_or_cb,
) -> int | None:
    """Creates the booking in the shared schedule and shows the confirmation."""
    calc = calculate_services(selected_services)
    dt = datetime.datetime.fromtimestamp(timestamp, tz=SARATOV_TZ)

    try:
        appt_id = await asyncio.to_thread(
            create_or_update_appointment,
            phone=phone,
            appointment_time=dt,
            services=", ".join(calc["names"]),
            total_price=calc["total_price"],
            client_name=client_name,
            platform="telegram",
            platform_user_id=user_id,
            duration_min=calc["duration_minutes"],
        )
    except SlotUnavailableError:
        text = (
            "⚠️ <b>Это время уже занято.</b>\n\n"
            "Пожалуйста, выберите другой слот или запишитесь через сайт «Срез»:"
        )
        if isinstance(target_msg_or_cb, CallbackQuery):
            if target_msg_or_cb.message:
                await _edit_or_send(
                    target_msg_or_cb.message,
                    text,
                    reply_markup=get_booking_keyboard(),
                    parse_mode="HTML",
                )
            await target_msg_or_cb.answer()
        else:
            await target_msg_or_cb.answer(
                text,
                reply_markup=get_booking_keyboard(),
                parse_mode="HTML",
            )
        return None

    user_selected_slot.pop(user_id, None)

    cancel_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Мои записи",
                    callback_data="my:list",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить запись",
                    callback_data=f"appt:cancel:{appt_id}",
                )
            ],
        ]
    )

    text = (
        f"🎉 <b>Запись успешно подтверждена!</b>\n\n"
        f"✂️ <b>Услуги:</b> {', '.join(calc['names'])}\n"
        f"💰 <b>Стоимость:</b> {calc['total_price']} ₽ ({calc['duration_text']})\n"
        f"📅 <b>Дата и время:</b> {dt.strftime('%d.%m.%Y в %H:%M')}\n"
        f"📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
        "Ждём вас! Мы пришлём напоминание накануне вечером и за 2 часа до визита.\n\n"
        "💡 Все свои записи вы всегда найдёте в разделе «📋 Мои записи» в меню — "
        "там можно посмотреть детали, перенести или отменить визит."
    )

    if isinstance(target_msg_or_cb, CallbackQuery):
        await _edit_or_send(
            target_msg_or_cb.message,
            text,
            reply_markup=cancel_markup,
            parse_mode="HTML",
        )
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

    return appt_id


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

    current_services = user_selected_services.get(user_id, {"hair"})
    client_name = callback.from_user.full_name or "Клиент"
    await _create_booking_and_notify(
        callback.bot,
        user_id,
        phone,
        timestamp,
        current_services,
        client_name,
        callback,
    )


@router.callback_query(F.data == "book:cancel")
async def cancel_booking(callback: CallbackQuery):
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    user_selected_slot.pop(user_id, None)

    try:
        if callback.message:
            await _edit_or_send(callback.message, "Запись отменена.")
    except TelegramBadRequest:
        pass
    await callback.answer("Запись отменена.")


async def _process_phone_provided(message: Message, user_id: int, phone_raw: str):
    from handlers.referrals import _has_active_admin_draft

    if _has_active_admin_draft(message):
        return

    success, link_msg = link_user_phone(user_id, phone_raw, platform="telegram")
    if not success:
        await message.answer(link_msg or "Пожалуйста, введите корректный номер телефона (например, +79991234567).")
        return

    pending_ts = user_selected_slot.pop(user_id, None)
    if pending_ts:
        eff_phone = get_user_phone(user_id, platform="telegram") or normalize_phone(phone_raw)
        current_services = user_selected_services.get(user_id, {"hair"})
        dt = datetime.datetime.fromtimestamp(pending_ts, tz=SARATOV_TZ)

        await message.answer(
            f"⏳ Номер <code>{eff_phone}</code> сохранён. Оформляю запись на <b>{dt.strftime('%d.%m.%Y в %H:%M')}</b>...",
            parse_mode="HTML",
        )
        client_name = message.from_user.full_name if message.from_user else "Клиент"
        await _create_booking_and_notify(
            message.bot,
            user_id,
            eff_phone,
            pending_ts,
            current_services,
            client_name,
            message,
        )
        return

    user_selected_services[user_id] = {"hair"}
    await message.answer(
        f"✅ <b>{html.escape(link_msg)}</b>\n\n"
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
            await _edit_or_send(callback.message, 
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
                        ],
                        [
                            InlineKeyboardButton(
                                text="📋 Мои записи",
                                callback_data="my:list",
                            )
                        ],
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
            await _edit_or_send(callback.message, 
                "❌ <b>Запись успешно отменена</b>\n\n"
                "Будем рады видеть вас в другой раз! Чтобы выбрать новую дату, нажмите «✂️ Записаться».\n\n"
                "💡 Остальные ваши записи — в разделе «📋 Мои записи».",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📋 Мои записи", callback_data="my:list")],
                        [InlineKeyboardButton(text="✂️ Записаться", callback_data="menu:booking")],
                    ]
                ),
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
