from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import SALON_LATITUDE, SALON_LONGITUDE


def get_navigation_keyboard() -> InlineKeyboardMarkup:
    yandex_maps_url = (
        f"https://yandex.ru/maps/?pt={SALON_LONGITUDE},{SALON_LATITUDE}&z=17&l=map"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗺 Открыть в Яндекс Картах",
                    url=yandex_maps_url,
                )
            ]
        ]
    )
