import logging
from aiogram import Bot
from aiogram.utils.deep_linking import create_start_link

from config import REFERRAL_REWARD_AMOUNT, REFERRAL_REWARD_TEXT
from database import get_connection

logger = logging.getLogger(__name__)


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
) -> tuple[int, bool]:
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

        if not user and phone:
            row = conn.execute("SELECT * FROM users WHERE phone = %s", (phone,)).fetchone()
            user = dict(row) if row else None

        is_new = user is None

        if is_new:
            conn.execute(
                """
                INSERT INTO users (telegram_id, vk_id, max_user_id, phone, username, first_name, last_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (telegram_id, vk_id, max_user_id, phone, username, first_name, last_name),
            )
            if telegram_id:
                row = conn.execute("SELECT id FROM users WHERE telegram_id = %s", (telegram_id,)).fetchone()
            elif vk_id:
                row = conn.execute("SELECT id FROM users WHERE vk_id = %s", (vk_id,)).fetchone()
            elif max_user_id:
                row = conn.execute("SELECT id FROM users WHERE max_user_id = %s", (max_user_id,)).fetchone()
            else:
                row = conn.execute("SELECT id FROM users WHERE phone = %s", (phone,)).fetchone()
            user_id = row["id"]
        else:
            user_id = user["id"]
            conn.execute(
                """
                UPDATE users
                SET telegram_id = COALESCE(%s, telegram_id),
                    vk_id = COALESCE(%s, vk_id),
                    max_user_id = COALESCE(%s, max_user_id),
                    phone = COALESCE(%s, phone),
                    username = COALESCE(%s, username),
                    first_name = COALESCE(%s, first_name),
                    last_name = COALESCE(%s, last_name),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (telegram_id, vk_id, max_user_id, phone, username, first_name, last_name, user_id),
            )

        return user_id, is_new


def save_user(
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
) -> tuple[int, bool]:
    return _save_user_unified(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
    )


async def build_referral_link(bot: Bot, user_id: int) -> str:
    return await create_start_link(bot, f"ref_{user_id}", encode=False)


def register_referral(new_user_telegram_id: int, referrer_telegram_id: int) -> tuple[bool, str, dict | None]:
    if new_user_telegram_id == referrer_telegram_id:
        return False, "Нельзя пригласить самого себя.", None

    with get_connection() as conn:
        new_user = _find_user(conn, new_user_telegram_id, "telegram")
        if not new_user:
            return False, "Пользователь не найден.", None

        referrer = _find_user(conn, referrer_telegram_id)

        if not referrer:
            conn.execute("INSERT INTO users (telegram_id) VALUES (%s)", (referrer_telegram_id,))
            referrer = _find_user(conn, referrer_telegram_id, "telegram")

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

        notify_data = {
            "referrer_id": referrer["id"],
            "referrer_telegram_id": referrer.get("telegram_id") or referrer_telegram_id,
            "referrer_vk_id": referrer.get("vk_id"),
            "referrer_max_id": referrer.get("max_user_id"),
            "referrer_first_name": referrer["first_name"] or "Друг",
            "invited_first_name": new_user["first_name"] or "Новый клиент",
        }

    return True, "Реферальная связь сохранена.", notify_data


def get_referral_stats(referrer_telegram_id: int, platform: str | None = "telegram") -> dict:
    with get_connection() as conn:
        referrer = _find_user(conn, referrer_telegram_id, platform)
        if not referrer:
            return {
                "started": 0,
                "confirmed": 0,
                "active_rewards_count": 0,
                "active_rewards_sum": 0,
            }

        started = conn.execute(
            "SELECT COUNT(*) AS total FROM referrals WHERE referrer_user_id = %s",
            (referrer["id"],),
        ).fetchone()["total"]

        confirmed = conn.execute(
            "SELECT COUNT(*) AS total FROM referrals WHERE referrer_user_id = %s AND status = 'rewarded'",
            (referrer["id"],),
        ).fetchone()["total"]

        rewards = conn.execute(
            """
            SELECT COUNT(*) AS total_count, COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = %s AND status = 'active'
            """,
            (referrer["id"],),
        ).fetchone()

        return {
            "started": started,
            "confirmed": confirmed,
            "active_rewards_count": rewards["total_count"],
            "active_rewards_sum": rewards["total_sum"],
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
                referrer.id AS referrer_db_id,
                referrer.telegram_id AS referrer_telegram_id,
                referrer.vk_id AS referrer_vk_id,
                referrer.max_user_id AS referrer_max_id,
                referrer.phone AS referrer_phone,
                referrer.first_name AS referrer_first_name,
                referrer.last_name AS referrer_last_name,
                referrer.username AS referrer_username,
                r.created_at
            FROM referrals r
            JOIN users invited ON invited.id = r.invited_user_id
            JOIN users referrer ON referrer.id = r.referrer_user_id
            WHERE r.status = 'started'
            ORDER BY r.created_at ASC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()

        return [dict(row) for row in rows], total


def mark_referral_visited(referral_id: int) -> tuple[bool, str, dict | None]:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE referrals
            SET status = 'rewarded',
                visited_at = CURRENT_TIMESTAMP,
                rewarded_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'started'
            """,
            (referral_id,),
        )

        if cur.rowcount == 0:
            return False, "Этот визит уже был подтвержден ранее или не найден.", None

        referral = conn.execute(
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
            WHERE r.id = %s
            """,
            (referral_id,),
        ).fetchone()

        conn.execute(
            """
            INSERT INTO rewards (user_id, referral_id, amount, status)
            VALUES (%s, %s, %s, 'active')
            """,
            (referral["referrer_user_id"], referral_id, REFERRAL_REWARD_AMOUNT),
        )

        rewards_sum_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = %s AND status = 'active'
            """,
            (referral["referrer_user_id"],),
        ).fetchone()

        notify_data = {
            "referrer_db_id": referral["referrer_user_id"],
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
                users.vk_id,
                users.max_user_id,
                users.phone,
                users.first_name,
                users.last_name,
                users.username
            FROM rewards
            JOIN users ON users.id = rewards.user_id
            WHERE rewards.status = 'active'
            ORDER BY rewards.created_at ASC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()

        return [dict(row) for row in rows], total


def mark_reward_used(reward_id: int) -> tuple[bool, str, dict | None]:
    with get_connection() as conn:
        reward = conn.execute(
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
            WHERE rewards.id = %s
            """,
            (reward_id,),
        ).fetchone()

        if not reward:
            return False, "Бонус не найден.", None

        cur = conn.execute(
            """
            UPDATE rewards
            SET status = 'used',
                used_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'active'
            """,
            (reward_id,),
        )

        if cur.rowcount == 0:
            return False, "Бонус уже был списан ранее.", None

        remaining_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = %s AND status = 'active'
            """,
            (reward["user_id"],),
        ).fetchone()

        notify_data = {
            "user_db_id": reward["user_id"],
            "user_telegram_id": reward["telegram_id"],
            "telegram_id": reward["telegram_id"],
            "user_vk_id": reward["vk_id"],
            "vk_id": reward["vk_id"],
            "max_user_id": reward["max_user_id"],
            "first_name": reward["first_name"] or "Клиент",
            "amount": reward["amount"],
            "remaining_balance": remaining_row["total_sum"],
        }

    return True, f"Бонус {reward['amount']} ₽ отмечен как использованный.", notify_data


def normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return f"+{digits}" if digits else phone


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


def link_user_phone(user_identifier: int, phone: str, platform: str | None = None) -> tuple[bool, str]:
    clean_phone = normalize_phone(phone)
    digits = "".join(ch for ch in clean_phone if ch.isdigit())
    if len(digits) < 11:
        return False, "Некорректный номер телефона."

    with get_connection() as conn:
        current_user = _find_user(conn, user_identifier, platform)
        if not current_user:
            return False, "Пользователь не найден."

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
            conn.execute("DELETE FROM users WHERE id = %s", (user_db_id,))
            return True, "Профиль успешно объединён по номеру телефона."
        else:
            conn.execute("UPDATE users SET phone = %s WHERE id = %s", (clean_phone, user_db_id))
            return True, "Номер телефона сохранён."
