from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from config import WEB_APP_URL


def get_main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    if WEB_APP_URL and WEB_APP_URL.startswith("https://"):
        site_btn = KeyboardButton(
            text="🌐 Наш сайт / Запись",
            web_app=WebAppInfo(url=WEB_APP_URL),
        )
    else:
        site_btn = KeyboardButton(text="🌐 Наш сайт")

    keyboard = [
        [
            KeyboardButton(text="✂️ Записаться"),
            site_btn,
        ],
        [
            KeyboardButton(text="📍 Как добраться"),
            KeyboardButton(text="📋 Мои записи"),
        ],
        [
            KeyboardButton(text="🎁 Пригласить друга"),
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
