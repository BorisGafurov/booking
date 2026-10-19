from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import (
    SALON_ADDRESS,
    SALON_LATITUDE,
    SALON_LONGITUDE,
    WORK_HOURS,
)

router = Router()


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


@router.message(Command("location", "address", "map"))
@router.message(F.text.in_({"📍 Как добраться", "Как добраться"}))
async def navigation(message: Message):
    await message.answer_location(
        latitude=SALON_LATITUDE,
        longitude=SALON_LONGITUDE,
    )

    await message.answer(
        f"<b>Адрес:</b>\n{SALON_ADDRESS}\n\n"
        f"<b>Часы работы:</b>\n{WORK_HOURS}",
        reply_markup=get_navigation_keyboard(),
    )

