from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from config import BOOKING_URL


def get_booking_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✂️ Записаться онлайн",
                    url=BOOKING_URL,
                )
            ]
        ]
    )


def get_request_phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📱 Отправить номер телефона",
                    request_contact=True,
                )
            ],
            [
                KeyboardButton(text="◀️ Назад в меню")
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
