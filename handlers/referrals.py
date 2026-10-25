import asyncio
import html
import logging
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_IDS, REFERRAL_REWARD_TEXT
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
    is_phone_number,
    mark_referral_visited,
    mark_reward_used,
    save_user,
)

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 10
admin_broadcast_drafts: dict[int, dict] = {}


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Ожидают подтверждения", callback_data="admin:pending:0")],
            [InlineKeyboardButton(text="💰 Активные бонусы", callback_data="admin:rewards:0")],
            [InlineKeyboardButton(text="🔄 Синхронизация Masters", callback_data="admin:masters")],
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin:broadcast")],
        ]
    )


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

    # Pagination buttons
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

    # Pagination buttons
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


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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
    # Ensure current user is registered in DB so their ref link works immediately
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
        "⚙️ <b>Админ-панель:</b>",
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
                "⚙️ <b>Админ-панель:</b>",
                reply_markup=admin_menu_keyboard(),
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


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


@router.message(lambda msg: bool(msg.from_user and msg.from_user.id in pending_master_logins and msg.text and msg.text.strip().isdigit() and len(msg.text.strip()) == 4))
async def handle_master_sms_code(message: Message):
    user_id = message.from_user.id
    session_data = pending_master_logins.get(user_id)
    if not session_data or session_data.get("awaiting_phone"):
        return

    code = message.text.strip()
    success, msg = confirm_master_login(session_data, code)
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


@router.message(lambda msg: bool(msg.from_user and msg.from_user.id in pending_master_logins and pending_master_logins.get(msg.from_user.id, {}).get("awaiting_phone") and msg.text and is_phone_number(msg.text)))
async def handle_master_phone_input(message: Message):
    user_id = message.from_user.id
    phone = message.text.strip()
    success, msg, session_data = request_master_login(phone)
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


@router.message(
    lambda msg: bool(
        msg.from_user
        and is_admin(msg.from_user.id)
        and admin_broadcast_drafts.get(msg.from_user.id, {}).get("step") == "awaiting_text"
        and msg.text
        and not msg.text.startswith("/")
    )
)
async def admin_broadcast_receive_text(message: Message):
    admin_id = message.from_user.id
    broadcast_text = message.text.strip()
    recipients = get_broadcast_recipients("telegram")
    count = len(recipients)

    admin_broadcast_drafts[admin_id] = {
        "step": "confirm",
        "text": broadcast_text,
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
        f"{broadcast_text}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 <b>Получателей в Telegram:</b> {count} чел.\n\n"
        "Отправить это сообщение всем клиентам?"
    )
    await message.answer(preview, reply_markup=kb, parse_mode="HTML")


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


