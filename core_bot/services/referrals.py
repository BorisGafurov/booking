import logging
import re
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


def _find_user(conn, identifier: int, platform: str | None = None) -> dict | None:
    if platform == "telegram":
        row = conn.execute("SELECT * FROM users WHERE telegram_id = %s", (identifier,)).fetchone()
        if row:
            return dict(row)
    elif platform == "vk":
        row = conn.execute("SELECT * FROM users WHERE vk_id = %s", (identifier,)).fetchone()
        if row:
            return dict(row)
    elif platform == "max":
        row = conn.execute("SELECT * FROM users WHERE max_user_id = %s", (identifier,)).fetchone()
        if row:
            return dict(row)

    row = conn.execute("SELECT * FROM users WHERE id = %s", (identifier,)).fetchone()
    if row:
        return dict(row)

    row = conn.execute(
        """
        SELECT * FROM users
        WHERE telegram_id = %s OR vk_id = %s OR max_user_id = %s
        """,
        (identifier, identifier, identifier),
    ).fetchone()
    return dict(row) if row else None


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
        if telegram_id:
            row = conn.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,)).fetchone()
            user = dict(row) if row else None
        elif vk_id:
            row = conn.execute("SELECT * FROM users WHERE vk_id = %s", (vk_id,)).fetchone()
            user = dict(row) if row else None
        elif max_user_id:
            row = conn.execute("SELECT * FROM users WHERE max_user_id = %s", (max_user_id,)).fetchone()
            user = dict(row) if row else None

        if not user:
            cursor = conn.execute(
                """
                INSERT INTO users (telegram_id, vk_id, max_user_id, username, first_name, last_name, phone)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (telegram_id, vk_id, max_user_id, uname, first_name, last_name, phone),
            )
            inserted_id = cursor.fetchone()[0]
            conn.commit()
            return inserted_id, True

        conn.execute(
            """
            UPDATE users
            SET username = %s,
                first_name = %s,
                last_name = %s,
                phone = COALESCE(%s, phone),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (uname, first_name, last_name, phone, user["id"]),
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
    platform: str = "telegram",
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
        platform = "telegram"

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

        conn.execute(
            """
            INSERT INTO rewards (user_id, referral_id, amount, status)
            VALUES (%s, %s, %s, 'active');
            """,
            (referral["referrer_user_id"], referral_id, REFERRAL_REWARD_AMOUNT),
        )

        rewards_sum_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = %s AND status = 'active';
            """,
            (referral["referrer_user_id"],),
        ).fetchone()

        conn.commit()

        notify_data = {
            "referrer_db_id": referral["referrer_user_id"],
            "referrer_id": referral["referrer_user_id"],
            "referrer_telegram_id": referral["referrer_telegram_id"],
            "referrer_vk_id": referral["referrer_vk_id"],
            "referrer_max_id": referral["referrer_max_id"],
            "invited_first_name": referral["invited_first_name"] or "друг",
            "amount": REFERRAL_REWARD_AMOUNT,
            "new_balance": rewards_sum_row["total_sum"],
        }

    return True, f"Визит подтвержден. Бонус {REFERRAL_REWARD_TEXT} начислен.", notify_data


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


def get_user_by_platform_id(platform_user_id: int, platform: str = "telegram") -> dict | None:
    with get_connection() as conn:
        return _find_user(conn, platform_user_id, platform)


def is_phone_number(text: str) -> bool:
    if not text:
        return False
    digits = "".join(ch for ch in text if ch.isdigit())
    return len(digits) in (10, 11) and (text.strip().startswith("+") or digits.startswith(("7", "8", "9")))


def get_user_phone(user_identifier: int, platform: str | None = None) -> str | None:
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


def link_user_phone(user_identifier: int, phone: str, platform: str | None = None) -> tuple[bool, str]:
    clean_phone = normalize_phone(phone)
    digits = "".join(ch for ch in clean_phone if ch.isdigit())
    if len(digits) < 11:
        return False, "Некорректный номер телефона."

    with get_connection() as conn:
        current_user = _find_user(conn, user_identifier, platform)
        if not current_user:
            _save_user_unified(
                telegram_id=user_identifier if platform == "telegram" else None,
                vk_id=user_identifier if platform == "vk" else None,
                max_user_id=user_identifier if platform == "max" else None,
                phone=clean_phone,
            )
            return True, "Номер телефона успешно привязан."

        user_db_id = current_user["id"]
        existing = conn.execute(
            "SELECT * FROM users WHERE phone = %s AND id != %s",
            (clean_phone, user_db_id),
        ).fetchone()

        if existing:
            target_id = existing["id"]
            # Clear unique fields on old record first to prevent UNIQUE constraint conflict during transfer
            conn.execute(
                "UPDATE users SET telegram_id = NULL, vk_id = NULL, max_user_id = NULL, phone = NULL WHERE id = %s",
                (user_db_id,),
            )
            conn.execute(
                """
                UPDATE users
                SET telegram_id = COALESCE(telegram_id, %s),
                    vk_id = COALESCE(vk_id, %s),
                    max_user_id = COALESCE(max_user_id, %s),
                    username = COALESCE(username, %s),
                    first_name = COALESCE(first_name, %s),
                    last_name = COALESCE(last_name, %s),
                    phone = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    current_user.get("telegram_id"),
                    current_user.get("vk_id"),
                    current_user.get("max_user_id"),
                    current_user.get("username"),
                    current_user.get("first_name"),
                    current_user.get("last_name"),
                    clean_phone,
                    target_id,
                ),
            )
            conn.execute("UPDATE referrals SET referrer_user_id = %s WHERE referrer_user_id = %s", (target_id, user_db_id))
            
            existing_invite = conn.execute("SELECT id FROM referrals WHERE invited_user_id = %s", (target_id,)).fetchone()
            if existing_invite:
                conn.execute("DELETE FROM referrals WHERE invited_user_id = %s", (user_db_id,))
            else:
                conn.execute("UPDATE referrals SET invited_user_id = %s WHERE invited_user_id = %s", (target_id, user_db_id))

            conn.execute("DELETE FROM referrals WHERE referrer_user_id = %s AND invited_user_id = %s", (target_id, target_id))
            conn.execute("UPDATE rewards SET user_id = %s WHERE user_id = %s", (target_id, user_db_id))
            conn.execute("UPDATE appointments SET user_id = %s WHERE user_id = %s", (target_id, user_db_id))
            conn.execute("UPDATE user_roles SET user_id = %s WHERE user_id = %s", (target_id, user_db_id))
            conn.execute("DELETE FROM users WHERE id = %s", (user_db_id,))
            conn.commit()
            return True, "Профиль успешно объединён по номеру телефона."
        else:
            conn.execute("UPDATE users SET phone = %s WHERE id = %s", (clean_phone, user_db_id))
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
        rows = conn.execute(
            """
            SELECT id, telegram_id, vk_id, max_user_id, phone, username, first_name, last_name, created_at
            FROM users
            WHERE first_name ILIKE %s OR last_name ILIKE %s OR phone ILIKE %s OR username ILIKE %s
            ORDER BY created_at DESC
            LIMIT %s;
            """,
            (q, q, q, q, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_platform_user_ids(platform: str = "telegram") -> list[int]:
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


def get_user_role(platform_user_id: int, platform: str = "telegram") -> str | None:
    try:
        from config import ADMIN_IDS
        if platform_user_id in ADMIN_IDS:
            return "admin"
    except ImportError:
        pass

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


def is_admin_user(platform_user_id: int, platform: str = "telegram") -> bool:
    try:
        from config import ADMIN_IDS
        if platform_user_id in ADMIN_IDS:
            return True
    except ImportError:
        pass
    return get_user_role(platform_user_id, platform) == "admin"


def is_master_user(platform_user_id: int, platform: str = "telegram") -> bool:
    if is_admin_user(platform_user_id, platform):
        return True
    return get_user_role(platform_user_id, platform) in ("admin", "master")


def get_all_master_ids(platform: str = "telegram") -> list[int]:
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
