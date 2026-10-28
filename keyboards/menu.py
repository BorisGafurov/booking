from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def get_main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(text="✂️ Записаться"),
            KeyboardButton(text="📍 Как добраться"),
        ],
        [
            KeyboardButton(text="📋 Мои записи"),
            KeyboardButton(text="🎁 Пригласить друга"),
        ],
        [
            KeyboardButton(text="🧴 Советы и товары"),
        ],
    ]

    if is_admin:
        keyboard.append([KeyboardButton(text="⚙️ Админ-панель")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
    )

