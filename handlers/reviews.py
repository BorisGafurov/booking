"""Client review handlers for Telegram bot."""

import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from handlers.ui import edit_or_send as _edit_or_send

from services.reviews import (
    can_review_platform_appointment,
    create_review_from_platform,
    get_reviewable_for_platform,
    get_reviews_for_platform,
)

logger = logging.getLogger(__name__)
router = Router()

# State for client awaiting review text input: telegram_user_id -> {"appointment_id": int, "rating": int}
user_pending_review: dict[int, dict[str, Any]] = {}


def get_rating_keyboard(appointment_id: int) -> InlineKeyboardMarkup:
    """Builds inline keyboard with 1..5 star buttons and a cancel button."""
    star_buttons = [
        InlineKeyboardButton(
            text=f"⭐ {n}",
            callback_data=f"review:rate:{appointment_id}:{n}",
        )
        for n in range(1, 6)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            star_buttons,
            [InlineKeyboardButton(text="❌ Отмена", callback_data="review:cancel")],
        ]
    )


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить написание отзыва", callback_data="review:cancel")]
        ]
    )


@router.message(F.text.in_({"⭐ Мои отзывы", "Мои отзывы", "/reviews", "/my_reviews"}))
async def show_my_reviews(message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    reviews = get_reviews_for_platform("telegram", user_id)
    reviewable = get_reviewable_for_platform("telegram", user_id)

    lines = ["⭐ <b>Мои отзывы</b>\n"]

    if not reviews and not reviewable:
        lines.append(
            "У тебя пока нет оставленных отзывов.\n\n"
            "После того как визит состоится и мастер подтвердит оплату, "
            "здесь появится возможность оценить стрижку и сервис! 💈"
        )
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    # If there are completed visits without review yet, show buttons to review them
    if reviewable:
        lines.append("✂️ <b>Визиты, ожидающие вашего отзыва:</b>")
        buttons = []
        for v in reviewable[:3]:
            svc = v.get("services") or "Визит"
            time_str = str(v.get("appointment_time") or "")[:10]
            lines.append(f"• {html.escape(svc)} ({time_str})")
            buttons.append([
                InlineKeyboardButton(
                    text=f"✍️ Оставить отзыв ({svc[:15]})",
                    callback_data=f"review:start:{v['id']}",
                )
            ])
        lines.append("")

    if reviews:
        lines.append("📋 <b>Ваша история отзывов:</b>\n")
        for r in reviews[:5]:
            stars = "⭐" * int(r.get("rating") or 5)
            created = str(r.get("created_at") or "")[:10]
            comment = html.escape(str(r.get("comment") or ""))
            svc = html.escape(str(r.get("services") or "Стрижка"))
            lines.append(f"{stars} ({r.get('rating')}/5) · <i>{created}</i>\n✂️ {svc}\n«{comment}»\n")

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if reviewable else None
    await message.answer("\n".join(lines), reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith("review:start:"))
async def handle_review_start(callback: CallbackQuery):
    if not callback.from_user or not callback.data:
        return
    user_id = callback.from_user.id
    try:
        appt_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка запроса.", show_alert=True)
        return

    can, msg, appt = can_review_platform_appointment("telegram", user_id, appt_id)
    if not can:
        await callback.answer(msg or "Отзыв за этот визит недоступен.", show_alert=True)
        return

    services = (appt.get("services") or "Стрижка") if appt else "Визит"
    text = (
        "✂️ <b>Оставить отзыв о визите</b>\n\n"
        f"Услуга: <b>{html.escape(str(services))}</b>\n\n"
        "Пожалуйста, выбери оценку от 1 до 5 звёзд:"
    )
    if callback.message:
        await _edit_or_send(callback.message, text, reply_markup=get_rating_keyboard(appt_id), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("review:rate:"))
async def handle_rating_selected(callback: CallbackQuery):
    if not callback.from_user or not callback.data:
        return
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    try:
        appt_id = int(parts[2])
        rating = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Ошибка параметров.", show_alert=True)
        return

    can, msg, _ = can_review_platform_appointment("telegram", user_id, appt_id)
    if not can:
        await callback.answer(msg or "Отзыв за этот визит недоступен.", show_alert=True)
        return

    user_pending_review[user_id] = {"appointment_id": appt_id, "rating": rating}

    stars_str = "⭐" * rating
    text = (
        f"Ты выбрал оценку: <b>{rating} из 5</b> ({stars_str})\n\n"
        "✍️ <b>Напиши, пожалуйста, текстовый отзыв:</b>\n"
        "<i>(от 10 до 500 символов — что понравилось, качество стрижки, сервис и атмосфера)</i>\n\n"
        "<i>Для отмены напиши /cancel или нажми кнопку ниже 👇</i>"
    )

    if callback.message:
        await _edit_or_send(callback.message, text, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "review:cancel")
async def handle_review_cancel_callback(callback: CallbackQuery):
    if callback.from_user:
        user_pending_review.pop(callback.from_user.id, None)
    if callback.message:
        await _edit_or_send(
            callback.message,
            "Отзыв отменён. Ты сможешь оставить его в любое время в разделе «📋 Мои записи».",
            parse_mode="HTML",
        )
    await callback.answer("Отменено")


@router.message(F.text)
async def handle_review_comment_input(message: Message):
    if not message.from_user or not message.text:
        return

    user_id = message.from_user.id
    pending = user_pending_review.get(user_id)
    if not pending:
        # User is not currently submitting a review; let other routers/fallback handle it
        return

    text = message.text.strip()
    if text in {"/cancel", "отмена", "Отмена"}:
        user_pending_review.pop(user_id, None)
        await message.answer("Отзыв отменён. Ты сможешь оставить его в любое время в разделе «📋 Мои записи».")
        return

    appt_id = pending["appointment_id"]
    rating = pending["rating"]

    ok, err_or_msg, created = create_review_from_platform(
        platform="telegram",
        platform_user_id=user_id,
        appointment_id=appt_id,
        rating=rating,
        comment=text,
    )

    if not ok:
        await message.answer(
            f"❌ <b>{html.escape(err_or_msg)}</b>\n\n"
            "Пожалуйста, напиши отзыв ещё раз или отправь /cancel для отмены:",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML",
        )
        return

    user_pending_review.pop(user_id, None)
    await message.answer(
        "🎉 <b>Спасибо за твой отзыв!</b>\n\n"
        "Он успешно опубликован и теперь виден на нашем сайте <b>srezforma.ru</b> в разделе «Отзывы».\n\n"
        "Будем рады видеть тебя снова! 💈",
        parse_mode="HTML",
    )
