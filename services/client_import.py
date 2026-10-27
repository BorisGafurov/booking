import csv
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from database import get_connection

logger = logging.getLogger(__name__)


def normalize_client_phone(raw: str) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and (digits.startswith("8") or digits.startswith("7")):
        digits = "7" + digits[1:]
    elif len(digits) != 11:
        return None
    return f"+{digits}"


def parse_simple_xlsx(file_bytes: bytes) -> list[list[str]]:
    """Parse .xlsx without third-party dependencies using zipfile and ElementTree."""
    zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    shared_strings: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        tree = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in tree.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
            t = si.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
            shared_strings.append(t.text if t is not None and t.text else "")

    sheet_names = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
    if not sheet_names:
        return []

    sheet_xml = zf.read(sheet_names[0])
    tree = ET.fromstring(sheet_xml)
    sheet_data = tree.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheetData")
    if sheet_data is None:
        return []

    rows: list[list[str]] = []
    for row_node in sheet_data.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
        row_vals: list[str] = []
        for c in row_node.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
            v = c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
            val = v.text if v is not None and v.text else ""
            if c.get("t") == "s" and val.isdigit():
                idx = int(val)
                val = shared_strings[idx] if idx < len(shared_strings) else val
            row_vals.append(val.strip())
        if any(row_vals):
            rows.append(row_vals)
    return rows


def parse_csv_bytes(file_bytes: bytes) -> list[list[str]]:
    """Parse CSV trying UTF-8 and CP1251 with auto-detected delimiter."""
    text = ""
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if not text:
        return []

    sample = text[:2048]
    delimiter = ";" if ";" in sample else ("," if "," in sample else "\t")
    rows: list[list[str]] = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for row in reader:
        cleaned = [col.strip() for col in row]
        if any(cleaned):
            rows.append(cleaned)
    return rows


def import_clients_from_rows(rows: list[list[str]]) -> tuple[int, int, str]:
    if not rows:
        return 0, 0, "Файл не содержит данных."

    name_col = -1
    phone_col = -1
    start_row = 0

    # Scan top rows to find header
    for r_idx in range(min(5, len(rows))):
        r = rows[r_idx]
        for c_idx, cell in enumerate(r):
            c_low = cell.lower().strip()
            if any(w in c_low for w in ("имя", "фио", "клиент", "name", "client", "фамилия")) and name_col == -1:
                name_col = c_idx
            if any(w in c_low for w in ("тел", "phone", "номер", "моб")) and phone_col == -1:
                phone_col = c_idx
        if phone_col != -1:
            start_row = r_idx + 1
            break

    added = 0
    updated = 0

    with get_connection() as conn:
        for row in rows[start_row:]:
            phone_val = ""
            name_val = ""

            if phone_col != -1 and phone_col < len(row):
                phone_val = row[phone_col]
            if name_col != -1 and name_col < len(row):
                name_val = row[name_col]

            if not phone_val:
                # Search across all cells for a phone
                for cell in row:
                    norm = normalize_client_phone(cell)
                    if norm:
                        phone_val = cell
                        break

            norm_phone = normalize_client_phone(phone_val)
            if not norm_phone:
                continue

            if not name_val:
                # Use first non-empty text cell that is not the phone
                for cell in row:
                    if cell != phone_val and not normalize_client_phone(cell) and len(cell) > 1:
                        name_val = cell
                        break

            name_val = re.sub(r"[,;|\t]", " ", name_val).strip()
            name_val = re.sub(r"\s+", " ", name_val).strip()
            if not name_val:
                name_val = "Клиент"

            name_parts = name_val.split(maxsplit=1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            # Check if user exists by phone
            existing = conn.execute(
                "SELECT id, first_name, last_name FROM users WHERE phone = %s",
                (norm_phone,),
            ).fetchone()

            if existing:
                cur_first = existing["first_name"] or ""
                new_first = cur_first if (cur_first and cur_first != "Клиент") else first_name
                new_last = existing["last_name"] or last_name
                conn.execute(
                    """
                    UPDATE users
                    SET first_name = %s, last_name = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (new_first, new_last, existing["id"]),
                )
                updated += 1
                user_id = existing["id"]
            else:
                if conn.is_pg:
                    ins_res = conn.execute(
                        """
                        INSERT INTO users (phone, first_name, last_name)
                        VALUES (%s, %s, %s)
                        RETURNING id
                        """,
                        (norm_phone, first_name, last_name),
                    ).fetchone()
                    user_id = ins_res["id"] if ins_res else None
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO users (phone, first_name, last_name)
                        VALUES (%s, %s, %s)
                        """,
                        (norm_phone, first_name, last_name),
                    )
                    user_id = cur.lastrowid
                added += 1

            # If existing appointments with this phone have generic client_name, update them!
            full_name = f"{first_name} {last_name}".strip()
            conn.execute(
                """
                UPDATE appointments
                SET client_name = %s, user_id = COALESCE(user_id, %s)
                WHERE phone = %s AND (client_name IS NULL OR client_name IN ('Клиент (Masters)', 'Клиент', ''))
                """,
                (full_name, user_id, norm_phone),
            )

        conn.commit()

    total = added + updated
    return added, updated, f"Обработано клиентов: {total} (новых: {added}, обновлено: {updated})"


def import_clients_from_text(raw_text: str) -> tuple[int, int, str]:
    if not raw_text or not raw_text.strip():
        return 0, 0, "Текст пуст."

    phone_pattern = re.compile(
        r"(\+?[78][\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}|\b9\d{9}\b|\b\d{10,11}\b)"
    )

    rows: list[list[str]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = phone_pattern.search(line)
        if m:
            raw_phone = m.group(1)
            name = (line[:m.start()] + " " + line[m.end():]).strip(" :;,-")
            name = re.sub(r"\s+", " ", name).strip()
            rows.append([name or "Клиент", raw_phone])

    if not rows:
        return 0, 0, "Не удалось распознать номера телефонов в тексте."

    return import_clients_from_rows(rows)


def import_clients_from_file(filename: str, file_bytes: bytes) -> tuple[int, int, str]:
    name_low = filename.lower()
    if name_low.endswith(".xlsx"):
        rows = parse_simple_xlsx(file_bytes)
        return import_clients_from_rows(rows)
    elif name_low.endswith((".csv", ".tsv", ".txt")):
        rows = parse_csv_bytes(file_bytes)
        if not rows:
            text = file_bytes.decode("utf-8", errors="ignore")
            return import_clients_from_text(text)
        return import_clients_from_rows(rows)
    else:
        # Try xlsx first, then csv/text
        try:
            rows = parse_simple_xlsx(file_bytes)
            if rows:
                return import_clients_from_rows(rows)
        except Exception:
            pass
        rows = parse_csv_bytes(file_bytes)
        return import_clients_from_rows(rows)


def get_clients_stats() -> dict[str, int]:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        total = row["cnt"] if row else 0
        p_row = conn.execute("SELECT COUNT(*) as cnt FROM users WHERE phone IS NOT NULL AND phone != ''").fetchone()
        with_phone = p_row["cnt"] if p_row else 0
        return {"total": total, "with_phone": with_phone}


def get_clients_page(offset: int = 0, limit: int = 10, search: str | None = None) -> tuple[list[dict], int]:
    with get_connection() as conn:
        if search:
            st = search.strip()
            q_orig = f"%{st}%"
            q_low = f"%{st.lower()}%"
            q_title = f"%{st.title()}%"
            q_upper = f"%{st.upper()}%"

            where_sql = """
                WHERE (
                    COALESCE(first_name, '') || ' ' || COALESCE(last_name, '') LIKE %s
                    OR COALESCE(first_name, '') || ' ' || COALESCE(last_name, '') LIKE %s
                    OR COALESCE(first_name, '') || ' ' || COALESCE(last_name, '') LIKE %s
                    OR COALESCE(first_name, '') || ' ' || COALESCE(last_name, '') LIKE %s
                    OR phone LIKE %s
                )
            """
            params = (q_orig, q_low, q_title, q_upper, q_orig)

            count_row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM users {where_sql}",
                params,
            ).fetchone()
            total = count_row["cnt"] if count_row else 0

            rows = conn.execute(
                f"""
                SELECT id, phone, first_name, last_name, telegram_id, vk_id, max_user_id, created_at
                FROM users
                {where_sql}
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            ).fetchall()
        else:
            count_row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
            total = count_row["cnt"] if count_row else 0

            rows = conn.execute(
                """
                SELECT id, phone, first_name, last_name, telegram_id, vk_id, max_user_id, created_at
                FROM users
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            ).fetchall()

        return [dict(r) for r in rows], total


def update_appointment_client_details(
    appt_id: int, client_name: str, phone: str | None = None
) -> tuple[bool, str]:
    client_name = client_name.strip()
    norm_phone = normalize_client_phone(phone) if phone else None
    if norm_phone in ("+79173225155", "79173225155", "89173225155"):
        norm_phone = None

    with get_connection() as conn:
        appt = conn.execute("SELECT * FROM appointments WHERE id = %s", (appt_id,)).fetchone()
        if not appt:
            return False, "Запись не найдена."

        existing_name = appt["client_name"] or ""
        if (not client_name or client_name in ("Клиент", "Клиент (Masters)")) and existing_name and existing_name not in ("Клиент", "Клиент (Masters)"):
            client_name = existing_name

        user_id = appt["user_id"]
        if norm_phone:
            u_row = conn.execute("SELECT id FROM users WHERE phone = %s", (norm_phone,)).fetchone()
            if u_row:
                user_id = u_row["id"]
                conn.execute(
                    "UPDATE users SET first_name = %s WHERE id = %s AND (first_name IS NULL OR first_name = 'Клиент')",
                    (client_name, user_id),
                )
            else:
                name_parts = client_name.split(maxsplit=1)
                first = name_parts[0]
                last = name_parts[1] if len(name_parts) > 1 else ""
                if conn.is_pg:
                    ins = conn.execute(
                        "INSERT INTO users (phone, first_name, last_name) VALUES (%s, %s, %s) RETURNING id",
                        (norm_phone, first, last),
                    ).fetchone()
                    user_id = ins["id"] if ins else None
                else:
                    cur = conn.execute(
                        "INSERT INTO users (phone, first_name, last_name) VALUES (%s, %s, %s)",
                        (norm_phone, first, last),
                    )
                    user_id = cur.lastrowid

        existing_phone = appt["phone"] or ""
        if existing_phone in ("+79173225155", "79173225155", "89173225155"):
            existing_phone = ""

        effective_phone = norm_phone if norm_phone is not None else existing_phone
        conn.execute(
            """
            UPDATE appointments
            SET client_name = %s, phone = %s, user_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (client_name, effective_phone, user_id, appt_id),
        )
        conn.commit()

    return True, f"Клиент для записи #{appt_id} успешно обновлён: {client_name}"
