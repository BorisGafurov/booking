import logging
import os
import re
from pathlib import Path
from typing import Any

try:
    from aiogram import Bot
    from aiogram.utils.deep_linking import create_start_link
except ImportError:
    Bot = None
    create_start_link = None

try:
    from config import REFERRAL_REWARD_AMOUNT, REFERRAL_REWARD_TEXT
except ImportError:
    REFERRAL_REWARD_AMOUNT = 300
    REFERRAL_REWARD_TEXT = "300 ₽"

from core_bot.database import get_connection

logger = logging.getLogger(__name__)


def get_current_platform() -> str:
    env_p = (os.getenv("DEFAULT_PLATFORM") or os.getenv("PLATFORM") or "").strip().lower()
    if env_p in ("telegram", "vk", "max"):
        return env_p
    try:
        repo_name = Path(__file__).resolve().parents[2].name.lower()
        if "vk" in repo_name:
            return "vk"
        if "max" in repo_name:
            return "max"
        if "booking" in repo_name:
            return "telegram"
    except Exception:
        pass
    try:
        import config
        if hasattr(config, "VK_TOKEN") and config.VK_TOKEN:
            return "vk"
        if hasattr(config, "MAX_BOT_TOKEN") and config.MAX_BOT_TOKEN:
            return "max"
    except Exception:
        pass
    cwd = os.getcwd().lower()
    if "vk" in cwd:
        return "vk"
    if "max" in cwd:
        return "max"
    return "telegram"


def normalize_phone(raw_phone: str | None) -> str:
    """Normalize phone string to standard +7XXXXXXXXXX format."""
    if not raw_phone:
        return ""
    digits = re.sub(r"\D", "", raw_phone)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    elif digits.startswith("7") and len(digits) == 11:
        pass
    elif len(digits) == 10:
        digits = "7" + digits
    return f"+{digits}" if digits else ""


def get_master_phones() -> set[str]:
    """Return normalized master phone numbers configured via MASTER_PHONES/MASTER_PHONE."""
    raw = os.getenv("MASTER_PHONES") or os.getenv("MASTER_PHONE") or ""
    phones: set[str] = set()
    for item in raw.split(","):
        norm = normalize_phone(item.strip())
        if norm:
            phones.add(norm)
    return phones


def is_master_phone(phone: str | None) -> bool:
    norm = normalize_phone(phone)
    return bool(norm and norm in get_master_phones())


def _find_user(conn, identifier: int, platform: str | None = None) -> dict | None:
    if platform is None:
        platform = get_current_platform()

    if platform == "telegram":
        row = conn.execute("SELECT * FROM users WHERE telegram_id = %s", (identifier,)).fetchone()
        return dict(row) if row else None
    elif platform == "vk":
        row = conn.execute("SELECT * FROM users WHERE vk_id = %s", (identifier,)).fetchone()
        return dict(row) if row else None
    elif platform == "max":
        row = conn.execute("SELECT * FROM users WHERE max_user_id = %s", (identifier,)).fetchone()
        return dict(row) if row else None
    elif platform in ("id", "db", "user_id"):
        row = conn.execute("SELECT * FROM users WHERE id = %s", (identifier,)).fetchone()
        return dict(row) if row else None

    return None


def _save_user_unified(
    *,
    telegram_id: int | None = None,
    vk_id: int | None = None,
    max_user_id: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
    domain: str | None = None,
) -> tuple[int, bool]:
    uname = username or domain
    with get_connection() as conn:
        user = None
        # 1. Search by platform IDs
        if telegram_id:
            row = conn.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,)).fetchone()
            user = dict(row) if row else None
        elif vk_id:
            row = conn.execute("SELECT * FROM users WHERE vk_id = %s", (vk_id,)).fetchone()
            user = dict(row) if row else None
        elif max_user_id:
            row = conn.execute("SELECT * FROM users WHERE max_user_id = %s", (max_user_id,)).fetchone()
            user = dict(row) if row else None

        # 2. Search by normalized phone if not found by platform ID
        if not user and phone:
            row = conn.execute("SELECT * FROM users WHERE phone = %s", (phone,)).fetchone()
            user = dict(row) if row else None

        if not user:
            if conn.is_pg:
                cursor = conn.execute(
                    """
                    INSERT INTO users (telegram_id, vk_id, max_user_id, username, first_name, last_name, phone)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (telegram_id, vk_id, max_user_id, uname, first_name, last_name, phone),
                )
                inserted_id = cursor.fetchone()[0]
            else:
                conn.execute(
                    """
                    INSERT INTO users (telegram_id, vk_id, max_user_id, username, first_name, last_name, phone)
                    VALUES (%s, %s, %s, %s, %s, %s, %s);
                    """,
                    (telegram_id, vk_id, max_user_id, uname, first_name, last_name, phone),
                )
                inserted_id = conn.raw_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            return inserted_id, True

        # Exact phone match: merge this platform profile into the phone owner.
        if phone:
            normalized = normalize_phone(phone)
            if normalized:
                phone = normalized
                owner = conn.execute("SELECT * FROM users WHERE phone = %s", (normalized,)).fetchone()
                if owner and owner["id"] != user["id"]:
                    if merge_users(conn, owner["id"], user["id"]):
                        user = dict(
                            conn.execute("SELECT * FROM users WHERE id = %s", (owner["id"],)).fetchone()
                        )
                    else:
                        # Same-platform ID conflict: keep the existing phone.
                        phone = None

        conn.execute(
            """
            UPDATE users
            SET username = COALESCE(%s, username),
                first_name = COALESCE(%s, first_name),
                last_name = COALESCE(%s, last_name),
                phone = COALESCE(%s, phone),
                telegram_id = COALESCE(%s, telegram_id),
                vk_id = COALESCE(%s, vk_id),
                max_user_id = COALESCE(%s, max_user_id),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (uname, first_name, last_name, phone, telegram_id, vk_id, max_user_id, user["id"]),
        )
        conn.commit()
        return user["id"], False


def save_user(
    telegram_id: int | None = None,
    vk_id: int | None = None,
    max_user_id: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
    domain: str | None = None,
) -> tuple[int, bool]:
    norm_phone = normalize_phone(phone) if phone else None
    return _save_user_unified(
        telegram_id=telegram_id,
        vk_id=vk_id,
        max_user_id=max_user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        phone=norm_phone,
        domain=domain,
    )


def build_referral_link(bot_or_id: Any, user_id: int | None = None) -> Any:
    """Build deep link. Supports bot object (aiogram, async) or user_id integer (sync)."""
    if user_id is None and (isinstance(bot_or_id, int) or (isinstance(bot_or_id, str) and str(bot_or_id).isdigit())):
        uid = int(bot_or_id)
        try:
            from config import VK_GROUP_DOMAIN, VK_GROUP_ID
            if VK_GROUP_DOMAIN:
                return f"https://vk.me/{VK_GROUP_DOMAIN}?ref=ref_{uid}"
            elif VK_GROUP_ID:
                return f"https://vk.com/im?sel=-{VK_GROUP_ID}&ref=ref_{uid}"
        except ImportError:
            pass
        return f"https://vk.me/?ref=ref_{uid}"

    async def _async_builder():
        if Bot is not None and isinstance(bot_or_id, Bot) and user_id is not None:
            if create_start_link is not None:
                return await create_start_link(bot_or_id, f"ref_{user_id}", encode=False)
            return f"https://t.me/bot?start=ref_{user_id}"
        uid = user_id if user_id is not None else bot_or_id
        return f"ref_{uid}"

    return _async_builder()


get_referral_link = build_referral_link


def register_referral(
    new_user_id: int | None = None,
    referrer_id: int | None = None,
    platform: str | None = None,
    *,
    new_user_telegram_id: int | None = None,
    referrer_telegram_id: int | None = None,
    new_user_vk_id: int | None = None,
    referrer_vk_id: int | None = None,
    new_user_max_id: int | None = None,
    referrer_max_id: int | None = None,
) -> tuple[bool, str, dict | None]:
    # Resolve platform & IDs from explicit kwargs if passed
    if new_user_telegram_id is not None:
        new_user_id = new_user_telegram_id
        referrer_id = referrer_telegram_id
        platform = "telegram"
    elif new_user_vk_id is not None:
        new_user_id = new_user_vk_id
        referrer_id = referrer_vk_id
        platform = "vk"
    elif new_user_max_id is not None:
        new_user_id = new_user_max_id
        referrer_id = referrer_max_id
        platform = "max"
    elif platform is None:
        platform = get_current_platform()

    if new_user_id == referrer_id:
        return False, "Нельзя пригласить самого себя.", None

    col = "telegram_id" if platform == "telegram" else ("vk_id" if platform == "vk" else "max_user_id")

    with get_connection() as conn:
        new_user = _find_user(conn, new_user_id, platform)
        if not new_user:
            return False, "Пользователь не найден.", None

        referrer = _find_user(conn, referrer_id, platform)

        if not referrer:
            conn.execute(f"INSERT INTO users ({col}) VALUES (%s)", (referrer_id,))
            referrer = _find_user(conn, referrer_id, platform)

        if not referrer:
            return False, "Пригласивший пользователь не найден.", None

        if new_user["id"] == referrer["id"]:
            return False, "Нельзя пригласить самого себя.", None

        existing = conn.execute(
            "SELECT id FROM referrals WHERE invited_user_id = %s",
            (new_user["id"],),
        ).fetchone()

        if existing:
            return False, "Вы уже были зарегистрированы по реферальной ссылке ранее.", None

        conn.execute(
            """
            INSERT INTO referrals (referrer_user_id, invited_user_id, status)
            VALUES (%s, %s, 'started')
            """,
            (referrer["id"], new_user["id"]),
        )
        conn.commit()

        notify_data = {
            "referrer_id": referrer["id"],
            "referrer_db_id": referrer["id"],
            "referrer_telegram_id": referrer.get("telegram_id") or (referrer_id if platform == "telegram" else None),
            "referrer_vk_id": referrer.get("vk_id") or (referrer_id if platform == "vk" else None),
            "referrer_max_id": referrer.get("max_user_id") or (referrer_id if platform == "max" else None),
            "referrer_first_name": referrer["first_name"] or "Друг",
            "invited_first_name": new_user["first_name"] or "Новый клиент",
        }

    return True, "Реферальная связь сохранена.", notify_data


def get_referral_stats(
    referrer_platform_id: int | None = None,
    platform: str | None = None,
    *,
    referrer_telegram_id: int | None = None,
    referrer_vk_id: int | None = None,
    referrer_max_id: int | None = None,
) -> dict:
    if referrer_telegram_id is not None:
        referrer_platform_id = referrer_telegram_id
        platform = "telegram"
    elif referrer_vk_id is not None:
        referrer_platform_id = referrer_vk_id
        platform = "vk"
    elif referrer_max_id is not None:
        referrer_platform_id = referrer_max_id
        platform = "max"
    elif platform is None:
        platform = get_current_platform()

    with get_connection() as conn:
        referrer = _find_user(conn, referrer_platform_id, platform)
        if not referrer:
            return {
                "started": 0,
                "confirmed": 0,
                "active_rewards_count": 0,
                "active_rewards_sum": 0,
                "used_rewards_sum": 0,
            }

        started_count = conn.execute(
            "SELECT COUNT(*) AS total FROM referrals WHERE referrer_user_id = %s",
            (referrer["id"],),
        ).fetchone()["total"]

        confirmed_count = conn.execute(
            "SELECT COUNT(*) AS total FROM referrals WHERE referrer_user_id = %s AND status = 'rewarded'",
            (referrer["id"],),
        ).fetchone()["total"]

        active_rewards = conn.execute(
            """
            SELECT COUNT(*) AS total_count, COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = %s AND status = 'active'
            """,
            (referrer["id"],),
        ).fetchone()

        used_rewards = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = %s AND status = 'used'
            """,
            (referrer["id"],),
        ).fetchone()

        return {
            "started": started_count,
            "confirmed": confirmed_count,
            "active_rewards_count": active_rewards["total_count"],
            "active_rewards_sum": active_rewards["total_sum"],
            "used_rewards_sum": used_rewards["total_sum"],
        }


def get_pending_referrals(limit: int = 15, offset: int = 0) -> tuple[list[dict], int]:
    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS total FROM referrals WHERE status = 'started'"
        ).fetchone()["total"]

        rows = conn.execute(
            """
            SELECT
                r.id,
                invited.id AS invited_db_id,
                invited.telegram_id AS invited_telegram_id,
                invited.vk_id AS invited_vk_id,
                invited.max_user_id AS invited_max_id,
                invited.phone AS invited_phone,
                invited.first_name AS invited_first_name,
                invited.last_name AS invited_last_name,
                invited.username AS invited_username,
                invited.username AS invited_domain,
                referrer.id AS referrer_db_id,
                referrer.telegram_id AS referrer_telegram_id,
                referrer.vk_id AS referrer_vk_id,
                referrer.max_user_id AS referrer_max_id,
                referrer.phone AS referrer_phone,
                referrer.first_name AS referrer_first_name,
                referrer.last_name AS referrer_last_name,
                referrer.username AS referrer_username,
                referrer.username AS referrer_domain,
                r.created_at
            FROM referrals r
            JOIN users invited ON invited.id = r.invited_user_id
            JOIN users referrer ON referrer.id = r.referrer_user_id
            WHERE r.status = 'started'
            ORDER BY r.created_at DESC
            LIMIT %s OFFSET %s;
            """,
            (limit, offset),
        ).fetchall()

        return [dict(row) for row in rows], total


def mark_referral_visited(referral_id: int) -> tuple[bool, str, dict | None]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                r.*,
                referrer.id AS referrer_user_id,
                referrer.telegram_id AS referrer_telegram_id,
                referrer.vk_id AS referrer_vk_id,
                referrer.max_user_id AS referrer_max_id,
                invited.id AS invited_user_id,
                invited.telegram_id AS invited_telegram_id,
                invited.vk_id AS invited_vk_id,
                invited.max_user_id AS invited_max_id,
                invited.first_name AS invited_first_name
            FROM referrals r
            JOIN users referrer ON referrer.id = r.referrer_user_id
            JOIN users invited ON invited.id = r.invited_user_id
            WHERE r.id = %s;
            """,
            (referral_id,),
        ).fetchone()

        if not row:
            return False, "Реферальная запись не найдена.", None

        referral = dict(row)

        if referral["status"] == "rewarded":
            return False, "Бонус за этого приглашенного уже был начислен ранее.", None

        conn.execute(
            """
            UPDATE referrals
            SET status = 'rewarded',
                visited_at = CURRENT_TIMESTAMP,
                rewarded_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (referral_id,),
        )

        # 1. Award bonus to the inviter (referrer)
        conn.execute(
            """
            INSERT INTO rewards (user_id, referral_id, amount, status)
            VALUES (%s, %s, %s, 'active');
            """,
            (referral["referrer_user_id"], referral_id, REFERRAL_REWARD_AMOUNT),
        )

        # 2. Award bonus to the invited friend for repeat visit
        conn.execute(
            """
            INSERT INTO rewards (user_id, referral_id, amount, status)
            VALUES (%s, %s, %s, 'active');
            """,
            (referral["invited_user_id"], referral_id, REFERRAL_REWARD_AMOUNT),
        )

        rewards_sum_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = %s AND status = 'active';
            """,
            (referral["referrer_user_id"],),
        ).fetchone()

        invited_sum_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = %s AND status = 'active';
            """,
            (referral["invited_user_id"],),
        ).fetchone()

        conn.commit()

        notify_data = {
            "referrer_db_id": referral["referrer_user_id"],
            "referrer_id": referral["referrer_user_id"],
            "referrer_telegram_id": referral["referrer_telegram_id"],
            "referrer_vk_id": referral["referrer_vk_id"],
            "referrer_max_id": referral["referrer_max_id"],
            "invited_db_id": referral["invited_user_id"],
            "invited_id": referral["invited_user_id"],
            "invited_telegram_id": referral.get("invited_telegram_id"),
            "invited_vk_id": referral.get("invited_vk_id"),
            "invited_max_id": referral.get("invited_max_id"),
            "invited_first_name": referral["invited_first_name"] or "друг",
            "amount": REFERRAL_REWARD_AMOUNT,
            "new_balance": rewards_sum_row["total_sum"],
            "invited_new_balance": invited_sum_row["total_sum"],
        }

    return True, f"Визит подтвержден. Начислены бонусы по {REFERRAL_REWARD_TEXT} пригласившему и приглашенному другу.", notify_data


def get_active_rewards(limit: int = 15, offset: int = 0) -> tuple[list[dict], int]:
    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS total FROM rewards WHERE status = 'active'"
        ).fetchone()["total"]

        rows = conn.execute(
            """
            SELECT
                rewards.id,
                rewards.amount,
                rewards.created_at,
                users.id AS user_db_id,
                users.telegram_id,
                users.telegram_id AS user_telegram_id,
                users.vk_id,
                users.vk_id AS user_vk_id,
                users.max_user_id,
                users.max_user_id AS user_max_id,
                users.max_user_id AS max_id,
                users.phone,
                users.first_name,
                users.last_name,
                users.username,
                users.username AS domain
            FROM rewards
            JOIN users ON users.id = rewards.user_id
            WHERE rewards.status = 'active'
            ORDER BY rewards.created_at DESC
            LIMIT %s OFFSET %s;
            """,
            (limit, offset),
        ).fetchall()

        return [dict(row) for row in rows], total


def mark_reward_used(reward_id: int) -> tuple[bool, str, dict | None]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                rewards.id,
                rewards.amount,
                rewards.status,
                rewards.user_id,
                users.telegram_id,
                users.vk_id,
                users.max_user_id,
                users.first_name
            FROM rewards
            JOIN users ON users.id = rewards.user_id
            WHERE rewards.id = %s;
            """,
            (reward_id,),
        ).fetchone()

        if not row:
            return False, "Бонус не найден.", None

        reward = dict(row)

        if reward["status"] == "used":
            return False, "Этот бонус уже был списан ранее.", None

        conn.execute(
            """
            UPDATE rewards
            SET status = 'used',
                used_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (reward_id,),
        )

        remaining_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = %s AND status = 'active';
            """,
            (reward["user_id"],),
        ).fetchone()

        conn.commit()

        notify_data = {
            "user_db_id": reward["user_id"],
            "user_id": reward["user_id"],
            "telegram_id": reward["telegram_id"],
            "user_telegram_id": reward["telegram_id"],
            "vk_id": reward["vk_id"],
            "user_vk_id": reward["vk_id"],
            "max_user_id": reward["max_user_id"],
            "user_max_id": reward["max_user_id"],
            "first_name": reward["first_name"] or "Клиент",
            "amount": reward["amount"],
            "remaining_balance": remaining_row["total_sum"],
        }

    return True, f"Скидка {reward['amount']} ₽ успешно применена.", notify_data


def get_user_by_platform_id(platform_user_id: int, platform: str | None = None) -> dict | None:
    if platform is None:
        platform = get_current_platform()
    with get_connection() as conn:
        return _find_user(conn, platform_user_id, platform)


def is_phone_number(text: str) -> bool:
    if not text:
        return False
    digits = "".join(ch for ch in text if ch.isdigit())
    return len(digits) in (10, 11) and (text.strip().startswith("+") or digits.startswith(("7", "8", "9")))


def get_user_phone(user_identifier: int, platform: str | None = None) -> str | None:
    if platform is None:
        platform = get_current_platform()
    with get_connection() as conn:
        user = _find_user(conn, user_identifier, platform)
        if user and user.get("phone"):
            return user["phone"]
    return None


def get_user_by_phone(phone: str) -> dict | None:
    norm = normalize_phone(phone)
    if not norm:
        return None
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE phone = %s", (norm,)).fetchone()
        return dict(row) if row else None


PHONE_CONFLICT_MESSAGE = (
    "Этот номер уже привязан к другому профилю. "
    "Аккаунты не объединены; запись можно оформить на этот номер. "
    "Для объединения профилей обратитесь к мастеру."
)
PROFILE_MERGED_MESSAGE = (
    "Номер привязан, профили объединены. "
    "Теперь записи, бонусы и отзывы доступны с одного аккаунта во всех ботах."
)


def _table_columns(conn, table: str) -> set[str]:
    if conn.is_pg:
        rows = conn.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        ).fetchall()
        return {row["column_name"] for row in rows}
    rows = conn.raw_conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def merge_users(conn, primary_id: int, secondary_id: int) -> bool:
    """Merges ``secondary_id`` into ``primary_id`` (primary keeps the phone).

    Used only for an explicit exact-phone match. All client history is moved to
    the surviving profile so appointments, reviews, referrals and bonuses stay
    in one place.
    """
    if not primary_id or not secondary_id or primary_id == secondary_id:
        return True

    # Never absorb or delete a master/admin profile through a phone match.
    try:
        privileged = conn.execute(
            """
            SELECT 1 FROM user_roles
            WHERE user_id IN (%s, %s) AND role IN ('admin', 'master') AND is_active = TRUE
            LIMIT 1
            """,
            (primary_id, secondary_id),
        ).fetchone()
        if privileged:
            logger.warning("Refusing to merge users %s and %s: master/admin profile", primary_id, secondary_id)
            return False
    except Exception:
        pass

    primary_row = conn.execute("SELECT * FROM users WHERE id = %s", (primary_id,)).fetchone()
    secondary_row = conn.execute("SELECT * FROM users WHERE id = %s", (secondary_id,)).fetchone()
    if not primary_row or not secondary_row:
        return False
    primary = dict(primary_row)
    secondary = dict(secondary_row)

    # Never merge a configured master/admin bot account with a client profile.
    try:
        from config import ADMIN_IDS

        for candidate in (primary, secondary):
            for column in ("telegram_id", "vk_id", "max_user_id"):
                if candidate.get(column) and int(candidate[column]) in ADMIN_IDS:
                    logger.warning(
                        "Refusing to merge users %s and %s: configured admin account",
                        primary_id,
                        secondary_id,
                    )
                    return False
    except ImportError:
        pass

    # Two different IDs in the same column (e.g. two Telegram accounts) cannot
    # be stored in one profile. Refuse rather than silently losing an identity.
    for column in ("telegram_id", "vk_id", "max_user_id"):
        if primary.get(column) and secondary.get(column) and primary[column] != secondary[column]:
            logger.warning("Cannot merge users %s and %s: both have %s", primary_id, secondary_id, column)
            return False

    columns = _table_columns(conn, "users")
    merged_values = {
        "telegram_id": primary.get("telegram_id") or secondary.get("telegram_id"),
        "vk_id": primary.get("vk_id") or secondary.get("vk_id"),
        "max_user_id": primary.get("max_user_id") or secondary.get("max_user_id"),
        "username": primary.get("username") or secondary.get("username"),
        "first_name": primary.get("first_name") or secondary.get("first_name"),
        "last_name": primary.get("last_name") or secondary.get("last_name"),
    }
    if "password_hash" in columns:
        merged_values["password_hash"] = primary.get("password_hash") or secondary.get("password_hash")

    # Free the unique platform columns on the row that is about to be deleted,
    # otherwise copying its IDs to the primary row hits a UNIQUE constraint.
    conn.execute(
        "UPDATE users SET telegram_id = NULL, vk_id = NULL, max_user_id = NULL WHERE id = %s",
        (secondary_id,),
    )

    set_parts = []
    params = []
    for column, value in merged_values.items():
        if column in columns:
            set_parts.append(f"{column} = %s")
            params.append(value)
    if set_parts:
        params.append(primary_id)
        conn.execute(
            f"UPDATE users SET {', '.join(set_parts)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            tuple(params),
        )

    # Move all client history to the surviving profile.
    conn.execute("UPDATE appointments SET user_id = %s WHERE user_id = %s", (primary_id, secondary_id))
    conn.execute("UPDATE reviews SET user_id = %s WHERE user_id = %s", (primary_id, secondary_id))
    conn.execute("UPDATE review_notifications SET user_id = %s WHERE user_id = %s", (primary_id, secondary_id))
    conn.execute("UPDATE user_roles SET user_id = %s WHERE user_id = %s", (primary_id, secondary_id))
    try:
        conn.execute(
            "UPDATE client_login_codes SET user_id = %s WHERE user_id = %s",
            (primary_id, secondary_id),
        )
    except Exception:
        pass

    # Rewards have a unique (user_id, referral_id) index; drop only duplicates.
    conn.execute(
        """
        DELETE FROM rewards
        WHERE user_id = %s
          AND referral_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM rewards r2
              WHERE r2.user_id = %s AND r2.referral_id = rewards.referral_id
          )
        """,
        (secondary_id, primary_id),
    )
    conn.execute("UPDATE rewards SET user_id = %s WHERE user_id = %s", (primary_id, secondary_id))

    # Referrals: invited_user_id is unique, so keep the existing row if any.
    invited_rows = conn.execute(
        "SELECT id FROM referrals WHERE invited_user_id = %s", (secondary_id,)
    ).fetchall()
    for invited_row in invited_rows:
        existing = conn.execute(
            "SELECT id FROM referrals WHERE invited_user_id = %s LIMIT 1", (primary_id,)
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM referrals WHERE id = %s", (invited_row["id"],))
        else:
            conn.execute(
                "UPDATE referrals SET invited_user_id = %s WHERE id = %s",
                (primary_id, invited_row["id"]),
            )
    conn.execute(
        "UPDATE referrals SET referrer_user_id = %s WHERE referrer_user_id = %s",
        (primary_id, secondary_id),
    )
    # Preserve already earned rewards before removing self-referral relationship
    conn.execute(
        """
        UPDATE rewards
        SET referral_id = NULL
        WHERE referral_id IN (
            SELECT id FROM referrals WHERE referrer_user_id = invited_user_id
        )
        """
    )
    # A merged pair can no longer refer itself.
    conn.execute("DELETE FROM referrals WHERE referrer_user_id = invited_user_id")

    conn.execute("DELETE FROM users WHERE id = %s", (secondary_id,))
    conn.commit()
    logger.info("Merged user profile %s into %s by phone match", secondary_id, primary_id)
    return True


def link_user_phone(user_identifier: int, phone: str, platform: str | None = None) -> tuple[bool, str]:
    """Attach a phone to the current platform profile.

    If the exact phone already belongs to another profile, the two profiles are
    merged automatically (enabled by the project owner's explicit choice). The
    phone owner survives and receives all appointments, reviews, referrals and
    rewards from the current bot profile. A merge is refused when both profiles
    already contain different IDs for the same messenger, because one of those
    identities would be lost.
    """
    if platform is None:
        platform = get_current_platform()
    clean_phone = normalize_phone(phone)
    digits = "".join(ch for ch in clean_phone if ch.isdigit())
    if len(digits) < 11:
        return False, "Некорректный номер телефона."

    with get_connection() as conn:
        current_user = _find_user(conn, user_identifier, platform)
        existing = conn.execute(
            "SELECT * FROM users WHERE phone = %s",
            (clean_phone,),
        ).fetchone()

        if existing and (not current_user or existing["id"] != current_user["id"]):
            # Exact phone match: merge the current bot profile into the profile
            # that already owns the phone (the phone just entered becomes the
            # shared identity).
            if current_user and merge_users(conn, existing["id"], current_user["id"]):
                return True, PROFILE_MERGED_MESSAGE
            logger.warning(
                "Phone conflict: refusing to merge accounts for platform=%s user=%s",
                platform,
                user_identifier,
            )
            return True, PHONE_CONFLICT_MESSAGE

        if not current_user:
            try:
                conn.execute(
                    """
                    INSERT INTO users (telegram_id, vk_id, max_user_id, phone)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        user_identifier if platform == "telegram" else None,
                        user_identifier if platform == "vk" else None,
                        user_identifier if platform == "max" else None,
                        clean_phone,
                    ),
                )
                conn.commit()
            except Exception:
                conn.raw_conn.rollback()
                logger.warning(
                    "Phone conflict while creating profile for platform=%s user=%s",
                    platform,
                    user_identifier,
                )
                return True, PHONE_CONFLICT_MESSAGE
            return True, "Номер телефона успешно привязан."

        conn.execute("UPDATE users SET phone = %s WHERE id = %s", (clean_phone, current_user["id"]))
        conn.commit()
        return True, "Номер телефона сохранён."


def get_all_clients(limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
        rows = conn.execute(
            """
            SELECT id, telegram_id, vk_id, max_user_id, phone, username, first_name, last_name, created_at
            FROM users
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s;
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows], total


def search_clients(query: str, limit: int = 20) -> list[dict]:
    q = f"%{query.strip()}%"
    with get_connection() as conn:
        if conn.is_pg:
            where_sql = "WHERE first_name ILIKE %s OR last_name ILIKE %s OR phone ILIKE %s OR username ILIKE %s"
        else:
            where_sql = "WHERE LOWER(COALESCE(first_name, '')) LIKE LOWER(%s) OR LOWER(COALESCE(last_name, '')) LIKE LOWER(%s) OR LOWER(COALESCE(phone, '')) LIKE LOWER(%s) OR LOWER(COALESCE(username, '')) LIKE LOWER(%s)"

        sql = f"""
            SELECT id, telegram_id, vk_id, max_user_id, phone, username, first_name, last_name, created_at
            FROM users
            {where_sql}
            ORDER BY created_at DESC
            LIMIT %s;
        """
        rows = conn.execute(sql, (q, q, q, q, limit)).fetchall()
        return [dict(r) for r in rows]


def get_all_platform_user_ids(platform: str | None = None) -> list[int]:
    if platform is None:
        platform = get_current_platform()
    col = "telegram_id" if platform == "telegram" else ("vk_id" if platform == "vk" else "max_user_id")
    with get_connection() as conn:
        rows = conn.execute(f"SELECT {col} FROM users WHERE {col} IS NOT NULL").fetchall()
        user_ids = {dict(r)[col] for r in rows if dict(r).get(col)}

        rows2 = conn.execute(
            "SELECT DISTINCT platform_user_id FROM appointments WHERE platform = %s AND platform_user_id != 0",
            (platform,),
        ).fetchall()
        for r in rows2:
            val = dict(r).get("platform_user_id")
            if val:
                user_ids.add(val)

        return sorted(user_ids)


def get_user_role(platform_user_id: int, platform: str | None = None) -> str | None:
    if platform is None:
        platform = get_current_platform()
    try:
        from config import ADMIN_IDS
        if platform_user_id in ADMIN_IDS:
            return "admin"
    except ImportError:
        pass

    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT role FROM user_roles
                WHERE platform = %s AND platform_user_id = %s AND is_active = TRUE
                ORDER BY id DESC LIMIT 1
                """,
                (platform, platform_user_id),
            ).fetchone()
            if row:
                return dict(row)["role"]
            return None
    except Exception as e:
        logger.warning("Error fetching user role for %s (%s): %s", platform_user_id, platform, e)
        return None


def is_admin_user(platform_user_id: int, platform: str | None = None) -> bool:
    if platform is None:
        platform = get_current_platform()
    try:
        from config import ADMIN_IDS
        if platform_user_id in ADMIN_IDS:
            return True
    except ImportError:
        pass
    try:
        return get_user_role(platform_user_id, platform) == "admin"
    except Exception as e:
        logger.warning("Error in is_admin_user for %s (%s): %s", platform_user_id, platform, e)
        return False


def is_master_user(platform_user_id: int, platform: str | None = None) -> bool:
    if platform is None:
        platform = get_current_platform()
    try:
        if is_admin_user(platform_user_id, platform):
            return True
        return get_user_role(platform_user_id, platform) in ("admin", "master")
    except Exception as e:
        logger.warning("Error in is_master_user for %s (%s): %s", platform_user_id, platform, e)
        return False


def get_all_master_ids(platform: str | None = None) -> list[int]:
    if platform is None:
        platform = get_current_platform()
    try:
        from config import ADMIN_IDS
        master_ids = set(ADMIN_IDS)
    except ImportError:
        master_ids = set()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT platform_user_id FROM user_roles
            WHERE platform = %s AND role IN ('admin', 'master') AND is_active = TRUE
            """,
            (platform,),
        ).fetchall()
        for r in rows:
            master_ids.add(dict(r)["platform_user_id"])
    return sorted(master_ids)


def get_broadcast_recipients(platform: str) -> list[int]:
    """
    Get all unique user IDs for a given platform ('telegram', 'vk', 'max')
    who have interacted with the bot.
    """
    col = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_user_id"}.get(platform)
    if not col:
        return []

    with get_connection() as conn:
        rows1 = conn.execute(
            f"SELECT DISTINCT {col} FROM users WHERE {col} IS NOT NULL AND {col} != 0"
        ).fetchall()
        user_ids = {dict(row)[col] for row in rows1 if dict(row).get(col)}

        rows2 = conn.execute(
            "SELECT DISTINCT platform_user_id FROM appointments WHERE platform = %s AND platform_user_id != 0",
            (platform,),
        ).fetchall()
        for r in rows2:
            val = dict(r).get("platform_user_id")
            if val:
                user_ids.add(val)

        return sorted(user_ids)


def assign_user_role(
    platform_or_user_id,
    user_id_or_platform,
    role: str = "master",
    permissions: str = "all",
) -> bool:
    if isinstance(platform_or_user_id, int) or (isinstance(platform_or_user_id, str) and str(platform_or_user_id).isdigit()):
        platform_user_id = int(platform_or_user_id)
        platform = str(user_id_or_platform)
    else:
        platform = str(platform_or_user_id)
        platform_user_id = int(user_id_or_platform)

    try:
        with get_connection() as conn:
            user = _find_user(conn, platform_user_id, platform)
            user_id = user["id"] if user else None

            existing = conn.execute(
                "SELECT id FROM user_roles WHERE platform = %s AND platform_user_id = %s",
                (platform, platform_user_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE user_roles
                    SET role = %s, permissions = %s, is_active = TRUE
                    WHERE id = %s
                    """,
                    (role, permissions, existing["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO user_roles (user_id, platform, platform_user_id, role, permissions, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    """,
                    (user_id, platform, platform_user_id, role, permissions),
                )
            conn.commit()
            return True
    except Exception as e:
        logger.error("Failed to assign role %s to %s: %s", role, platform_user_id, e)
        return False


def revoke_user_role(platform_or_user_id, user_id_or_platform) -> bool:
    if isinstance(platform_or_user_id, int) or (isinstance(platform_or_user_id, str) and str(platform_or_user_id).isdigit()):
        platform_user_id = int(platform_or_user_id)
        platform = str(user_id_or_platform)
    else:
        platform = str(platform_or_user_id)
        platform_user_id = int(user_id_or_platform)

    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE user_roles SET is_active = FALSE WHERE platform = %s AND platform_user_id = %s",
                (platform, platform_user_id),
            )
            conn.commit()
            return True
    except Exception as e:
        logger.error("Failed to revoke role from %s: %s", platform_user_id, e)
        return False


assign_role = assign_user_role
revoke_role = revoke_user_role
