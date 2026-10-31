"""Shared review service for the site and all three bots.

Rules (same everywhere):
- only a client who owns a completed and paid visit can leave a review;
- one review per appointment;
- rating is an integer from 1 to 5;
- comment is 10..500 characters, no emoji, no links, no HTML;
- repeated whitespace is collapsed, the first letter and letters after
  sentence-ending punctuation are capitalised;
- a review can be edited by its author for 24 hours after creation.
"""

from __future__ import annotations

import datetime
import logging
import re
import unicodedata
from typing import Any

from core_bot.database import get_connection

logger = logging.getLogger(__name__)

MIN_COMMENT_LENGTH = 10
MAX_COMMENT_LENGTH = 500
EDIT_WINDOW_HOURS = 24
ALLOWED_SOURCES = {"site", "telegram", "vk", "max"}

_PLATFORM_COLUMNS = {
    "telegram": "telegram_id",
    "vk": "vk_id",
    "max": "max_user_id",
}

_LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|vk\.com/|vk\.ru/|"
    r"instagram\.com/|youtube\.com/|youtu\.be/|wa\.me/|clck\.ru/|"
    r"\b(?:[a-z0-9-]+\.)+(?:ru|com|net|org|io|me|tv|рф)\b)",
    re.IGNORECASE,
)
_HTML_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>|javascript:", re.IGNORECASE)
_SENTENCE_CAPITAL_RE = re.compile(r"([.!?…])(\s*)([a-zа-яё])", re.IGNORECASE)


def _contains_emoji(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if code in (0x200D, 0x20E3, 0xFE0F):
            return True
        if 0x1F000 <= code <= 0x1FAFF:
            return True
        if 0x1F1E6 <= code <= 0x1F1FF:
            return True
        if 0x2600 <= code <= 0x27BF:
            return True
        if 0x2B00 <= code <= 0x2BFF:
            return True
        category = unicodedata.category(ch)
        if category in {"So", "Sk"}:
            return True
    return False


def _contains_link(text: str) -> bool:
    return bool(_LINK_RE.search(text))


def _contains_html(text: str) -> bool:
    return bool(_HTML_RE.search(text))


def _capitalize_first_letter(text: str) -> str:
    for index, ch in enumerate(text):
        if ch.isalpha():
            return text[:index] + ch.upper() + text[index + 1 :]
    return text


def normalize_review_comment(value: Any) -> str:
    """Normalises any input into the canonical review-comment string."""
    if not isinstance(value, str):
        return ""

    text = unicodedata.normalize("NFC", value)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = _capitalize_first_letter(text)

    def _upper_sentence(match: re.Match[str]) -> str:
        return match.group(1) + match.group(2) + match.group(3).upper()

    text = _SENTENCE_CAPITAL_RE.sub(_upper_sentence, text)
    return text


def parse_rating(value: Any) -> int | None:
    """Returns a strict integer rating 1..5, or None for invalid input."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        rating = value
    elif isinstance(value, str) and re.fullmatch(r"[1-5]", value.strip()):
        rating = int(value.strip())
    else:
        return None
    return rating if 1 <= rating <= 5 else None


def validate_review_comment(value: Any) -> tuple[bool, str, str | None]:
    if not isinstance(value, str):
        return False, "", "Комментарий должен быть текстом."

    text = normalize_review_comment(value)

    if len(text) < MIN_COMMENT_LENGTH:
        return False, text, f"Комментарий должен содержать минимум {MIN_COMMENT_LENGTH} символов."
    if len(text) > MAX_COMMENT_LENGTH:
        return False, text, f"Комментарий должен содержать не более {MAX_COMMENT_LENGTH} символов."
    if not any(ch.isalpha() for ch in text):
        return False, text, "Комментарий должен содержать хотя бы одну букву."
    if _contains_emoji(text):
        return False, text, "Эмодзи в отзыве не поддерживаются — оставьте, пожалуйста, текст."
    if _contains_link(text):
        return False, text, "Ссылки в отзыве запрещены."
    if _contains_html(text):
        return False, text, "HTML-теги в отзыве запрещены."

    for ch in text:
        if ch.isspace():
            continue
        category = unicodedata.category(ch)
        if category[0] in {"L", "N", "P", "Z"} or category in {"Sc", "Sm"}:
            continue
        return False, text, "В комментарии есть недопустимые символы."

    return True, text, None


def validate_review(rating: Any, comment: Any) -> tuple[bool, int, str, str | None]:
    parsed_rating = parse_rating(rating)
    if parsed_rating is None:
        return False, 0, "", "Оценка должна быть целым числом от 1 до 5."

    ok, text, error = validate_review_comment(comment)
    if not ok:
        return False, parsed_rating, text, error

    return True, parsed_rating, text, None


def _as_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _as_datetime(value: Any) -> datetime.datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return None
    return None


def _can_edit_now(created_at: Any) -> bool:
    created = _as_datetime(created_at)
    if created is None:
        return False
    return datetime.datetime.now(datetime.timezone.utc) - created < datetime.timedelta(
        hours=EDIT_WINDOW_HOURS
    )


def _edit_until(created_at: Any) -> str | None:
    created = _as_datetime(created_at)
    if created is None:
        return None
    return (created + datetime.timedelta(hours=EDIT_WINDOW_HOURS)).isoformat()


def _platform_user_id(appointment: dict[str, Any], user: dict[str, Any] | None) -> tuple[str, int] | None:
    platform = str(appointment.get("platform") or "").lower()
    platform_user_id = appointment.get("platform_user_id")
    if platform in _PLATFORM_COLUMNS and platform_user_id:
        return platform, int(platform_user_id)

    if user:
        for candidate in ("telegram", "vk", "max"):
            value = user.get(_PLATFORM_COLUMNS[candidate])
            if value:
                return candidate, int(value)
    return None


def _get_user(
    conn,
    user_id: int | None = None,
    platform: str | None = None,
    platform_user_id: int | None = None,
) -> dict[str, Any] | None:
    if user_id is not None:
        row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        return _as_dict(row)

    if platform and platform_user_id is not None:
        column = _PLATFORM_COLUMNS.get(platform)
        if not column:
            return None
        row = conn.execute(
            f"SELECT * FROM users WHERE {column} = %s", (platform_user_id,)
        ).fetchone()
        return _as_dict(row)
    return None


def _phone_matches(appointment: dict[str, Any], user: dict[str, Any]) -> bool:
    from core_bot.services.referrals import normalize_phone

    return bool(
        appointment.get("phone")
        and user.get("phone")
        and normalize_phone(appointment["phone"]) == normalize_phone(user["phone"])
    )


def _owns_appointment(appointment: dict[str, Any], user: dict[str, Any]) -> bool:
    if appointment.get("user_id") is not None and int(appointment["user_id"]) == int(user["id"]):
        return True
    return _phone_matches(appointment, user)


def _is_paid(appointment: dict[str, Any]) -> bool:
    return bool(appointment.get("payment_confirmed"))


def get_reviewable_appointments(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with get_connection() as conn:
        user = _get_user(conn, user_id=user_id)
        if not user:
            return []
        val_true = True if conn.is_pg else 1
        rows = conn.execute(
            """
            SELECT a.id, a.services, a.total_price, a.appointment_time, a.platform
            FROM appointments a
            LEFT JOIN reviews r ON r.appointment_id = a.id
            WHERE (a.user_id = %s OR a.phone = %s)
              AND a.status = 'completed'
              AND a.payment_confirmed = %s
              AND r.id IS NULL
            ORDER BY a.appointment_time DESC
            LIMIT %s
            """,
            (user_id, user.get("phone") or "", val_true, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_user_for_platform(platform: str, platform_user_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        user = _get_user(conn, platform=platform, platform_user_id=platform_user_id)
        if user:
            return user
        col = _PLATFORM_COLUMNS.get(platform)
        if not col:
            return None
        row = conn.execute(
            """
            SELECT u.* FROM users u
            JOIN appointments a ON a.user_id = u.id
            WHERE a.platform = %s AND a.platform_user_id = %s
            ORDER BY a.appointment_time DESC LIMIT 1
            """,
            (platform, platform_user_id),
        ).fetchone()
        if row:
            conn.execute(f"UPDATE users SET {col} = %s WHERE id = %s", (platform_user_id, row["id"]))
            conn.commit()
            return dict(row)
        return None


def get_reviewable_for_platform(platform: str, platform_user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    user = get_user_for_platform(platform, platform_user_id)
    if not user:
        return []
    return get_reviewable_appointments(int(user["id"]), limit=limit)


def can_review_platform_appointment(
    platform: str,
    platform_user_id: int,
    appointment_id: int,
) -> tuple[bool, str, dict[str, Any] | None]:
    user = get_user_for_platform(platform, platform_user_id)
    if not user:
        return False, "Клиент не найден.", None
    return can_review_appointment(int(user["id"]), appointment_id)


def create_review_from_platform(
    platform: str,
    platform_user_id: int,
    appointment_id: int,
    rating: Any,
    comment: Any,
) -> tuple[bool, str, dict[str, Any] | None]:
    user = get_user_for_platform(platform, platform_user_id)
    if not user:
        return False, "Клиент не найден.", None
    return create_review(
        user_id=int(user["id"]),
        appointment_id=appointment_id,
        rating=rating,
        comment=comment,
        source=platform,
    )


def get_reviews_for_user(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.appointment_id, r.rating, r.comment, r.source, r.status,
                   r.created_at, r.updated_at,
                   a.services, a.total_price, a.appointment_time, a.platform
            FROM reviews r
            JOIN appointments a ON a.id = r.appointment_id
            WHERE r.user_id = %s
            ORDER BY r.created_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()
        return [
            {
                **dict(row),
                "can_edit": _can_edit_now(row["created_at"]),
                "edit_until": _edit_until(row["created_at"]),
            }
            for row in rows
        ]


def get_review_for_appointment(appointment_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM reviews WHERE appointment_id = %s", (appointment_id,)
        ).fetchone()
        return _as_dict(row)


def get_review_for_user(user_id: int, review_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM reviews WHERE id = %s AND user_id = %s", (review_id, user_id)
        ).fetchone()
        return _as_dict(row)


def get_public_reviews(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.rating, r.comment, r.source, r.created_at,
                   u.first_name,
                   a.services, a.appointment_time
            FROM reviews r
            JOIN users u ON u.id = r.user_id
            JOIN appointments a ON a.id = r.appointment_id
            WHERE r.status = 'published'
            ORDER BY r.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()

        stats_row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(AVG(rating), 0) AS average,
                   SUM(CASE WHEN rating = 5 THEN 1 ELSE 0 END) AS five,
                   SUM(CASE WHEN rating = 4 THEN 1 ELSE 0 END) AS four,
                   SUM(CASE WHEN rating = 3 THEN 1 ELSE 0 END) AS three,
                   SUM(CASE WHEN rating = 2 THEN 1 ELSE 0 END) AS two,
                   SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS one
            FROM reviews
            WHERE status = 'published'
            """
        ).fetchone()
        stats = dict(stats_row) if stats_row else {}
        total = int(stats.get("total") or 0)
        average = round(float(stats.get("average") or 0), 2) if total else 0

        reviews = []
        for row in rows:
            item = dict(row)
            name = str(item.get("first_name") or "").strip()
            reviews.append(
                {
                    "id": item["id"],
                    "author_name": name or "Клиент",
                    "rating": int(item["rating"]),
                    "comment": item["comment"],
                    "source": item.get("source") or "site",
                    "created_at": item.get("created_at"),
                    "services": item.get("services") or "",
                    "appointment_time": item.get("appointment_time"),
                }
            )

        return {
            "reviews": reviews,
            "stats": {
                "total": total,
                "average": average,
                "five": int(stats.get("five") or 0),
                "four": int(stats.get("four") or 0),
                "three": int(stats.get("three") or 0),
                "two": int(stats.get("two") or 0),
                "one": int(stats.get("one") or 0),
            },
        }


def get_master_reviews(limit: int = 100, offset: int = 0, rating: int | None = None) -> dict[str, Any]:
    with get_connection() as conn:
        params: list[Any] = []
        where = ""
        if rating is not None:
            where = "WHERE r.rating = %s"
            params.append(rating)

        rows = conn.execute(
            f"""
            SELECT r.id, r.appointment_id, r.rating, r.comment, r.source, r.status,
                   r.created_at, r.updated_at,
                   u.id AS user_id, u.first_name, u.last_name, u.phone, u.username,
                   u.telegram_id, u.vk_id, u.max_user_id,
                   u.social_vk, u.social_instagram, u.social_facebook, u.email,
                   a.services, a.total_price, a.appointment_time, a.platform
            FROM reviews r
            JOIN users u ON u.id = r.user_id
            JOIN appointments a ON a.id = r.appointment_id
            {where}
            ORDER BY r.created_at DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params + [limit, offset]),
        ).fetchall()

        stats_row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(AVG(rating), 0) AS average,
                   SUM(CASE WHEN rating = 5 THEN 1 ELSE 0 END) AS five,
                   SUM(CASE WHEN rating = 4 THEN 1 ELSE 0 END) AS four,
                   SUM(CASE WHEN rating = 3 THEN 1 ELSE 0 END) AS three,
                   SUM(CASE WHEN rating = 2 THEN 1 ELSE 0 END) AS two,
                   SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS one
            FROM reviews
            WHERE status = 'published'
            """
        ).fetchone()
        stats = dict(stats_row) if stats_row else {}
        total = int(stats.get("total") or 0)

        reviews = []
        for row in rows:
            item = dict(row)
            first_name = str(item.get("first_name") or "").strip()
            reviews.append(
                {
                    "id": item["id"],
                    "appointment_id": item["appointment_id"],
                    "author_name": first_name or "Клиент",
                    "rating": int(item["rating"]),
                    "comment": item["comment"],
                    "source": item.get("source") or "site",
                    "status": item.get("status") or "published",
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "can_edit": _can_edit_now(item.get("created_at")),
                    "services": item.get("services") or "",
                    "appointment_time": item.get("appointment_time"),
                    "platform": item.get("platform") or "web",
                    "client": {
                        "id": item.get("user_id"),
                        "first_name": item.get("first_name") or "",
                        "last_name": item.get("last_name") or "",
                        "phone": item.get("phone") or "",
                        "username": item.get("username") or "",
                        "telegram_id": item.get("telegram_id"),
                        "vk_id": item.get("vk_id"),
                        "max_user_id": item.get("max_user_id"),
                        "social_vk": item.get("social_vk") or "",
                        "social_instagram": item.get("social_instagram") or "",
                        "social_facebook": item.get("social_facebook") or "",
                        "email": item.get("email") or "",
                    },
                }
            )

        return {
            "reviews": reviews,
            "stats": {
                "total": total,
                "average": round(float(stats.get("average") or 0), 2) if total else 0,
                "five": int(stats.get("five") or 0),
                "four": int(stats.get("four") or 0),
                "three": int(stats.get("three") or 0),
                "two": int(stats.get("two") or 0),
                "one": int(stats.get("one") or 0),
            },
        }


def _insert_review(
    conn,
    user_id: int,
    appointment_id: int,
    rating: int,
    comment: str,
    source: str,
) -> dict[str, Any]:
    if conn.is_pg:
        row = conn.execute(
            """
            INSERT INTO reviews (user_id, appointment_id, rating, comment, source, status)
            VALUES (%s, %s, %s, %s, %s, 'published')
            RETURNING *
            """,
            (user_id, appointment_id, rating, comment, source),
        ).fetchone()
        return dict(row)

    conn.execute(
        """
        INSERT INTO reviews (user_id, appointment_id, rating, comment, source, status)
        VALUES (%s, %s, %s, %s, %s, 'published')
        """,
        (user_id, appointment_id, rating, comment, source),
    )
    row_id = conn.raw_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = conn.execute("SELECT * FROM reviews WHERE id = %s", (row_id,)).fetchone()
    return dict(row)


def _queue_master_notification(conn, review_id: int, user_id: int) -> None:
    conn.execute(
        """
        INSERT INTO review_notifications (kind, review_id, user_id, platform, status)
        VALUES ('master_new_review', %s, %s, 'telegram', 'pending')
        """,
        (review_id, user_id),
    )


def _queue_review_invite(conn, appointment: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any] | None:
    recipient = _platform_user_id(appointment, user)
    if not recipient:
        return None
    platform, recipient_id = recipient
    exists = conn.execute(
        """
        SELECT id FROM review_notifications
        WHERE kind = 'review_invite' AND appointment_id = %s
        LIMIT 1
        """,
        (appointment["id"],),
    ).fetchone()
    if exists:
        return {"platform": platform, "recipient_id": recipient_id, "already_queued": True}

    conn.execute(
        """
        INSERT INTO review_notifications
            (kind, appointment_id, user_id, platform, recipient_id, status)
        VALUES ('review_invite', %s, %s, %s, %s, 'pending')
        """,
        (appointment["id"], appointment.get("user_id"), platform, recipient_id),
    )
    return {"platform": platform, "recipient_id": recipient_id, "already_queued": False}


def create_review(
    user_id: int,
    appointment_id: int,
    rating: Any,
    comment: Any,
    source: str = "site",
) -> tuple[bool, str, dict[str, Any] | None]:
    ok, parsed_rating, normalized_comment, error = validate_review(rating, comment)
    if not ok:
        return False, error or "Проверьте оценку и комментарий.", None

    source = source if source in ALLOWED_SOURCES else "site"

    with get_connection() as conn:
        user = _get_user(conn, user_id=user_id)
        if not user:
            return False, "Клиент не найден.", None

        appointment_row = conn.execute(
            "SELECT * FROM appointments WHERE id = %s", (appointment_id,)
        ).fetchone()
        appointment = _as_dict(appointment_row)
        if not appointment:
            return False, "Визит не найден.", None
        if not _owns_appointment(appointment, user):
            return False, "Этот визит принадлежит другому клиенту.", None
        if appointment.get("status") != "completed":
            return False, "Оставить отзыв можно только после завершённого визита.", None
        if not _is_paid(appointment):
            return False, "Отзыв станет доступен после подтверждения оплаты мастером.", None

        existing = conn.execute(
            "SELECT id FROM reviews WHERE appointment_id = %s", (appointment_id,)
        ).fetchone()
        if existing:
            return False, "Вы уже оставили отзыв за этот визит.", None

        try:
            review = _insert_review(
                conn,
                int(user["id"]),
                int(appointment_id),
                parsed_rating,
                normalized_comment,
                source,
            )
            _queue_master_notification(conn, int(review["id"]), int(user["id"]))
            conn.commit()
        except Exception as exc:
            logger.warning("Could not create review for appointment %s: %s", appointment_id, exc)
            conn.rollback()
            return False, "Не удалось сохранить отзыв. Попробуйте ещё раз.", None

    return True, "Спасибо за отзыв! Он опубликован.", review


def update_review(
    user_id: int,
    review_id: int,
    rating: Any,
    comment: Any,
) -> tuple[bool, str, dict[str, Any] | None]:
    ok, parsed_rating, normalized_comment, error = validate_review(rating, comment)
    if not ok:
        return False, error or "Проверьте оценку и комментарий.", None

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM reviews WHERE id = %s AND user_id = %s", (review_id, user_id)
        ).fetchone()
        review = _as_dict(row)
        if not review:
            return False, "Отзыв не найден.", None
        if not _can_edit_now(review.get("created_at")):
            return False, "Отзыв можно изменить только в течение 24 часов после публикации.", None

        conn.execute(
            """
            UPDATE reviews
            SET rating = %s, comment = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND user_id = %s
            """,
            (parsed_rating, normalized_comment, review_id, user_id),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM reviews WHERE id = %s", (review_id,)).fetchone()

    return True, "Отзыв обновлён.", _as_dict(updated)


def can_review_appointment(
    user_id: int,
    appointment_id: int,
) -> tuple[bool, str, dict[str, Any] | None]:
    with get_connection() as conn:
        user = _get_user(conn, user_id=user_id)
        if not user:
            return False, "Клиент не найден.", None
        appointment = _as_dict(
            conn.execute("SELECT * FROM appointments WHERE id = %s", (appointment_id,)).fetchone()
        )
        if not appointment:
            return False, "Визит не найден.", None
        if not _owns_appointment(appointment, user):
            return False, "Этот визит принадлежит другому клиенту.", None
        if appointment.get("status") != "completed":
            return False, "Оставить отзыв можно только после завершённого визита.", None
        if not _is_paid(appointment):
            return False, "Отзыв станет доступен после подтверждения оплаты мастером.", None
        existing = conn.execute(
            "SELECT id FROM reviews WHERE appointment_id = %s", (appointment_id,)
        ).fetchone()
        if existing:
            return False, "Вы уже оставили отзыв за этот визит.", appointment
        return True, "", appointment


def mark_appointment_paid(appointment_id: int) -> tuple[bool, str, dict[str, Any] | None, dict[str, Any] | None]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM appointments WHERE id = %s", (appointment_id,)).fetchone()
        appointment = _as_dict(row)
        if not appointment:
            return False, "Запись не найдена.", None, None
        if appointment.get("status") == "cancelled":
            return False, "Нельзя подтвердить оплату отменённой записи.", appointment, None
        if appointment.get("status") != "completed":
            return False, "Сначала отметьте визит как выполненный.", appointment, None

        user = _get_user(conn, user_id=int(appointment["user_id"])) if appointment.get("user_id") else None
        already_paid = bool(appointment.get("payment_confirmed"))

        if not already_paid:
            if conn.is_pg:
                updated = conn.execute(
                    """
                    UPDATE appointments
                    SET payment_confirmed = TRUE,
                        paid_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING *
                    """,
                    (appointment_id,),
                ).fetchone()
                appointment = dict(updated)
            else:
                conn.execute(
                    """
                    UPDATE appointments
                    SET payment_confirmed = 1,
                        paid_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (appointment_id,),
                )
                appointment = _as_dict(
                    conn.execute("SELECT * FROM appointments WHERE id = %s", (appointment_id,)).fetchone()
                )

        invite = _queue_review_invite(conn, appointment, user) if appointment else None
        conn.commit()

    if already_paid:
        return True, "Оплата уже подтверждена. Клиент может оставить отзыв.", appointment, invite
    return True, "Оплата подтверждена. Клиенту отправлено приглашение оставить отзыв.", appointment, invite


def get_pending_notifications(
    platform: str,
    kind: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT n.*,
                   a.services, a.total_price, a.appointment_time, a.status AS appointment_status,
                   a.payment_confirmed, a.platform AS appointment_platform,
                   u.first_name, u.last_name, u.phone, u.username,
                   r.rating, r.comment, r.created_at AS review_created_at,
                   (SELECT id FROM reviews rr WHERE rr.appointment_id = n.appointment_id LIMIT 1) AS existing_review_id
            FROM review_notifications n
            LEFT JOIN appointments a ON a.id = n.appointment_id
            LEFT JOIN users u ON u.id = n.user_id
            LEFT JOIN reviews r ON r.id = n.review_id
            WHERE n.status = 'pending'
              AND n.platform = %s
              AND n.kind = %s
              AND n.attempts < 5
            ORDER BY n.created_at ASC
            LIMIT %s
            """,
            (platform, kind, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_notification_sent(notification_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE review_notifications
            SET status = 'sent', sent_at = CURRENT_TIMESTAMP, last_error = NULL
            WHERE id = %s
            """,
            (notification_id,),
        )
        conn.commit()


def mark_notification_skipped(notification_id: int, reason: str = "not_applicable") -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE review_notifications
            SET status = 'skipped', last_error = %s, sent_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (reason, notification_id),
        )
        conn.commit()


def mark_notification_failed(notification_id: int, error: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE review_notifications
            SET attempts = attempts + 1, last_error = %s
            WHERE id = %s
            """,
            (str(error)[:500], notification_id),
        )
        conn.commit()


def notify_master_direct_text(review: dict[str, Any], client: dict[str, Any]) -> str:
    """Builds a plain-text master notification for review workers."""
    name = str(client.get("first_name") or "").strip() or "Клиент"
    phone = client.get("phone") or "—"
    rating = int(review.get("rating") or 0)
    comment = str(review.get("comment") or "").strip()
    services = str(review.get("services") or "").strip() or "Визит"
    return (
        f"⭐ Новый отзыв: {rating}/5\n\n"
        f"👤 Клиент: {name}\n"
        f"📱 Телефон: {phone}\n"
        f"✂️ Услуга: {services}\n\n"
        f"«{comment}»"
    )
