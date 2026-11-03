VERSION = "014_link_ramil_safarov_vk"
NAME = "Link VK ID 198855350 to client Ramil Safarov (+79063117901)"


def _get_field(row, col_name, idx=0):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(col_name)
    try:
        return row[col_name]
    except (TypeError, KeyError, IndexError):
        try:
            return row[idx]
        except Exception:
            return None


def apply(conn) -> None:
    vk_id = 198855350
    phone = "+79063117901"
    first_name = "Рамиль"
    last_name = "Сафаров"
    social_vk = "https://vk.com/id198855350"

    phones = (phone, "89063117901", "79063117901")

    # 1. Search existing user by phone
    cur = conn.execute(
        "SELECT id, phone, vk_id, first_name, last_name FROM users WHERE phone IN (%s, %s, %s)",
        phones,
    )
    user_by_phone = cur.fetchone()

    # 2. Search existing user by vk_id
    cur = conn.execute(
        "SELECT id, phone, vk_id, first_name, last_name FROM users WHERE vk_id = %s",
        (vk_id,),
    )
    user_by_vk = cur.fetchone()

    target_user_id = None

    uid_phone = _get_field(user_by_phone, "id", 0)
    uid_vk = _get_field(user_by_vk, "id", 0)

    if uid_phone and uid_vk and uid_phone != uid_vk:
        # Merge two records into uid_phone
        try:
            conn.execute("UPDATE appointments SET user_id = %s WHERE user_id = %s", (uid_phone, uid_vk))
        except Exception:
            pass
        try:
            conn.execute("DELETE FROM users WHERE id = %s", (uid_vk,))
        except Exception:
            pass
        conn.execute(
            """
            UPDATE users
            SET vk_id = %s,
                phone = %s,
                first_name = COALESCE(first_name, %s),
                last_name = COALESCE(last_name, %s),
                social_vk = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (vk_id, phone, first_name, last_name, social_vk, uid_phone),
        )
        target_user_id = uid_phone
    elif uid_phone:
        conn.execute(
            """
            UPDATE users
            SET vk_id = %s,
                phone = %s,
                first_name = COALESCE(first_name, %s),
                last_name = COALESCE(last_name, %s),
                social_vk = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (vk_id, phone, first_name, last_name, social_vk, uid_phone),
        )
        target_user_id = uid_phone
    elif uid_vk:
        conn.execute(
            """
            UPDATE users
            SET phone = %s,
                first_name = COALESCE(first_name, %s),
                last_name = COALESCE(last_name, %s),
                social_vk = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (phone, first_name, last_name, social_vk, uid_vk),
        )
        target_user_id = uid_vk
    else:
        conn.execute(
            """
            INSERT INTO users (phone, first_name, last_name, vk_id, social_vk, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (phone, first_name, last_name, vk_id, social_vk),
        )
        cur = conn.execute("SELECT id FROM users WHERE phone = %s", (phone,))
        row = cur.fetchone()
        target_user_id = _get_field(row, "id", 0)

    # 3. Associate appointments by phone if any
    if target_user_id:
        try:
            conn.execute(
                """
                UPDATE appointments
                SET user_id = %s
                WHERE phone IN (%s, %s, %s) AND (user_id IS NULL OR user_id != %s)
                """,
                (target_user_id, phone, "89063117901", "79063117901", target_user_id),
            )
        except Exception:
            pass

    conn.commit()


def rollback(conn) -> None:
    pass
