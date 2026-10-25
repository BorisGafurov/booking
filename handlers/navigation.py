from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from config import (
    SALON_ADDRESS,
    SALON_LATITUDE,
    SALON_LONGITUDE,
    WORK_HOURS,
)
from keyboards.navigation import get_navigation_keyboard

router = Router()


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

