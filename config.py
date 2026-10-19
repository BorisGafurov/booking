import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BOOKING_URL = os.getenv(
    "BOOKING_URL",
    "https://mst.link/boris_gafurov/price?cid=vddrm99a3uu0o9cgk508u6uctp",
)

SALON_ADDRESS = os.getenv("SALON_ADDRESS", "Саратов, ул. имени Е.И. Пугачёва, 110")
SALON_LATITUDE = float(os.getenv("SALON_LATITUDE", "51.536988"))
SALON_LONGITUDE = float(os.getenv("SALON_LONGITUDE", "46.013896"))
WORK_HOURS = os.getenv("WORK_HOURS", "Вторник и суббота: 10:00–19:00")

ADMIN_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_IDS", "").split(",")
    if item.strip().isdigit()
}

REFERRAL_REWARD_AMOUNT = int(os.getenv("REFERRAL_REWARD_AMOUNT", "300"))
REFERRAL_REWARD_TEXT = f"{REFERRAL_REWARD_AMOUNT} рублей"

