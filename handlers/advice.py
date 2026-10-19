import logging
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from keyboards.advice import (
    get_article_detail_keyboard,
    get_articles_keyboard,
    get_product_detail_keyboard,
    get_products_keyboard,
    get_recommendations_hub_keyboard,
)
from services.recommendations import get_article, get_product

logger = logging.getLogger(__name__)
router = Router()

HUB_TEXT = (
    "🧴 <b>Рекомендации мастера и проверенный стайлинг</b>\n\n"
    "Здесь собраны подробные инструкции по укладке и ссылки на проверенные средства с Ozon, "
    "которыми мастер пользуется лично.\n\n"
    "Выбери нужный раздел:"
)


@router.message(Command("advice", "tips", "care", "products"))
@router.message(F.text.in_({"🧴 Советы по уходу", "Советы по уходу", "🧴 Советы и товары", "Советы и товары"}))
async def advice_menu(message: Message):
    await message.answer(
        HUB_TEXT,
        reply_markup=get_recommendations_hub_keyboard(),
    )


@router.callback_query(F.data == "rec:hub")
async def callback_rec_hub(callback: CallbackQuery):
    try:
        if callback.message:
            await callback.message.edit_text(
                HUB_TEXT,
                reply_markup=get_recommendations_hub_keyboard(),
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "rec:articles")
async def callback_rec_articles(callback: CallbackQuery):
    try:
        if callback.message:
            await callback.message.edit_text(
                "📖 <b>Инструкции по уходу и укладке:</b>\n\n"
                "Выбери тему, чтобы прочитать руководство мастера 👇",
                reply_markup=get_articles_keyboard(),
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "rec:products")
async def callback_rec_products(callback: CallbackQuery):
    try:
        if callback.message:
            await callback.message.edit_text(
                "🛍 <b>Проверенные средства на Ozon:</b>\n\n"
                "Здесь собраны проверенные стайлинги и уход, которые мастер лично использует в работе. "
                "Выбери категорию, чтобы посмотреть рекомендации и ссылки 👇",
                reply_markup=get_products_keyboard(),
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("art:"))
async def callback_article_detail(callback: CallbackQuery):
    slug = callback.data.split(":", 1)[1]
    article = get_article(slug)

    if not article:
        await callback.answer("Инструкция не найдена", show_alert=True)
        return

    text = f"<b>{article['title']}</b>\n\n{article['text']}"

    try:
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=get_article_detail_keyboard(slug),
                disable_web_page_preview=True,
            )
    except TelegramBadRequest as e:
        logger.warning("Ошибка при отображении статьи: %s", e)

    await callback.answer()


@router.callback_query(F.data.startswith("prod:"))
async def callback_product_detail(callback: CallbackQuery):
    slug = callback.data.split(":", 1)[1]
    product = get_product(slug)

    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return

    text = f"<b>{product['title']}</b>\n\n{product['text']}"

    try:
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=get_product_detail_keyboard(product["links"]),
                disable_web_page_preview=True,
            )
    except TelegramBadRequest as e:
        logger.warning("Ошибка при отображении товара: %s", e)

    await callback.answer()
