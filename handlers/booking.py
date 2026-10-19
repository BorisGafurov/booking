from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from keyboards.booking import get_booking_keyboard

router = Router()


@router.message(Command("book", "booking"))
@router.message(F.text.in_({"✂️ Записаться", "Записаться"}))
async def booking(message: Message):
    await message.answer(
        "Нажми кнопку ниже, выбери услугу и свободное время:",
        reply_markup=get_booking_keyboard(),
    )

