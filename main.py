import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message

from config import ADMIN_IDS, BOT_TOKEN
from database import init_db
from handlers.advice import router as advice_router
from handlers.booking import router as booking_router
from handlers.my_appointments import router as my_appointments_router
from handlers.navigation import router as navigation_router
from handlers.referrals import router as referrals_router
from handlers.start import router as start_router
from keyboards.menu import get_main_menu
from services.reminder_worker import run_reminder_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

fallback_router = Router()


@fallback_router.message()
async def fallback_handler(message: Message):
    if not message.from_user:
        return
    is_admin = message.from_user.id in ADMIN_IDS
    await message.answer(
        "Пожалуйста, выбери нужный раздел с помощью меню ниже 👇",
        reply_markup=get_main_menu(is_admin=is_admin),
    )


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Добавь BOT_TOKEN в файл .env")

    init_db()

    dp = Dispatcher()

    dp.include_router(start_router)
    dp.include_router(referrals_router)
    dp.include_router(booking_router)
    dp.include_router(my_appointments_router)
    dp.include_router(navigation_router)
    dp.include_router(advice_router)
    dp.include_router(fallback_router)

    default_properties = DefaultBotProperties(parse_mode=ParseMode.HTML)
    async with Bot(token=BOT_TOKEN, default=default_properties) as bot:
        await bot.delete_webhook(drop_pending_updates=False)
        worker_task = asyncio.create_task(run_reminder_worker(bot, "telegram"))
        logger.info("Бот успешно запущен. Нажми Ctrl+C для остановки.")
        try:
            await dp.start_polling(bot)
        finally:
            worker_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())

