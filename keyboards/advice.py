from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services.recommendations import get_articles, get_products


def get_recommendations_hub_keyboard() -> InlineKeyboardMarkup:
    articles = get_articles()
    rows = []

    # Display articles directly in hub for instant 1-click access
    for slug, item in articles.items():
        rows.append(
            [InlineKeyboardButton(text=item["title"], callback_data=f"art:{slug}")]
        )

    # Big button to Ozon products
    rows.append(
        [InlineKeyboardButton(text="🛍 Проверенные товары на Ozon", callback_data="rec:products")]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_products_keyboard() -> InlineKeyboardMarkup:
    products = get_products()
    rows = []
    for slug, item in products.items():
        rows.append(
            [InlineKeyboardButton(text=item["title"], callback_data=f"prod:{slug}")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад к советам", callback_data="rec:hub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_article_detail_keyboard(slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Товары на Ozon", callback_data="rec:products")],
            [InlineKeyboardButton(text="⬅️ Назад к советам", callback_data="rec:hub")],
        ]
    )


def get_product_detail_keyboard(links: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = []
    for label, url in links:
        btn_text = f"🛍 {label[:28]} ↗"
        rows.append([InlineKeyboardButton(text=btn_text, url=url)])

    rows.append([InlineKeyboardButton(text="⬅️ Назад к товарам", callback_data="rec:products")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
