from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BOOKING_URL


def get_booking_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Записаться онлайн",
                    url=BOOKING_URL,
                )
            ]
        ]
    )
