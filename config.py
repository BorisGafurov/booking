import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BOOKING_URL = os.getenv(
    "BOOKING_URL",
    "https://srezforma.ru",
)
WEB_APP_URL = os.getenv("WEB_APP_URL", "").strip() or BOOKING_URL

SALON_ADDRESS = os.getenv("SALON_ADDRESS", "Саратов, ул. Пугачёва, 110 (2-я дверь после арки, между Bookberry и Ozon)")
SALON_LATITUDE = float(os.getenv("SALON_LATITUDE", "51.537009"))
SALON_LONGITUDE = float(os.getenv("SALON_LONGITUDE", "46.013908"))
WORK_HOURS = os.getenv("WORK_HOURS", "Вторник и суббота: 10:00–19:00")

ADMIN_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_IDS", "").split(",")
    if item.strip().isdigit()
}

REFERRAL_REWARD_AMOUNT = int(os.getenv("REFERRAL_REWARD_AMOUNT", "300"))
REFERRAL_REWARD_TEXT = f"{REFERRAL_REWARD_AMOUNT} рублей"

