from aiogram import Bot
from aiogram.utils.deep_linking import create_start_link

from config import REFERRAL_REWARD_AMOUNT, REFERRAL_REWARD_TEXT
from database import get_connection


def save_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None = None,
) -> tuple[int, bool]:
    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()

        is_new = existing is None

        if is_new:
            conn.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
                """,
                (telegram_id, username, first_name, last_name),
            )
        else:
            conn.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, last_name = ?
                WHERE telegram_id = ?
                """,
                (username, first_name, last_name, telegram_id),
            )

        row = conn.execute(
            """
            SELECT id
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()

        return row["id"], is_new


async def build_referral_link(bot: Bot, user_id: int) -> str:
    return await create_start_link(bot, f"ref_{user_id}", encode=False)


def register_referral(new_user_telegram_id: int, referrer_telegram_id: int) -> tuple[bool, str, dict | None]:
    if new_user_telegram_id == referrer_telegram_id:
        return False, "Нельзя пригласить самого себя.", None

    with get_connection() as conn:
        new_user = conn.execute(
            """
            SELECT id, first_name
            FROM users
            WHERE telegram_id = ?
            """,
            (new_user_telegram_id,),
        ).fetchone()

        if not new_user:
            return False, "Пользователь не найден.", None

        # Check if user was already invited
        existing = conn.execute(
            """
            SELECT id
            FROM referrals
            WHERE invited_user_id = ?
            """,
            (new_user["id"],),
        ).fetchone()

        if existing:
            return False, "Вы уже были зарегистрированы по реферальной ссылке ранее.", None

        # Ensure referrer exists in users table (create if they shared link without /start)
        referrer = conn.execute(
            """
            SELECT id, first_name
            FROM users
            WHERE telegram_id = ?
            """,
            (referrer_telegram_id,),
        ).fetchone()

        if not referrer:
            conn.execute(
                """
                INSERT OR IGNORE INTO users (telegram_id)
                VALUES (?)
                """,
                (referrer_telegram_id,),
            )
            referrer = conn.execute(
                """
                SELECT id, first_name
                FROM users
                WHERE telegram_id = ?
                """,
                (referrer_telegram_id,),
            ).fetchone()

        if not referrer:
            return False, "Пригласивший пользователь не найден.", None

        conn.execute(
            """
            INSERT INTO referrals (referrer_user_id, invited_user_id, status)
            VALUES (?, ?, 'started')
            """,
            (referrer["id"], new_user["id"]),
        )

        notify_data = {
            "referrer_telegram_id": referrer_telegram_id,
            "referrer_first_name": referrer["first_name"] or "Друг",
            "invited_first_name": new_user["first_name"] or "Новый клиент",
        }

    return True, "Реферальная связь сохранена.", notify_data


def get_referral_stats(referrer_telegram_id: int) -> dict:
    with get_connection() as conn:
        referrer = conn.execute(
            """
            SELECT id
            FROM users
            WHERE telegram_id = ?
            """,
            (referrer_telegram_id,),
        ).fetchone()

        if not referrer:
            return {
                "started": 0,
                "confirmed": 0,
                "active_rewards_count": 0,
                "active_rewards_sum": 0,
            }

        started = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE referrer_user_id = ?
            """,
            (referrer["id"],),
        ).fetchone()["total"]

        confirmed = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE referrer_user_id = ? AND status = 'rewarded'
            """,
            (referrer["id"],),
        ).fetchone()["total"]

        rewards = conn.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = ? AND status = 'active'
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
            """
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE status = 'started'
            """
        ).fetchone()["total"]

        rows = conn.execute(
            """
            SELECT
                r.id,
                invited.telegram_id AS invited_telegram_id,
                invited.first_name AS invited_first_name,
                invited.last_name AS invited_last_name,
                invited.username AS invited_username,
                referrer.telegram_id AS referrer_telegram_id,
                referrer.first_name AS referrer_first_name,
                referrer.last_name AS referrer_last_name,
                referrer.username AS referrer_username,
                r.created_at
            FROM referrals r
            JOIN users invited ON invited.id = r.invited_user_id
            JOIN users referrer ON referrer.id = r.referrer_user_id
            WHERE r.status = 'started'
            ORDER BY r.created_at ASC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

        return [dict(row) for row in rows], total


def mark_referral_visited(referral_id: int) -> tuple[bool, str, dict | None]:
    with get_connection() as conn:
        # Atomic update with WHERE status = 'started' to prevent race conditions
        cur = conn.execute(
            """
            UPDATE referrals
            SET status = 'rewarded',
                visited_at = CURRENT_TIMESTAMP,
                rewarded_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'started'
            """,
            (referral_id,),
        )

        if cur.rowcount == 0:
            return False, "Этот визит уже был подтвержден ранее или не найден.", None

        referral = conn.execute(
            """
            SELECT
                r.*,
                referrer.telegram_id AS referrer_telegram_id,
                invited.first_name AS invited_first_name
            FROM referrals r
            JOIN users referrer ON referrer.id = r.referrer_user_id
            JOIN users invited ON invited.id = r.invited_user_id
            WHERE r.id = ?
            """,
            (referral_id,),
        ).fetchone()

        conn.execute(
            """
            INSERT INTO rewards (user_id, referral_id, amount, status)
            VALUES (?, ?, ?, 'active')
            """,
            (referral["referrer_user_id"], referral_id, REFERRAL_REWARD_AMOUNT),
        )

        rewards_sum_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = ? AND status = 'active'
            """,
            (referral["referrer_user_id"],),
        ).fetchone()

        notify_data = {
            "referrer_telegram_id": referral["referrer_telegram_id"],
            "invited_first_name": referral["invited_first_name"] or "друг",
            "amount": REFERRAL_REWARD_AMOUNT,
            "new_balance": rewards_sum_row["total_sum"],
        }

    return True, f"Визит подтвержден. Бонус {REFERRAL_REWARD_TEXT} начислен.", notify_data


def get_active_rewards(limit: int = 15, offset: int = 0) -> tuple[list[dict], int]:
    with get_connection() as conn:
        total = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM rewards
            WHERE status = 'active'
            """
        ).fetchone()["total"]

        rows = conn.execute(
            """
            SELECT
                rewards.id,
                rewards.amount,
                rewards.created_at,
                users.telegram_id,
                users.first_name,
                users.last_name,
                users.username
            FROM rewards
            JOIN users ON users.id = rewards.user_id
            WHERE rewards.status = 'active'
            ORDER BY rewards.created_at ASC
            LIMIT ? OFFSET ?
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
                users.first_name
            FROM rewards
            JOIN users ON users.id = rewards.user_id
            WHERE rewards.id = ?
            """,
            (reward_id,),
        ).fetchone()

        if not reward:
            return False, "Бонус не найден.", None

        # Atomic update with WHERE status = 'active'
        cur = conn.execute(
            """
            UPDATE rewards
            SET status = 'used',
                used_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'active'
            """,
            (reward_id,),
        )

        if cur.rowcount == 0:
            return False, "Бонус уже был списан ранее.", None

        remaining_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total_sum
            FROM rewards
            WHERE user_id = ? AND status = 'active'
            """,
            (reward["user_id"],),
        ).fetchone()

        notify_data = {
            "user_telegram_id": reward["telegram_id"],
            "first_name": reward["first_name"] or "Клиент",
            "amount": reward["amount"],
            "remaining_balance": remaining_row["total_sum"],
        }

    return True, f"Бонус {reward['amount']} ₽ отмечен как использованный.", notify_data

