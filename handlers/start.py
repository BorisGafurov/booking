import html
import logging
from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from config import ADMIN_IDS
from keyboards.menu import get_main_menu
from services.referrals import register_referral, save_user

logger = logging.getLogger(__name__)
router = Router()

START_TEXTS = {
    "начать",
    "начни",
    "старт",
    "start",
    "/start",
    "меню",
    "главное меню",
    "menu",
    "привет",
    "здравствуйте",
    "хай",
    "записаться",
    "запись",
}


@router.message(CommandStart())
@router.message(F.text.lower().in_(START_TEXTS))
async def start(message: Message, command: CommandObject | None = None):
    user = message.from_user
    if not user:
        return

    user_db_id, is_new = None, False
    try:
        user_db_id, is_new = save_user(
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
    except Exception as e:
        logger.error("Failed to save user in start handler: %s", e)

    text = "Привет! Выбери нужный раздел:"
    extra_text = ""

    raw_args = command.args if (command and command.args) else None
    if not raw_args and message.text:
        parts = message.text.strip().split()
        if len(parts) > 1 and parts[0].lower() in ("/start", "start", "начать"):
            raw_args = parts[1]

    if raw_args and raw_args.startswith("ref_"):
        referrer_part = raw_args.replace("ref_", "", 1)

        if referrer_part.isdigit():
            referrer_telegram_id = int(referrer_part)

            if not is_new:
                extra_text = (
                    "\n\nℹ️ Вы уже были зарегистрированы в боте ранее. "
                    "Реферальный бонус начисляется только за приглашение новых пользователей."
                )
            else:
                success, result_text, notify_data = register_referral(
                    new_user_telegram_id=user.id,
                    referrer_telegram_id=referrer_telegram_id,
                )

                if success:
                    extra_text = "\n\n🎁 Вы пришли по приглашению. Реферальная связь сохранена!"
                    if notify_data:
                        try:
                            invited_name = html.escape(notify_data["invited_first_name"])
                            await message.bot.send_message(
                                notify_data["referrer_telegram_id"],
                                f"👋 По вашей ссылке зарегистрировался друг: <b>{invited_name}</b>!\n"
                                f"После его первого визита в салон вам начислится бонус."
                            )
                        except Exception as e:
                            logger.warning("Не удалось уведомить пригласившего: %s", e)
                else:
                    extra_text = f"\n\n{result_text}"

    is_admin = user.id in ADMIN_IDS
    await message.answer(
        text + extra_text,
        reply_markup=get_main_menu(is_admin=is_admin),
    )

