import asyncio
import datetime
import html
import io
import logging
import re
from zoneinfo import ZoneInfo
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_IDS, REFERRAL_REWARD_TEXT
from services.appointments import (
    SARATOV_TZ,
    adjust_master_day_hours,
    cancel_appointment,
    complete_appointment,
    confirm_appointment_by_client,
    create_manual_appointment,
    get_appointment,
    get_appointments_counts,
    get_current_saratov_time,
    get_day_time_slots_status,
    get_master_appointments,
    get_master_day_schedule,
    get_upcoming_days_schedule,
    reschedule_appointment,
    set_master_day_schedule,
    update_appointment_price_and_service,
)
from services.client_import import (
    get_clients_page,
    get_clients_stats,
    import_clients_from_file,
    import_clients_from_text,
    update_appointment_client_details,
)
from services.masters import get_service_options_for_creation
from services.masters_sync import (
    confirm_master_login,
    disconnect_masters_session,
    get_active_masters_session,
    pending_master_logins,
    request_master_login,
    sync_masters_appointments,
)
from services.referrals import (
    build_referral_link,
    get_active_rewards,
    get_broadcast_recipients,
    get_pending_referrals,
    get_referral_stats,
    get_user_phone,
    is_admin_user,
    is_master_user,
    is_phone_number,
    mark_referral_visited,
    mark_reward_used,
    normalize_phone,
    save_user,
)

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 8
admin_broadcast_drafts: dict[int, dict] = {}
admin_create_drafts: dict[int, dict] = {}
admin_resched_drafts: dict[int, dict] = {}
admin_edit_client_drafts: dict[int, dict] = {}
admin_edit_price_drafts: dict[int, dict] = {}
admin_clients_state: dict[int, dict] = {}

RU_MONTHS = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def is_admin(user_id: int) -> bool:
    return is_master_user(user_id, platform="telegram")


def parse_to_saratov(val) -> datetime.datetime:
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=SARATOV_TZ)
        return val.astimezone(SARATOV_TZ)
    if isinstance(val, str):
        val_clean = val.replace("T", " ")
        if "." in val_clean:
            val_clean = val_clean.split(".")[0]
        try:
            dt = datetime.datetime.strptime(val_clean[:19], "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=SARATOV_TZ)
        except Exception:
            pass
    return get_current_saratov_time()


def format_dt(val) -> str:
    dt = parse_to_saratov(val)
    return f"{dt.day} {RU_MONTHS[dt.month]} в {dt.strftime('%H:%M')}"


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Календарь записей", callback_data="admin:appts:all:0")],
            [InlineKeyboardButton(text="➕ Новая запись мастера", callback_data="admin:create:start")],
            [InlineKeyboardButton(text="👥 База клиентов", callback_data="admin:clients:0")],
            [InlineKeyboardButton(text="🕒 Рабочий график", callback_data="admin:sched:0")],
            [InlineKeyboardButton(text="⏳ Ожидают подтверждения", callback_data="admin:pending:0")],
            [InlineKeyboardButton(text="💰 Активные бонусы", callback_data="admin:rewards:0")],
            [InlineKeyboardButton(text="🔄 Синхронизация Masters", callback_data="admin:masters")],
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin:broadcast")],
        ]
    )


def admin_appts_keyboard(
    appts: list[dict],
    current_filter: str,
    offset: int,
    total: int,
    counts: dict[str, int] | None = None,
) -> InlineKeyboardMarkup:
    rows = []

    if counts is None:
        try:
            counts = get_appointments_counts()
        except Exception:
            counts = {"today": 0, "tomorrow": 0, "pending": 0, "all": total}

    filters = [
        ("today", f"Сегодня ({counts['today']})"),
        ("tomorrow", f"Завтра ({counts['tomorrow']})"),
        ("pending", f"Ожидающие ({counts['pending']})"),
        ("all", f"Все ({counts['all']})"),
    ]
    f_row = []
    for f_key, f_title in filters:
        title = f"• {f_title} •" if f_key == current_filter else f_title
        f_row.append(InlineKeyboardButton(text=title, callback_data=f"admin:appts:{f_key}:0"))
    rows.append(f_row)

    for item in appts:
        dt = parse_to_saratov(item["appointment_time"])
        time_str = dt.strftime("%H:%M")
        if current_filter == "all":
            time_str = f"{dt.day} {RU_MONTHS[dt.month]} {time_str}"

        st = item.get("status", "confirmed")
        if st == "completed":
            badge = "✅"
        elif item.get("client_confirmed"):
            badge = "🟢"
        else:
            badge = "⏳"

        name = (item.get("client_name") or "Клиент")[:12]
        price = item.get("total_price") or 0
        btn_text = f"{badge} {time_str} · {name} ({price} ₽)"
        rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"admin:appt:{item['id']}:{current_filter}:{offset}")])

    if not appts:
        rows.append([InlineKeyboardButton(text="Нет записей в этой категории", callback_data="noop")])

    nav_row = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:appts:{current_filter}:{prev_offset}"))
    if offset + PAGE_SIZE < total:
        next_offset = offset + PAGE_SIZE
        nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin:appts:{current_filter}:{next_offset}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([
        InlineKeyboardButton(text="➕ Новая запись", callback_data="admin:create:start"),
        InlineKeyboardButton(text="⬅️ Главное меню", callback_data="admin:menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_appt_card_keyboard(appt: dict, current_filter: str, offset: int) -> InlineKeyboardMarkup:
    rows = []
    appt_id = appt["id"]
    status = appt.get("status", "confirmed")

    if status != "completed" and status != "cancelled":
        rows.append([
            InlineKeyboardButton(text="✂️ Пострижен (Завершить)", callback_data=f"admin:complete_appt:{appt_id}:{current_filter}:{offset}")
        ])
        if not appt.get("client_confirmed"):
            rows.append([
                InlineKeyboardButton(text="✅ Подтвердить запись", callback_data=f"admin:confirm_appt:{appt_id}:{current_filter}:{offset}")
            ])
        rows.append([
            InlineKeyboardButton(text="✏️ Изменить клиента (имя / тел.)", callback_data=f"admin:edit_client:{appt_id}:{current_filter}:{offset}"),
            InlineKeyboardButton(text="💰 Изменить цену / услугу", callback_data=f"admin:edit_price:{appt_id}:{current_filter}:{offset}"),
        ])
        rows.append([
            InlineKeyboardButton(text="🗓 Перенести", callback_data=f"admin:resched_start:{appt_id}:{current_filter}:{offset}"),
            InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"admin:cancel_appt:{appt_id}:{current_filter}:{offset}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="✏️ Изменить клиента (имя / тел.)", callback_data=f"admin:edit_client:{appt_id}:{current_filter}:{offset}"),
            InlineKeyboardButton(text="💰 Изменить цену / услугу", callback_data=f"admin:edit_price:{appt_id}:{current_filter}:{offset}"),
        ])

    rows.append([
        InlineKeyboardButton(text="⬅️ К списку записей", callback_data=f"admin:appts:{current_filter}:{offset}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_schedule_keyboard(days: list[dict], offset: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for d in days:
        status_txt = f"🟢 {d['start_time']} - {d['end_time']}" if d["is_working"] else "🔴 Выходной"
        rows.append([
            InlineKeyboardButton(
                text=f"{d['title']}: {status_txt}",
                callback_data=f"admin:sched_day:{d['day_key']}",
            )
        ])

    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:sched:{max(0, offset - 7)}"))
    if offset + 7 < total:
        nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin:sched:{offset + 7}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_sched_day_keyboard(sched: dict) -> InlineKeyboardMarkup:
    day_key = sched["day_key"]
    rows = []
    if sched["is_working"]:
        rows.append([InlineKeyboardButton(text="🔴 Сделать выходным днем", callback_data=f"admin:sched_toggle:{day_key}")])
        rows.append([
            InlineKeyboardButton(text="➖ Начало -1ч", callback_data=f"admin:sched_adj:{day_key}:start:-1"),
            InlineKeyboardButton(text="➕ Начало +1ч", callback_data=f"admin:sched_adj:{day_key}:start:1"),
        ])
        rows.append([
            InlineKeyboardButton(text="➖ Конец -1ч", callback_data=f"admin:sched_adj:{day_key}:end:-1"),
            InlineKeyboardButton(text="➕ Конец +1ч", callback_data=f"admin:sched_adj:{day_key}:end:1"),
        ])
    else:
        rows.append([InlineKeyboardButton(text="🟢 Сделать рабочим днем (10:00 - 20:00)", callback_data=f"admin:sched_toggle:{day_key}")])

    rows.append([InlineKeyboardButton(text="⬅️ К календарю графика", callback_data="admin:sched:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pending_referrals_keyboard(referrals: list[dict], offset: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for item in referrals:
        invited_full = f"{item['invited_first_name'] or ''} {item.get('invited_last_name') or ''}".strip() or "Без имени"
        referrer_full = f"{item['referrer_first_name'] or ''} {item.get('referrer_last_name') or ''}".strip() or "Без имени"
        invited_name = invited_full[:15]
        referrer_name = referrer_full[:15]
        text = f"✅ #{item['id']} · {invited_name} ← {referrer_name}"
        rows.append(
            [InlineKeyboardButton(text=text, callback_data=f"ref_visit:{item['id']}:{offset}")]
        )

    if not rows:
        rows = [[InlineKeyboardButton(text="Нет ожидающих рефералов", callback_data="noop")]]

    nav_row = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:pending:{prev_offset}"))
    if offset + PAGE_SIZE < total:
        next_offset = offset + PAGE_SIZE
        nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin:pending:{next_offset}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="⬅️ Главное меню админки", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def active_rewards_keyboard(rewards: list[dict], offset: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for item in rewards:
        full_name = f"{item['first_name'] or ''} {item.get('last_name') or ''}".strip() or "Без имени"
        name_str = full_name[:18]
        text = f"✂️ Списать #{item['id']} · {name_str} · {item['amount']} ₽"
        rows.append(
            [InlineKeyboardButton(text=text, callback_data=f"reward_use:{item['id']}:{offset}")]
        )

    if not rows:
        rows = [[InlineKeyboardButton(text="Нет активных бонусов", callback_data="noop")]]

    nav_row = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:rewards:{prev_offset}"))
    if offset + PAGE_SIZE < total:
        next_offset = offset + PAGE_SIZE
        nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin:rewards:{next_offset}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="⬅️ Главное меню админки", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("myid"))
async def my_id(message: Message):
    if not message.from_user:
        return
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("referral", "ref"))
@router.message(F.text.in_({"🎁 Пригласить друга", "Пригласить друга"}))
async def referrals(message: Message):
    if not message.from_user:
        return

    user = message.from_user
    save_user(user.id, user.username, user.first_name, user.last_name)

    referral_link = await build_referral_link(message.bot, user.id)
    stats = get_referral_stats(user.id)

    await message.answer(
        "🎁 <b>Приглашай друзей и получай бонусы!</b>\n\n"
        "Отправь свою персональную ссылку другу. "
        f"Когда он впервые придет на стрижку или уход, тебе начислится <b>{REFERRAL_REWARD_TEXT}</b>!\n\n"
        f"🔗 <b>Твоя реферальная ссылка:</b>\n<code>{referral_link}</code>\n\n"
        "📊 <b>Твоя статистика:</b>\n"
        f"• Переходов по ссылке: <b>{stats['started']}</b>\n"
        f"• Подтвержденных визитов: <b>{stats['confirmed']}</b>\n"
        f"• Доступных бонусов: <b>{stats['active_rewards_count']}</b> (на сумму <b>{stats['active_rewards_sum']} ₽</b>)\n\n"
        "<i>Бонусы суммируются: 300 ₽ за каждого друга. Их можно списать при оплате услуг мастера.</i>"
    )


@router.message(Command("admin"))
@router.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    await message.answer(
        "⚙️ <b>Панель мастера и администратора:</b>",
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:menu")
async def admin_menu(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    try:
        if callback.message:
            await callback.message.edit_text(
                "⚙️ <b>Панель мастера и администратора:</b>",
                reply_markup=admin_menu_keyboard(),
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("admin:appts:"))
async def admin_appts_list(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    current_filter = parts[2] if len(parts) > 2 else "all"
    offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    counts = get_appointments_counts()
    appts, total = get_master_appointments(date_filter=current_filter, limit=PAGE_SIZE, offset=offset)

    filter_names = {
        "today": "Сегодня",
        "tomorrow": "Завтра",
        "pending": "Ожидающие подтверждения",
        "all": "Все будущие",
    }
    title = filter_names.get(current_filter, current_filter)

    text = f"📋 <b>Календарь записей · {title}</b> ({total} всего):\n\n"
    if not appts:
        text += "<i>Записей не найдено.</i>"
    else:
        text += "<i>Нажмите на запись для просмотра деталей, подтверждения, завершения визита или переноса:</i>"

    try:
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=admin_appts_keyboard(appts, current_filter, offset, total, counts),
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("admin:appt:"))
async def admin_appt_detail(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    try:
        await callback.answer()
    except Exception:
        pass

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    appt = get_appointment(appt_id)
    if not appt:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    st = appt.get("status", "confirmed")
    if st == "completed":
        st_text = "✂️ Завершена (клиент пострижен)"
    elif st == "cancelled":
        st_text = "❌ Отменена"
    elif appt.get("client_confirmed"):
        st_text = "🟢 Подтверждена клиентом"
    else:
        st_text = "⏳ Ожидает подтверждения"

    phone = appt.get("phone") or "—"
    name = appt.get("client_name") or "Клиент"
    services = appt.get("services") or "—"
    price = appt.get("total_price") or 0
    time_fmt = format_dt(appt["appointment_time"])
    platform = appt.get("platform") or "telegram"
    masters_id = appt.get("masters_id") or "—"

    card_text = (
        f"📋 <b>Карточка записи #{appt['id']}</b>\n\n"
        f"👤 <b>Клиент:</b> {html.escape(name)}\n"
        f"📱 <b>Телефон:</b> <code>{phone}</code>\n"
        f"✂️ <b>Услуга:</b> {html.escape(services)}\n"
        f"💰 <b>Стоимость:</b> {price} ₽\n"
        f"⏰ <b>Дата и время:</b> {time_fmt}\n"
        f"📌 <b>Статус:</b> {st_text}\n"
        f"🌐 <b>Источник:</b> {platform.upper()}\n"
        f"🔄 <b>Masters ID:</b> <code>{masters_id}</code>"
    )

    try:
        if callback.message:
            await callback.message.edit_text(
                card_text,
                reply_markup=admin_appt_card_keyboard(appt, current_filter, offset),
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("admin:complete_appt:"))
async def admin_complete_appt_handler(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    ok, msg, appt, ref_notify = complete_appointment(appt_id)
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    if ref_notify:
        try:
            invited_name = html.escape(ref_notify["invited_first_name"])
            await callback.bot.send_message(
                ref_notify["referrer_telegram_id"],
                f"🎉 <b>Отличные новости!</b>\n"
                f"Твой друг <b>{invited_name}</b> посетил салон!\n"
                f"Тебе начислен бонус: <b>{ref_notify['amount']} ₽</b>.\n\n"
                f"💰 Доступный баланс бонусов: <b>{ref_notify['new_balance']} ₽</b>.\n"
                "Ты можешь списать их при следующем визите к мастеру!",
            )
        except Exception as e:
            logger.warning("Не удалось отправить уведомление рефереру: %s", e)

    await callback.answer(msg[:200], show_alert=False)
    # Refresh card
    updated_appt = get_appointment(appt_id)
    if updated_appt and callback.message:
        await admin_appt_detail(callback)


@router.callback_query(F.data.startswith("admin:confirm_appt:"))
async def admin_confirm_appt_handler(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    ok, msg, appt = confirm_appointment_by_client(appt_id)
    await callback.answer(msg, show_alert=False)
    await admin_appt_detail(callback)


@router.callback_query(F.data.startswith("admin:cancel_appt:"))
async def admin_cancel_appt_handler(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    ok, msg, appt = cancel_appointment(appt_id)
    if ok and appt and appt.get("platform_user_id") and appt.get("platform") == "telegram":
        try:
            await callback.bot.send_message(
                chat_id=appt["platform_user_id"],
                text="❌ <b>Ваша запись была отменена мастером.</b>\n\nЕсли у вас возникли вопросы или вы хотите подобрать другое время, напишите мастеру.",
            )
        except Exception as e:
            logger.warning("Failed to notify client about cancel: %s", e)

    await callback.answer("Запись отменена", show_alert=False)
    await admin_appts_list(callback)


# Reschedule flow
@router.callback_query(F.data.startswith("admin:resched_start:"))
async def admin_resched_start(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    now = get_current_saratov_time()
    rows = []
    for i in range(7):
        d = now + datetime.timedelta(days=i)
        day_key = d.strftime("%Y-%m-%d")
        title = f"{RU_WEEKDAYS[d.weekday()]}, {d.day} {RU_MONTHS[d.month]}"
        if i == 0:
            title += " (Сегодня)"
        elif i == 1:
            title += " (Завтра)"
        rows.append([
            InlineKeyboardButton(
                text=title,
                callback_data=f"admin:resched_day:{appt_id}:{day_key}:{current_filter}:{offset}",
            )
        ])

    rows.append([
        InlineKeyboardButton(text="⬅️ Назад к карточке", callback_data=f"admin:appt:{appt_id}:{current_filter}:{offset}")
    ])

    if callback.message:
        await callback.message.edit_text(
            f"🗓 <b>Перенос записи #{appt_id}</b>\n\nВыберите новую дату:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:resched_day:"))
async def admin_resched_day_chosen(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    day_key = parts[3]
    current_filter = parts[4] if len(parts) > 4 else "today"
    offset = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0

    # Times keyboard
    hours = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00", "20:00"]
    rows = []
    row = []
    for h in hours:
        row.append(InlineKeyboardButton(text=h, callback_data=f"admin:resched_time:{appt_id}:{day_key}:{h}:{current_filter}:{offset}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([
        InlineKeyboardButton(text="✏️ Ввести другое время (например 08:30)", callback_data=f"admin:resched_custom:{appt_id}:{day_key}:{current_filter}:{offset}")
    ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад к выбору даты", callback_data=f"admin:resched_start:{appt_id}:{current_filter}:{offset}")
    ])

    if callback.message:
        await callback.message.edit_text(
            f"🗓 <b>Перенос записи #{appt_id} на {day_key}</b>\n\nВыберите время или введите любое своё:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:resched_time:"))
async def admin_resched_time_chosen(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    day_key = parts[3]
    time_str = parts[4]
    current_filter = parts[5] if len(parts) > 5 else "today"
    offset = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0

    try:
        new_dt = datetime.datetime.strptime(f"{day_key} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SARATOV_TZ)
    except Exception as e:
        await callback.answer("Ошибка формата времени", show_alert=True)
        return

    ok, msg, appt = reschedule_appointment(appt_id, new_dt)
    if ok and appt and appt.get("platform_user_id") and appt.get("platform") == "telegram":
        try:
            await callback.bot.send_message(
                chat_id=appt["platform_user_id"],
                text=(
                    "🗓 <b>Ваша запись перенесена мастером!</b>\n\n"
                    f"⏰ Новое время: <b>{format_dt(new_dt)}</b>\n"
                    f"✂️ Услуга: {appt.get('services')}\n"
                    f"💰 Стоимость: {appt.get('total_price')} ₽\n\n"
                    "Ждем вас!"
                ),
            )
        except Exception as e:
            logger.warning("Failed to notify client about reschedule: %s", e)

    await callback.answer("Запись успешно перенесена!", show_alert=False)
    await admin_appt_detail(callback)


@router.callback_query(F.data.startswith("admin:resched_custom:"))
async def admin_resched_custom_start(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    day_key = parts[3]
    current_filter = parts[4] if len(parts) > 4 else "today"
    offset = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0

    admin_resched_drafts[callback.from_user.id] = {
        "appt_id": appt_id,
        "day_key": day_key,
        "current_filter": current_filter,
        "offset": offset,
        "step": "awaiting_custom_time",
    }

    if callback.message:
        await callback.message.edit_text(
            f"✏️ <b>Введите желаемое время на {day_key}</b>\n\n"
            "Напишите время в ответном сообщении (например <code>08:30</code> или <code>21:15</code>):",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin:appt:{appt_id}:{current_filter}:{offset}")]]
            ),
        )
    await callback.answer()


# Manual appointment creation flow
@router.callback_query(F.data == "admin:create:start")
async def admin_create_start(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    opts = await asyncio.to_thread(get_service_options_for_creation)
    admin_create_drafts[callback.from_user.id] = {
        "step": "service",
        "options": opts,
    }

    rows = []
    for idx, o in enumerate(opts):
        rows.append([
            InlineKeyboardButton(
                text=f"{o['name']} ({o['price']} ₽)",
                callback_data=f"admin:create:svc:{idx}",
            )
        ])
    rows.append([InlineKeyboardButton(text="✏️ Другая услуга / своя цена", callback_data="admin:create:svc:custom")])
    rows.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:menu")])

    if callback.message:
        await callback.message.edit_text(
            "➕ <b>Создание новой записи мастером</b>\n\n"
            "<b>Шаг 1 из 4:</b> Выберите услугу из Masters или введите другое название:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:create:svc:"))
async def admin_create_service_chosen(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    svc_code = parts[3]

    draft = admin_create_drafts.setdefault(callback.from_user.id, {})
    if svc_code == "custom":
        draft["step"] = "awaiting_custom_service"
        if callback.message:
            await callback.message.edit_text(
                "✏️ <b>Введите название услуги</b> в чат (например: <code>Моделирование бороды VIP</code>):",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:create:start")]]
                ),
                parse_mode="HTML",
            )
        await callback.answer()
        return

    opts = draft.get("options")
    if not opts:
        opts = await asyncio.to_thread(get_service_options_for_creation)
        draft["options"] = opts

    if svc_code.isdigit():
        idx = int(svc_code)
        if 0 <= idx < len(opts):
            selected = opts[idx]
            svc_name = selected["name"]
            price = selected["price"]
        else:
            svc_name = "Стрижка (МУЖСКАЯ)"
            price = 2300
    else:
        svc_name = svc_code
        price = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 2300

    draft["service"] = svc_name
    draft["price"] = price
    draft["step"] = "day"

    await show_create_day_picker(callback)


async def show_create_day_picker(callback: CallbackQuery):
    now = get_current_saratov_time()
    rows = []
    for i in range(7):
        d = now + datetime.timedelta(days=i)
        day_key = d.strftime("%Y-%m-%d")
        title = f"{RU_WEEKDAYS[d.weekday()]}, {d.day} {RU_MONTHS[d.month]}"
        if i == 0:
            title += " (Сегодня)"
        elif i == 1:
            title += " (Завтра)"
        rows.append([
            InlineKeyboardButton(
                text=title,
                callback_data=f"admin:create:day:{day_key}",
            )
        ])

    rows.append([InlineKeyboardButton(text="⬅️ Назад к услугам", callback_data="admin:create:start")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin:menu")])
    if callback.message:
        await callback.message.edit_text(
            "➕ <b>Шаг 2 из 4:</b> Выберите дату записи:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "admin:create:back_to_days")
async def admin_create_back_to_days(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await show_create_day_picker(callback)


@router.callback_query(F.data.startswith("admin:create:day:"))
async def admin_create_day_chosen(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    day_key = callback.data.split(":")[3]
    draft = admin_create_drafts.setdefault(callback.from_user.id, {})
    draft["day_key"] = day_key
    draft["step"] = "time"

    slots_info = await asyncio.to_thread(get_day_time_slots_status, day_key)
    free_slots = slots_info.get("free_slots", [])
    busy_appts = slots_info.get("busy_appts", [])

    lines = [f"➕ <b>Шаг 3 из 4: Выберите время на {day_key}</b>\n"]
    if free_slots:
        lines.append(f"🟢 <b>Свободные окна:</b> {', '.join(free_slots)}")
    else:
        lines.append("⚠️ <i>На этот день нет свободных окон в Masters</i>")

    if busy_appts:
        lines.append("\n🔴 <b>Уже занято в расписании:</b>")
        for b in busy_appts:
            c_name = b["client_name"] or "Клиент"
            lines.append(f"• <b>{b['time']}</b> — {c_name} (<i>{b['services']}</i>)")

    lines.append("\n<i>Выберите свободное окно 🟢 или занятое 🔴 для овербукинга:</i>")
    text = "\n".join(lines)

    rows = []
    # 1. Free slots in rows of 3
    if free_slots:
        row = []
        for s in free_slots:
            row.append(InlineKeyboardButton(text=f"🟢 {s}", callback_data=f"admin:create:time:{s}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    # 2. Busy appointments buttons
    if busy_appts:
        row = []
        for b in busy_appts:
            row.append(InlineKeyboardButton(text=f"🔴 {b['time']} (занято)", callback_data=f"admin:create:busy:{b['time']}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    rows.append([
        InlineKeyboardButton(text="✏️ Любое другое время (08:30, 22:00...)", callback_data="admin:create:time:custom")
    ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к датам", callback_data="admin:create:back_to_days")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin:menu")])

    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:create:busy:"))
async def admin_create_busy_clicked(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    time_val = callback.data.split(":")[3]
    draft = admin_create_drafts.setdefault(callback.from_user.id, {})
    day_key = draft.get("day_key", "")

    slots_info = await asyncio.to_thread(get_day_time_slots_status, day_key)
    b_appt = next((b for b in slots_info.get("busy_appts", []) if b["time"] == time_val), None)
    details = f"{b_appt['client_name']} ({b_appt['services']})" if b_appt else "запись из Masters"

    text = (
        f"⚠️ <b>Внимание: на {day_key} в {time_val} уже есть запись!</b>\n\n"
        f"• <b>{time_val}</b> — {details}\n\n"
        "Вы действительно хотите записать ещё одного клиента на это же время (овербукинг)?"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⚠️ Да, записать на {time_val}", callback_data=f"admin:create:time:{time_val}")],
            [InlineKeyboardButton(text="⬅️ Выбрать другое время", callback_data=f"admin:create:day:{day_key}")],
        ]
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:create:time:"))
async def admin_create_time_chosen(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    time_val = callback.data.split(":")[3]
    draft = admin_create_drafts.setdefault(callback.from_user.id, {})
    if time_val == "custom":
        draft["step"] = "awaiting_custom_time"
        if callback.message:
            await callback.message.edit_text(
                f"✏️ <b>Введите любое время записи</b> на {draft.get('day_key')}\n\n"
                "Напишите в ответном сообщении (например <code>08:30</code> или <code>22:15</code>):",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:create:start")]]
                ),
            )
        await callback.answer()
        return

    draft["time_str"] = time_val
    draft["step"] = "awaiting_client_info"

    curr_price = draft.get("price", 2300)
    curr_svc = draft.get("service", "Стрижка (МУЖСКАЯ)")
    day_key = draft.get("day_key", "")

    if callback.message:
        await callback.message.edit_text(
            f"➕ <b>Шаг 4 из 4: Данные клиента и цена</b>\n\n"
            f"✂️ Услуга: <b>{curr_svc}</b>\n"
            f"⏰ Время: <b>{day_key} {time_val}</b>\n"
            f"💰 Цена: <b>{curr_price} ₽</b>\n\n"
            "👉 <b>Отправьте в чат телефон и имя клиента:</b>\n"
            "Например: <code>+79991234567 Иван</code> или просто номер <code>89991234567</code>.\n\n"
            "<i>Если хотите изменить цену, укажите её в конце сообщения, например: <code>89991234567 Иван 1200</code></i>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:menu")]]
            ),
        )
    await callback.answer()


# Schedule management flow
@router.callback_query(F.data.startswith("admin:sched:"))
async def admin_sched_list(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    offset = int(callback.data.split(":")[2])
    all_days = get_upcoming_days_schedule(days_ahead=14)
    slice_days = all_days[offset:offset+7]

    text = (
        "🕒 <b>Управление рабочим графиком мастера</b>\n\n"
        "Выберите день, чтобы сделать его выходным/рабочим или изменить часы работы (прибавить/убавить начало и конец дня):"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=admin_schedule_keyboard(slice_days, offset, len(all_days)),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:sched_day:"))
async def admin_sched_day_detail(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    day_key = callback.data.split(":")[2]
    sched = get_master_day_schedule(day_key)

    st_txt = "🟢 Рабочий день" if sched["is_working"] else "🔴 Выходной день"
    card_text = (
        f"🗓 <b>График на {day_key}:</b>\n\n"
        f"• Статус: <b>{st_txt}</b>\n"
        f"• Рабочие часы: <b>{sched['start_time']} — {sched['end_time']}</b>\n\n"
        "Вы можете изменить статус дня или скорректировать рабочие часы:"
    )

    if callback.message:
        await callback.message.edit_text(
            card_text,
            reply_markup=admin_sched_day_keyboard(sched),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:sched_toggle:"))
async def admin_sched_toggle(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    day_key = callback.data.split(":")[2]
    sched = get_master_day_schedule(day_key)
    new_work = not sched["is_working"]
    set_master_day_schedule(day_key, new_work, sched["start_time"], sched["end_time"])

    msg = "День отмечен как РАБОЧИЙ!" if new_work else "День отмечен как ВЫХОДНОЙ!"
    await callback.answer(msg, show_alert=False)
    await admin_sched_day_detail(callback)


@router.callback_query(F.data.startswith("admin:sched_adj:"))
async def admin_sched_adj(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    day_key = parts[2]
    which = parts[3]
    delta = int(parts[4])

    if which == "start":
        adjust_master_day_hours(day_key, start_delta_hours=delta, end_delta_hours=0)
    else:
        adjust_master_day_hours(day_key, start_delta_hours=0, end_delta_hours=delta)

    await callback.answer("Часы обновлены", show_alert=False)
    await admin_sched_day_detail(callback)


# Referrals & Rewards
@router.callback_query(F.data.startswith("admin:pending"))
async def admin_pending(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    referrals_list, total = get_pending_referrals(limit=PAGE_SIZE, offset=offset)

    count_info = f" ({total} всего)" if total > 0 else ""
    try:
        if callback.message:
            await callback.message.edit_text(
                f"⏳ <b>Ожидают подтверждения визита{count_info}:</b>\n"
                "<i>(нажми на нужного клиента, чтобы подтвердить его визит и начислить бонус другу)</i>",
                reply_markup=pending_referrals_keyboard(referrals_list, offset, total),
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("admin:rewards"))
async def admin_rewards(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    rewards_list, total = get_active_rewards(limit=PAGE_SIZE, offset=offset)

    count_info = f" ({total} всего)" if total > 0 else ""
    try:
        if callback.message:
            await callback.message.edit_text(
                f"💰 <b>Активные бонусы клиентов{count_info}:</b>\n"
                "<i>(нажми на бонус, чтобы списать его при оплате клиентом)</i>",
                reply_markup=active_rewards_keyboard(rewards_list, offset, total),
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("ref_visit:"))
async def confirm_visit(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await callback.answer("Некорректный ID", show_alert=True)
        return

    referral_id = int(parts[1])
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    success, text, notify_data = mark_referral_visited(referral_id)

    if success and notify_data:
        try:
            invited_name = html.escape(notify_data["invited_first_name"])
            await callback.bot.send_message(
                notify_data["referrer_telegram_id"],
                f"🎉 <b>Отличные новости!</b>\n"
                f"Твой друг <b>{invited_name}</b> посетил салон!\n"
                f"Тебе начислен бонус: <b>{notify_data['amount']} ₽</b>.\n\n"
                f"💰 Доступный баланс бонусов: <b>{notify_data['new_balance']} ₽</b>.\n"
                "Ты можешь списать их при следующем визите к мастеру!",
            )
        except Exception as e:
            logger.warning("Не удалось отправить уведомление рефереру: %s", e)

    referrals_list, total = get_pending_referrals(limit=PAGE_SIZE, offset=offset)

    safe_text = html.escape(text)
    try:
        if callback.message:
            await callback.message.edit_text(
                f"ℹ️ {safe_text}\n\n⏳ <b>Ожидают подтверждения:</b>",
                reply_markup=pending_referrals_keyboard(referrals_list, offset, total),
            )
    except TelegramBadRequest:
        pass

    await callback.answer(text[:200], show_alert=False)


@router.callback_query(F.data.startswith("reward_use:"))
async def use_reward(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await callback.answer("Некорректный ID", show_alert=True)
        return

    reward_id = int(parts[1])
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    success, text, notify_data = mark_reward_used(reward_id)

    if success and notify_data:
        try:
            await callback.bot.send_message(
                notify_data["user_telegram_id"],
                f"✂️ Бонус <b>{notify_data['amount']} ₽</b> успешно списан в счет оплаты визита!\n"
                f"💰 Оставшийся баланс бонусов: <b>{notify_data['remaining_balance']} ₽</b>.\n\n"
                "Спасибо за визит, ждем снова!",
            )
        except Exception as e:
            logger.warning("Не удалось отправить уведомление клиенту о списании: %s", e)

    rewards_list, total = get_active_rewards(limit=PAGE_SIZE, offset=offset)

    safe_text = html.escape(text)
    try:
        if callback.message:
            await callback.message.edit_text(
                f"ℹ️ {safe_text}\n\n💰 <b>Активные бонусы клиентов:</b>",
                reply_markup=active_rewards_keyboard(rewards_list, offset, total),
            )
    except TelegramBadRequest:
        pass

    await callback.answer(text[:200], show_alert=False)


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


# Masters integration
@router.callback_query(F.data == "admin:masters")
async def admin_masters_status(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    session = get_active_masters_session()
    if session:
        text = (
            "✅ <b>Синхронизация Masters подключена</b>\n\n"
            f"📱 Номер мастера: <code>{session['master_phone']}</code>\n"
            "Записи из Masters автоматически синхронизируются каждые 10 минут, "
            "а клиенты получают напоминания о стрижке накануне вечером и за 2 часа.\n\n"
            "Вы можете запустить принудительную синхронизацию прямо сейчас:"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Синхронизировать сейчас", callback_data="admin:masters_sync")],
                [InlineKeyboardButton(text="🚪 Отключить аккаунт Masters", callback_data="admin:masters_logout")],
                [InlineKeyboardButton(text="⬅️ Меню админки", callback_data="admin:menu")],
            ]
        )
    else:
        text = (
            "🔗 <b>Подключение аккаунта Masters</b>\n\n"
            "Бот может автоматически забирать все записи из твоего личного кабинета Masters "
            "и бесплатно рассылать клиентам напоминания о визите накануне вечером и за 2 часа.\n\n"
            "Нажми кнопку ниже, чтобы отправить проверочный код на твой номер:"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📲 Запросить СМС-код для входа", callback_data="admin:masters_request")],
                [InlineKeyboardButton(text="⬅️ Меню админки", callback_data="admin:menu")],
            ]
        )

    try:
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "admin:masters_request")
async def admin_masters_request_code(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = callback.from_user.id
    phone = get_user_phone(user_id, platform="telegram")

    if not phone:
        pending_master_logins[user_id] = {"awaiting_phone": True}
        try:
            if callback.message:
                await callback.message.edit_text(
                    "📱 <b>Введите номер телефона мастера</b>\n\n"
                    "Напишите номер, зарегистрированный в сервисе Masters (например, <code>+79991234567</code>):",
                    parse_mode="HTML",
                )
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

    await callback.answer("Запрашиваю код в Masters...")
    success, msg, session_data = request_master_login(phone)

    if not success:
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ <b>{msg}</b>\n\nПопробуйте ещё раз:",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:masters")]]
                    ),
                    parse_mode="HTML",
                )
        except TelegramBadRequest:
            pass
        return

    pending_master_logins[user_id] = session_data
    try:
        if callback.message:
            await callback.message.edit_text(
                f"📩 <b>Код подтверждения отправлен</b>\n\n"
                f"На номер <code>{session_data['phone_formatted']}</code> отправлено СМС от Masters.\n\n"
                "👉 <b>Напишите 4 цифры из СМС в ответном сообщении:</b>",
                parse_mode="HTML",
            )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "admin:masters_sync")
async def admin_masters_sync_now(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await callback.answer("Синхронизирую записи...")
    ok, count, msg = sync_masters_appointments()
    await callback.answer(msg, show_alert=True)


@router.callback_query(F.data == "admin:masters_logout")
async def admin_masters_disconnect(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    disconnect_masters_session()
    await callback.answer("Синхронизация Masters отключена.", show_alert=True)
    await admin_masters_status(callback)


# Broadcast flow
@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    admin_id = callback.from_user.id
    admin_broadcast_drafts[admin_id] = {"step": "awaiting_text"}

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:broadcast:cancel")]
        ]
    )
    text = (
        "📢 <b>Рассылка сообщений клиентам (Telegram)</b>\n\n"
        "Напишите и отправьте в чат текст, который хотите разослать всем клиентам.\n"
        "<i>(Можно использовать смайлы и форматирование: жирный, курсив и т.д.)</i>\n\n"
        "После отправки бот покажет предпросмотр и попросит подтвердить рассылку перед отправкой."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin:broadcast:cancel")
async def admin_broadcast_cancel(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    admin_broadcast_drafts.pop(callback.from_user.id, None)
    await callback.answer("Рассылка отменена.")
    await admin_menu(callback)


@router.callback_query(F.data == "admin:broadcast:send")
async def admin_broadcast_send(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    admin_id = callback.from_user.id
    draft = admin_broadcast_drafts.pop(admin_id, None)
    if not draft or not draft.get("text"):
        await callback.answer("Черновик рассылки не найден.", show_alert=True)
        return

    text = draft["text"]
    recipients = get_broadcast_recipients("telegram")
    total = len(recipients)

    if callback.message:
        await callback.message.edit_text(f"⏳ <b>Отправка рассылки...</b>\n0 из {total}", parse_mode="HTML")
    await callback.answer()

    client_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✂️ Записаться онлайн", callback_data="menu:booking")]
        ]
    )

    success_count = 0
    fail_count = 0

    for uid in recipients:
        try:
            await callback.bot.send_message(
                chat_id=uid,
                text=text,
                reply_markup=client_kb,
                parse_mode="HTML",
            )
            success_count += 1
        except Exception as e:
            logger.warning("Failed broadcast to %s: %s", uid, e)
            fail_count += 1
        await asyncio.sleep(0.05)

    report = (
        "✅ <b>Рассылка успешно завершена!</b>\n\n"
        f"📬 <b>Доставлено:</b> {success_count}\n"
        f"⚠️ <b>Не доставлено (бот заблокирован):</b> {fail_count}\n"
        f"👥 <b>Всего получателей:</b> {total}"
    )
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Меню админки", callback_data="admin:menu")]
        ]
    )
    if callback.message:
        await callback.message.edit_text(report, reply_markup=back_kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin:clients:"))
async def admin_clients_list(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[2] if len(parts) > 2 else "0"

    if action == "import":
        admin_clients_state[callback.from_user.id] = {"action": "awaiting_import"}
        text = (
            "📥 <b>Импорт базы клиентов из Masters</b>\n\n"
            "Вы можете выгрузить базу клиентов из Masters и загрузить в бота:\n"
            "1. В приложении или на сайте Masters: <b>Клиенты ➔ Экспорт базы (в Excel / CSV)</b>.\n"
            "2. <b>Отправьте полученный файл (.xlsx или .csv) прямо в этот чат</b> как документ.\n\n"
            "<i>Либо просто отправьте список клиентов сообщением в чат в формате:\n"
            "<code>Иван Иванов +79271234567\n"
            "Алексей 89053214455</code></i>"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к базе", callback_data="admin:clients:0")]]
        )
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    if action == "search":
        admin_clients_state[callback.from_user.id] = {"action": "awaiting_search"}
        text = (
            "🔍 <b>Поиск клиента в базе</b>\n\n"
            "Отправьте в чат имя или номер телефона клиента для поиска:"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к базе", callback_data="admin:clients:0")]]
        )
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    offset = int(action) if action.isdigit() else 0
    stats = get_clients_stats()
    clients, total = get_clients_page(offset=offset, limit=PAGE_SIZE)

    text = (
        f"👥 <b>База клиентов</b> ({stats['total']} всего, с телефонами: {stats['with_phone']}):\n\n"
    )
    if not clients:
        text += "<i>В базе пока нет сохранённых клиентов.\nНажмите кнопку «📥 Импорт из Masters», чтобы загрузить выгрузку Excel / CSV или список контактов.</i>"
    else:
        for c in clients:
            c_name = f"{c['first_name'] or ''} {c['last_name'] or ''}".strip() or "Без имени"
            c_phone = c.get("phone") or "—"
            text += f"• <b>{html.escape(c_name)}</b> — <code>{c_phone}</code>\n"

    rows = []
    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:clients:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin:clients:{offset + PAGE_SIZE}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([
        InlineKeyboardButton(text="📥 Импорт из Masters (Excel / CSV)", callback_data="admin:clients:import"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔍 Поиск клиента", callback_data="admin:clients:search"),
        InlineKeyboardButton(text="⬅️ Меню админки", callback_data="admin:menu"),
    ])

    if callback.message:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:edit_client:"))
async def admin_edit_client_start(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    appt = get_appointment(appt_id)
    if not appt:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    admin_edit_client_drafts[callback.from_user.id] = {
        "appt_id": appt_id,
        "current_filter": current_filter,
        "offset": offset,
    }

    cur_name = appt.get("client_name") or "Клиент"
    cur_phone = appt.get("phone") or "—"

    text = (
        f"✏️ <b>Изменение данных клиента для записи #{appt_id}</b>\n\n"
        f"Текущее имя: <b>{html.escape(cur_name)}</b>\n"
        f"Текущий телефон: <code>{cur_phone}</code>\n\n"
        "👉 <b>Отправьте в чат имя и/или номер телефона клиента:</b>\n"
        "Например: <code>Иван +79271234567</code> или просто номер <code>+79271234567</code>, или имя <code>Иван</code>\n\n"
        "<i>Запись мгновенно обновится в базе данных.</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin:appt:{appt_id}:{current_filter}:{offset}")]]
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:edit_price:"))
async def admin_edit_price_start(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    appt = get_appointment(appt_id)
    if not appt:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    cur_svc = appt.get("services") or "—"
    cur_price = appt.get("total_price") or 0
    cur_client = appt.get("client_name") or "Клиент"

    text = (
        f"💰 <b>Редактирование услуги и стоимости для записи #{appt_id}</b>\n\n"
        f"👤 Клиент: <b>{html.escape(cur_client)}</b>\n"
        f"✂️ Текущая услуга: <b>{html.escape(cur_svc)}</b>\n"
        f"💵 Текущая стоимость: <b>{cur_price} ₽</b>\n\n"
        "👉 <b>Выберите реальную услугу с ценой в 1 клик или введите любую свою цену:</b>"
    )

    rows = [
        [InlineKeyboardButton(text="💈 Стрижка (МУЖСКАЯ) — 2 300 ₽", callback_data=f"admin:set_price:{appt_id}:2300:hair:{current_filter}:{offset}")],
        [InlineKeyboardButton(text="🧔 Оформление бороды — 700 ₽", callback_data=f"admin:set_price:{appt_id}:700:beard:{current_filter}:{offset}")],
        [InlineKeyboardButton(text="💈+🧔 Стрижка + Борода — 3 000 ₽", callback_data=f"admin:set_price:{appt_id}:3000:combo:{current_filter}:{offset}")],
        [InlineKeyboardButton(text="✏️ Ввести свою цену (например 1500)", callback_data=f"admin:set_custom_price:{appt_id}:{current_filter}:{offset}")],
        [InlineKeyboardButton(text="⬅️ Назад к карточке", callback_data=f"admin:appt:{appt_id}:{current_filter}:{offset}")],
    ]

    if callback.message:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:set_price:"))
async def admin_set_price_quick(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    price = int(parts[3])
    svc_code = parts[4]
    current_filter = parts[5] if len(parts) > 5 else "today"
    offset = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0

    svc_map = {
        "hair": "Стрижка (МУЖСКАЯ)",
        "beard": "Оформление бороды",
        "combo": "Стрижка + Борода",
    }
    svc_name = svc_map.get(svc_code, "Стрижка")

    ok, msg, appt = update_appointment_price_and_service(appt_id, price, svc_name)
    await callback.answer(f"Стоимость обновлена: {price} ₽", show_alert=False)

    if callback.message:
        await admin_appt_detail(callback)


@router.callback_query(F.data.startswith("admin:set_custom_price:"))
async def admin_set_custom_price_start(callback: CallbackQuery):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    admin_edit_price_drafts[callback.from_user.id] = {
        "appt_id": appt_id,
        "current_filter": current_filter,
        "offset": offset,
    }

    text = (
        f"✏️ <b>Ввод своей стоимости для записи #{appt_id}</b>\n\n"
        "Отправьте в чат новую стоимость в рублях (например: <code>1500</code> или <code>2000</code>):"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admin:edit_price:{appt_id}:{current_filter}:{offset}")]]
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(F.document)
async def admin_document_handler(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    doc = message.document
    if not doc:
        return

    user_id = message.from_user.id
    fn = doc.file_name or "file.xlsx"
    if not fn.lower().endswith((".xlsx", ".csv", ".tsv", ".txt")):
        await message.answer("⚠️ Пожалуйста, отправьте файл выгрузки Masters в формате <code>.xlsx</code> или <code>.csv</code>.")
        return

    status_msg = await message.answer("⏳ Читаю и обрабатываю файл выгрузки клиентов из Masters...")
    try:
        file = await message.bot.get_file(doc.file_id)
        file_io = io.BytesIO()
        await message.bot.download_file(file.file_path, file_io)
        file_bytes = file_io.getvalue()

        added, updated, msg = import_clients_from_file(fn, file_bytes)
        admin_clients_state.pop(user_id, None)

        await status_msg.edit_text(
            f"✅ <b>Импорт клиентов завершён!</b>\n\n"
            f"• Добавлено новых клиентов: <b>{added}</b>\n"
            f"• Обновлено данных: <b>{updated}</b>\n"
            f"• {msg}\n\n"
            "Все клиенты успешно сохранены в базу данных бота.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="👥 Открыть базу клиентов", callback_data="admin:clients:0")],
                    [InlineKeyboardButton(text="📋 Календарь записей", callback_data="admin:appts:all:0")],
                ]
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Error importing clients from document: %s", e)
        await status_msg.edit_text(f"❌ Ошибка при чтении файла: {e}")


def _has_active_admin_draft(message: Message) -> bool:
    if not message.from_user:
        return False
    uid = message.from_user.id
    return (
        uid in pending_master_logins
        or uid in admin_broadcast_drafts
        or uid in admin_resched_drafts
        or uid in admin_create_drafts
        or uid in admin_edit_client_drafts
        or uid in admin_edit_price_drafts
        or uid in admin_clients_state
    )


# Message dispatcher for text inputs (SMS, Broadcast, Reschedule, Create appointment)
@router.message(F.text & ~F.text.startswith("/") & _has_active_admin_draft)
async def handle_admin_text_inputs(message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    raw_text = message.text.strip()

    # 1. Master SMS login
    if user_id in pending_master_logins and raw_text.isdigit() and len(raw_text) == 4:
        session_data = pending_master_logins.get(user_id)
        if session_data and not session_data.get("awaiting_phone"):
            success, msg = confirm_master_login(session_data, raw_text)
            if not success:
                await message.answer(f"❌ <b>{msg}</b>\n\nПопробуйте ввести код ещё раз:")
                return
            pending_master_logins.pop(user_id, None)
            ok, count, sync_msg = sync_masters_appointments()
            await message.answer(
                "🎉 <b>Аккаунт Masters успешно подключен!</b>\n\n"
                f"✅ {sync_msg}\n"
                "Теперь бот будет автоматически отправлять клиентам напоминания накануне вечером и за 2 часа до начала визита.",
                reply_markup=admin_menu_keyboard(),
                parse_mode="HTML",
            )
            return

    # 2. Master phone input
    if user_id in pending_master_logins and pending_master_logins.get(user_id, {}).get("awaiting_phone") and is_phone_number(raw_text):
        success, msg, session_data = request_master_login(raw_text)
        if not success:
            await message.answer(f"❌ <b>{msg}</b>\n\nПопробуйте ввести другой номер телефона:")
            return
        pending_master_logins[user_id] = session_data
        await message.answer(
            f"📩 <b>Код подтверждения отправлен</b>\n\n"
            f"На номер <code>{session_data['phone_formatted']}</code> отправлено СМС от Masters.\n\n"
            "👉 <b>Напишите 4 цифры из СМС в ответном сообщении:</b>",
            parse_mode="HTML",
        )
        return

    if not is_admin(user_id):
        return

    # 3. Client name edit on appointment
    if user_id in admin_edit_client_drafts:
        edit_draft = admin_edit_client_drafts.pop(user_id)
        appt_id = edit_draft["appt_id"]
        current_filter = edit_draft["current_filter"]
        offset = edit_draft["offset"]

        phone_match = re.search(r"(\+?[78][\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}|\b9\d{9}\b|\b\d{10,11}\b)", raw_text)
        client_phone = None
        if phone_match:
            client_phone = phone_match.group(1)
            client_name = (raw_text[:phone_match.start()] + " " + raw_text[phone_match.end():]).strip(" :;,-")
            client_name = re.sub(r"\s+", " ", client_name).strip()
        else:
            client_name = raw_text.strip()

        if not client_name:
            client_name = "Клиент"

        ok, msg = update_appointment_client_details(appt_id, client_name, client_phone)
        appt = get_appointment(appt_id) or {}
        fresh_name = appt.get("client_name") or client_name
        fresh_phone = appt.get("phone") or "—"

        await message.answer(
            f"✅ <b>Данные клиента обновлены!</b>\n\n"
            f"👤 Клиент: <b>{html.escape(fresh_name)}</b>\n"
            f"📱 Телефон: <code>{fresh_phone}</code>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 К карточке записи", callback_data=f"admin:appt:{appt_id}:{current_filter}:{offset}")],
                    [InlineKeyboardButton(text="⬅️ К списку записей", callback_data=f"admin:appts:{current_filter}:{offset}")],
                ]
            ),
            parse_mode="HTML",
        )
        return

    # 3b. Custom price edit on appointment
    if user_id in admin_edit_price_drafts:
        p_draft = admin_edit_price_drafts.pop(user_id)
        appt_id = p_draft["appt_id"]
        current_filter = p_draft["current_filter"]
        offset = p_draft["offset"]

        price_digits = re.sub(r"\D", "", raw_text)
        if not price_digits or int(price_digits) <= 0:
            await message.answer("⚠️ Пожалуйста, введите корректную сумму в рублях (например: <code>1500</code>):", parse_mode="HTML")
            admin_edit_price_drafts[user_id] = p_draft
            return

        new_price = int(price_digits)
        ok, res_msg, updated_appt = update_appointment_price_and_service(appt_id, new_price)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 К карточке записи", callback_data=f"admin:appt:{appt_id}:{current_filter}:{offset}")],
                [InlineKeyboardButton(text="⬅️ К календарю записей", callback_data=f"admin:appts:{current_filter}:{offset}")],
            ]
        )
        await message.answer(
            f"✅ <b>Стоимость записи #{appt_id} успешно изменена на {new_price} ₽!</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    # 4. Clients search / import text
    c_state = admin_clients_state.get(user_id)
    if c_state:
        action = c_state.get("action")
        if action == "awaiting_search":
            admin_clients_state.pop(user_id, None)
            clients, total = get_clients_page(offset=0, limit=PAGE_SIZE, search=raw_text)
            text = f"🔍 <b>Результаты поиска «{html.escape(raw_text)}»</b> (найдено: {total}):\n\n"
            if not clients:
                text += "<i>Клиенты не найдены.</i>"
            else:
                for c in clients:
                    c_name = f"{c['first_name'] or ''} {c['last_name'] or ''}".strip() or "Без имени"
                    text += f"• <b>{html.escape(c_name)}</b> — <code>{c.get('phone', '—')}</code>\n"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="admin:clients:search")],
                    [InlineKeyboardButton(text="👥 Вся база", callback_data="admin:clients:0")],
                ]
            )
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

        if action == "awaiting_import":
            admin_clients_state.pop(user_id, None)
            added, updated, msg = import_clients_from_text(raw_text)
            await message.answer(
                f"✅ <b>Импорт клиентов завершён!</b>\n\n"
                f"• Добавлено новых клиентов: <b>{added}</b>\n"
                f"• Обновлено существующих: <b>{updated}</b>\n"
                f"• {msg}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="👥 Открыть базу клиентов", callback_data="admin:clients:0")],
                        [InlineKeyboardButton(text="⬅️ Меню админки", callback_data="admin:menu")],
                    ]
                ),
                parse_mode="HTML",
            )
            return

    # 5. Broadcast text
    if admin_broadcast_drafts.get(user_id, {}).get("step") == "awaiting_text":
        recipients = get_broadcast_recipients("telegram")
        count = len(recipients)
        admin_broadcast_drafts[user_id] = {
            "step": "confirm",
            "text": raw_text,
        }
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Отправить рассылку ({count})", callback_data="admin:broadcast:send")],
                [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="admin:broadcast")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:broadcast:cancel")],
            ]
        )
        preview = (
            "📢 <b>Предпросмотр рассылки:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{raw_text}\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 <b>Получателей в Telegram:</b> {count} чел.\n\n"
            "Отправить это сообщение всем клиентам?"
        )
        await message.answer(preview, reply_markup=kb, parse_mode="HTML")
        return

    # 6. Reschedule custom time
    if admin_resched_drafts.get(user_id, {}).get("step") == "awaiting_custom_time":
        draft = admin_resched_drafts.pop(user_id)
        # Parse time string e.g. "08:30" or "8 30"
        m = re.match(r"^(\d{1,2})[:.-](\d{2})$", raw_text)
        if not m:
            await message.answer("⚠️ Неверный формат времени. Введите время в формате <code>08:30</code> или <code>21:00</code>:")
            admin_resched_drafts[user_id] = draft
            return

        h, mm = int(m.group(1)), int(m.group(2))
        time_str = f"{h:02d}:{mm:02d}"
        day_key = draft["day_key"]
        appt_id = draft["appt_id"]

        try:
            new_dt = datetime.datetime.strptime(f"{day_key} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SARATOV_TZ)
        except Exception:
            await message.answer("Некорректная дата/время")
            return

        ok, msg, appt = reschedule_appointment(appt_id, new_dt)
        if ok and appt and appt.get("platform_user_id") and appt.get("platform") == "telegram":
            try:
                await message.bot.send_message(
                    chat_id=appt["platform_user_id"],
                    text=(
                        "🗓 <b>Ваша запись перенесена мастером!</b>\n\n"
                        f"⏰ Новое время: <b>{format_dt(new_dt)}</b>\n"
                        f"✂️ Услуга: {appt.get('services')}\n"
                        f"💰 Стоимость: {appt.get('total_price')} ₽\n\n"
                        "Ждем вас!"
                    ),
                )
            except Exception as e:
                logger.warning("Failed to notify client: %s", e)

        await message.answer(
            f"✅ <b>Запись #{appt_id} успешно перенесена на {format_dt(new_dt)}!</b>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📋 К календарю", callback_data="admin:appts:today:0")]]
            ),
        )
        return

    # 7. Create appointment steps
    create_draft = admin_create_drafts.get(user_id)
    if create_draft:
        step = create_draft.get("step")

        if step == "awaiting_custom_service":
            create_draft["service"] = raw_text
            create_draft["price"] = 2300
            create_draft["step"] = "day"
            now = get_current_saratov_time()
            rows = []
            for i in range(7):
                d = now + datetime.timedelta(days=i)
                day_key = d.strftime("%Y-%m-%d")
                title = f"{RU_WEEKDAYS[d.weekday()]}, {d.day} {RU_MONTHS[d.month]}"
                rows.append([InlineKeyboardButton(text=title, callback_data=f"admin:create:day:{day_key}")])
            rows.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:menu")])
            await message.answer(
                f"✂️ Услуга: <b>{raw_text}</b>\n\n<b>Шаг 2 из 4:</b> Выберите дату записи:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            return

        if step == "awaiting_custom_time":
            m = re.match(r"^(\d{1,2})[:.-](\d{2})$", raw_text)
            if not m:
                await message.answer("⚠️ Неверный формат. Введите время в формате <code>08:30</code> или <code>22:00</code>:")
                return
            h, mm = int(m.group(1)), int(m.group(2))
            create_draft["time_str"] = f"{h:02d}:{mm:02d}"
            create_draft["step"] = "awaiting_client_info"
            await message.answer(
                f"⏰ Время установлено: <b>{create_draft['time_str']}</b>\n\n"
                "👉 <b>Теперь отправьте в чат телефон и имя клиента:</b>\n"
                "Например: <code>+79991234567 Иван</code> или с ценой <code>89991234567 Иван 1200</code>",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:menu")]]
                ),
            )
            return

        if step == "awaiting_client_info":
            # Extract phone, optional name, optional price
            tokens = raw_text.split()
            phone = ""
            name = "Клиент"
            custom_price = create_draft.get("price", 2300)

            for token in tokens:
                digits = re.sub(r"\D", "", token)
                if len(digits) >= 10:
                    phone = normalize_phone(token)
                elif token.isdigit() and int(token) >= 100:
                    custom_price = int(token)
                elif not token.isdigit():
                    name = token

            if not phone:
                await message.answer("⚠️ Пожалуйста, укажите номер телефона клиента (например: <code>+79991234567 Иван</code>):")
                return

            # Combine names if multiple non-digits
            name_parts = [t for t in tokens if not re.sub(r"\D", "", t).isdigit() or len(re.sub(r"\D", "", t)) < 5]
            if name_parts:
                name = " ".join(name_parts)

            create_draft["phone"] = phone
            create_draft["name"] = name
            create_draft["price"] = custom_price

            day_key = create_draft["day_key"]
            time_str = create_draft["time_str"]
            dt_obj = datetime.datetime.strptime(f"{day_key} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SARATOV_TZ)

            ok, res_msg, appt_id = create_manual_appointment(
                phone=phone,
                appointment_time=dt_obj,
                services=create_draft["service"],
                total_price=custom_price,
                client_name=name,
                platform="manual",
            )

            admin_create_drafts.pop(user_id, None)

            if ok:
                await message.answer(
                    "🎉 <b>Запись успешно создана!</b>\n\n"
                    f"👤 Клиент: <b>{name}</b> (<code>{phone}</code>)\n"
                    f"⏰ Дата и время: <b>{format_dt(dt_obj)}</b>\n"
                    f"✂️ Услуга: <b>{create_draft['service']}</b>\n"
                    f"💰 Стоимость: <b>{custom_price} ₽</b>\n\n"
                    "✅ Запись внесена в календарь и синхронизирована с Masters CRM!",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="📋 К календарю записей", callback_data="admin:appts:today:0")],
                            [InlineKeyboardButton(text="➕ Создать ещё запись", callback_data="admin:create:start")],
                            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="admin:menu")],
                        ]
                    ),
                )
            else:
                await message.answer(f"❌ Ошибка создания записи: {res_msg}")
            return
