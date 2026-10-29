"""Client-facing "My appointments" section (Telegram)."""

import asyncio
import datetime
import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from handlers.ui import edit_or_send as _edit_or_send

from config import SALON_ADDRESS
from services.appointments import (
    SARATOV_TZ,
    cancel_appointment,
    get_user_upcoming_appointments,
    reschedule_appointment,
)
from services.masters import get_schedule

logger = logging.getLogger(__name__)
router = Router()

# appointment id pending reschedule: telegram user id -> appt id
user_resched_appt: dict[int, int] = {}


def _fmt_dt(dt_val) -> str:
    if isinstance(dt_val, str):
        try:
            dt_val = datetime.datetime.fromisoformat(dt_val)
        except Exception:
            return dt_val
    if dt_val.tzinfo is None:
        dt_val = dt_val.replace(tzinfo=SARATOV_TZ)
    return dt_val.astimezone(SARATOV_TZ).strftime("%d.%m.%Y в %H:%M")


def _appt_text(appt: dict) -> str:
    return (
        f"📅 <b>{_fmt_dt(appt['appointment_time'])}</b>\n"
        f"✂️ {html.escape(str(appt.get('services') or 'Стрижка'))} — {appt.get('total_price') or 0} ₽\n"
        f"📍 {html.escape(SALON_ADDRESS)}"
    )


def _my_appts_keyboard(appts: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for a in appts:
        rows.append([
            InlineKeyboardButton(
                text=f"✏️ Перенести ({_fmt_dt(a['appointment_time'])})",
                callback_data=f"my:resched:{a['id']}",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="❌ Отменить запись",
                callback_data=f"appt:cancel:{a['id']}",
            )
        ])
    rows.append([InlineKeyboardButton(text="✂️ Записаться ещё", callback_data="menu:booking")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_my_appointments(target: Message | CallbackQuery, user_id: int):
    appts = await asyncio.to_thread(get_user_upcoming_appointments, "telegram", user_id)
    if not appts:
        text = (
            "📋 <b>У вас нет активных записей.</b>\n\n"
            "Запишитесь на удобное время — и запись появится здесь."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✂️ Записаться", callback_data="menu:booking")]]
        )
    else:
        blocks = "\n\n".join(
            f"#{a['id']}. {_appt_text(a)}" for a in appts
        )
        text = f"📋 <b>Ваши записи:</b>\n\n{blocks}"
        kb = _my_appts_keyboard(appts)

    if isinstance(target, CallbackQuery):
        await _edit_or_send(target.message, text, reply_markup=kb, parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(F.text.in_({"📋 Мои записи", "Мои записи"}))
@router.callback_query(F.data.in_({"menu:my_appts", "my:list"}))
async def my_appointments(event: Message | CallbackQuery):
    from_user = event.from_user
    if not from_user:
        return
    await _show_my_appointments(event, from_user.id)


@router.callback_query(F.data.startswith("my:resched:"))
async def start_reschedule(callback: CallbackQuery):
    if not callback.from_user or not callback.data:
        return
    appt_id = int(callback.data.split(":")[2])
    user_resched_appt[callback.from_user.id] = appt_id

    current_services = {"hair"}
    schedule = await asyncio.to_thread(get_schedule, current_services)
    if not schedule:
        if callback.message:
            await _edit_or_send(callback.message, 
                "⚠️ Не удалось загрузить расписание. Попробуйте позже или отмените запись и запишитесь заново."
            )
        await callback.answer()
        return

    rows: list[list[InlineKeyboardButton]] = []
    day_items = list(schedule.values())[:8]
    for i in range(0, len(day_items), 2):
        row = []
        for d in day_items[i : i + 2]:
            row.append(
                InlineKeyboardButton(text=f"📅 {d['title']}", callback_data=f"my:resched_day:{d['day_key']}")
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Мои записи", callback_data="my:list")])

    if callback.message:
        try:
            await _edit_or_send(callback.message, 
                "🗓 <b>Перенос записи</b>\n\nВыберите новый день:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("my:resched_day:"))
async def reschedule_day(callback: CallbackQuery):
    if not callback.from_user or not callback.data:
        return
    day_key = callback.data.split(":")[2]

    schedule = await asyncio.to_thread(get_schedule, {"hair"})
    day_data = schedule.get(day_key)
    slots = day_data["slots"] if day_data else []
    title = day_data["title"] if day_data else day_key

    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(slots), 4):
        row = []
        for s in slots[i : i + 4]:
            row.append(
                InlineKeyboardButton(text=f"🕒 {s['time']}", callback_data=f"my:resched_slot:{s['timestamp']}")
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Выбрать другой день", callback_data="my:resched_back")])

    if not slots:
        rows = [[InlineKeyboardButton(text="◀️ Выбрать другой день", callback_data="my:resched_back")]]

    if callback.message:
        try:
            await _edit_or_send(callback.message, 
                f"🕒 <b>Свободные окна на {title}:</b>\n\nВыберите новое время:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
    await callback.answer()


@router.callback_query(F.data == "my:resched_back")
async def reschedule_back(callback: CallbackQuery):
    if not callback.from_user:
        return
    await start_reschedule(callback)


@router.callback_query(F.data.startswith("my:resched_slot:"))
async def reschedule_slot(callback: CallbackQuery):
    if not callback.from_user or not callback.data:
        return
    user_id = callback.from_user.id
    appt_id = user_resched_appt.get(user_id)
    if not appt_id:
        await callback.answer("Сессия переноса устарела, начните заново.", show_alert=True)
        await _show_my_appointments(callback, user_id)
        return

    timestamp = int(callback.data.split(":")[2])
    new_dt = datetime.datetime.fromtimestamp(timestamp, tz=SARATOV_TZ)

    ok, msg, appt = await asyncio.to_thread(
        lambda: reschedule_appointment(appt_id, new_dt, platform="telegram", platform_user_id=user_id)
    )
    user_resched_appt.pop(user_id, None)

    if not ok:
        if callback.message:
            await _edit_or_send(callback.message, f"⚠️ {html.escape(msg)}")
        await callback.answer()
        return

    text = (
        "✅ <b>Запись перенесена!</b>\n\n"
        f"{_appt_text(appt)}\n\n"
        "Напомним накануне вечером и за 2 часа до визита."
    )
    if callback.message:
        try:
            await _edit_or_send(callback.message, 
                text,
                reply_markup=_my_appts_keyboard([appt]),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
    await callback.answer("Запись перенесена!")

    # Notify master
    for admin_id in _admin_ids():
        try:
            await callback.bot.send_message(
                chat_id=admin_id,
                text=(
                    "🗓 <b>Клиент перенёс запись!</b>\n\n"
                    f"👤 Клиент: {html.escape(str(appt.get('client_name') or 'Клиент'))}\n"
                    f"📱 Телефон: {appt.get('phone') or '—'}\n"
                    f"✂️ Услуги: {html.escape(str(appt.get('services') or ''))}\n"
                    f"⏰ Новое время: {_fmt_dt(appt['appointment_time'])}"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Failed to notify admin %s about reschedule: %s", admin_id, e)


def _admin_ids() -> list[int]:
    from config import ADMIN_IDS

    return list(ADMIN_IDS)
