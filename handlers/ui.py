"""Shared helpers for rendering screens on messages (Telegram)."""

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)


async def edit_or_send(
    message: Message | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    """
    Shows a screen on the message a button was pressed on.

    Telegram does not allow editing messages older than 48 hours, so pressing
    a button on an old message used to silently do nothing (the TelegramBadRequest
    was swallowed or left the callback unanswered). On any edit failure we fall
    back to sending the screen as a fresh message.
    """
    if not message:
        return

    kwargs: dict = {}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode

    try:
        await message.edit_text(text, **kwargs)
        return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        logger.debug("edit_text failed, sending a new message instead: %s", e)
    except Exception as e:
        logger.debug("edit_text failed, sending a new message instead: %s", e)

    try:
        await message.answer(text, **kwargs)
    except Exception as e:
        logger.warning("Failed to send fallback screen: %s", e)
