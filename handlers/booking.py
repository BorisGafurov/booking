from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ADMIN_IDS
from keyboards.booking import get_booking_keyboard, get_request_phone_keyboard
from keyboards.menu import get_main_menu
from services.referrals import get_user_phone, is_phone_number, link_user_phone

router = Router()


@router.message(Command("book", "booking"))
@router.message(F.text.in_({"✂️ Записаться", "Записаться"}))
async def booking(message: Message):
    if not message.from_user:
        return

    phone = get_user_phone(message.from_user.id, platform="telegram")
    if phone:
        await message.answer(
            "Нажми кнопку ниже, выбери услугу и свободное время:",
            reply_markup=get_booking_keyboard(),
        )
    else:
        await message.answer(
            "✂️ <b>Онлайн-запись</b>\n\n"
            "Чтобы закрепить за тобой бонусы и историю записей, нажми кнопку ниже и поделись номером телефона:",
            reply_markup=get_request_phone_keyboard(),
            parse_mode="HTML",
        )


@router.message(F.contact)
async def handle_contact(message: Message):
    if not message.contact or not message.from_user:
        return
    phone = message.contact.phone_number
    is_admin_user = message.from_user.id in ADMIN_IDS
    link_user_phone(message.from_user.id, phone, platform="telegram")

    await message.answer(
        "✅ <b>Номер телефона успешно сохранен!</b>\n\n"
        "Теперь твои бонусы и приглашённые друзья объединены в едином профиле.\n\n"
        "Нажми кнопку ниже, выбери услугу и свободное время:",
        reply_markup=get_booking_keyboard(),
        parse_mode="HTML",
    )
    await message.answer("Главное меню:", reply_markup=get_main_menu(is_admin_user))


@router.message(F.text == "◀️ Назад в меню")
async def back_to_menu(message: Message):
    if not message.from_user:
        return
    is_admin_user = message.from_user.id in ADMIN_IDS
    await message.answer("Главное меню:", reply_markup=get_main_menu(is_admin_user))


@router.message(lambda msg: bool(msg.text and is_phone_number(msg.text)))
async def handle_text_phone(message: Message):
    if not message.from_user or not message.text:
        return
    is_admin_user = message.from_user.id in ADMIN_IDS
    success, _ = link_user_phone(message.from_user.id, message.text, platform="telegram")
    if success:
        await message.answer(
            "✅ <b>Номер телефона успешно сохранен!</b>\n\n"
            "Теперь твои бонусы и приглашённые друзья объединены в едином профиле.\n\n"
            "Нажми кнопку ниже, выбери услугу и свободное время:",
            reply_markup=get_booking_keyboard(),
            parse_mode="HTML",
        )
        await message.answer("Главное меню:", reply_markup=get_main_menu(is_admin_user))
    else:
        await message.answer("Пожалуйста, введи корректный номер телефона (например, +79991234567).")

