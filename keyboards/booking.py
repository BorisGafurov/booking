from typing import Any
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from config import BOOKING_URL, WEB_APP_URL
from services.booking import SERVICES, calculate_services


def _get_site_inline_btn(text: str = "🌐 Открыть сайт / Запись онлайн") -> InlineKeyboardButton:
    if WEB_APP_URL and WEB_APP_URL.startswith("https://"):
        return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=WEB_APP_URL))
    return InlineKeyboardButton(text=text, url=WEB_APP_URL or BOOKING_URL)


def get_booking_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _get_site_inline_btn("🌐 Записаться на сайте / Мини-Апп")
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


def get_services_keyboard(selected_keys: list[str] | set[str]) -> InlineKeyboardMarkup:
    calc = calculate_services(selected_keys)
    active_keys = set(calc["keys"])

    rows = []
    for key, info in SERVICES.items():
        is_selected = key in active_keys
        icon = "✅" if is_selected else "⚪️"
        btn_text = f"{icon} {info['name']} · {info['price']} ₽ ({info['duration_text']})"
        rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"book:toggle:{key}")])

    # Next step button with total price and duration
    next_text = f"📅 Выбрать время ({calc['total_price']} ₽ · {calc['duration_text']})"
    rows.append([InlineKeyboardButton(text=next_text, callback_data="book:choose_day")])

    # Site / Mini App button
    rows.append([_get_site_inline_btn("🌐 Открыть сайт / Запись онлайн")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_days_keyboard(days_map: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = []
    day_items = list(days_map.values())[:8]  # Show next up to 8 available days

    # Pair buttons 2 per row
    for i in range(0, len(day_items), 2):
        row = []
        for d in day_items[i : i + 2]:
            row.append(InlineKeyboardButton(text=f"📅 {d['title']}", callback_data=f"book:day:{d['day_key']}"))
        rows.append(row)

    if not rows:
        rows.append([InlineKeyboardButton(text="Нет свободных дат", callback_data="noop")])

    rows.append([
        InlineKeyboardButton(text="◀️ К выбору услуг", callback_data="book:services"),
        _get_site_inline_btn("🌐 На сайт"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_slots_keyboard(slots: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    # 4 time buttons per row
    for i in range(0, len(slots), 4):
        row = []
        for s in slots[i : i + 4]:
            row.append(InlineKeyboardButton(text=f"🕒 {s['time']}", callback_data=f"book:slot:{s['timestamp']}"))
        rows.append(row)

    if not rows:
        rows.append([InlineKeyboardButton(text="На этот день нет свободных окон", callback_data="noop")])

    rows.append([
        InlineKeyboardButton(text="◀️ Выбрать другой день", callback_data="book:choose_day"),
        _get_site_inline_btn("🌐 На сайт"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
