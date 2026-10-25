import html
import logging
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_IDS, REFERRAL_REWARD_TEXT
from services.referrals import (
    build_referral_link,
    get_active_rewards,
    get_pending_referrals,
    get_referral_stats,
    mark_referral_visited,
    mark_reward_used,
    save_user,
)

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 10


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Ожидают подтверждения", callback_data="admin:pending:0")],
            [InlineKeyboardButton(text="💰 Активные бонусы", callback_data="admin:rewards:0")],
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

