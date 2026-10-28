import csv
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from core_bot.database import get_connection
from core_bot.services.referrals import is_master_phone

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

    # Masters CRM statistics export: keep all its fields (birthday, groups,
    # socials, email, comment, statistics), not just name + phone.
    try:
        masters_clients = parse_masters_clients_xlsx(file_bytes)
        if masters_clients:
            return import_masters_clients(masters_clients)
    except Exception as e:
        logger.debug("Masters export parse attempt failed for %s: %s", filename, e)

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


# ---------------------------------------------------------------------------
# Masters CRM client export ("Клиенты" sheet) import
# ---------------------------------------------------------------------------

_XLSNS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELSNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKGRELNS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _cell_col_index(cell_ref: str) -> int:
    """Converts a cell reference like 'D5' into a 0-based column index."""
    idx = 0
    for ch in cell_ref:
        if not ch.isalpha():
            break
        idx = idx * 26 + (ord(ch.upper()) - 64)
    return max(idx - 1, 0)


def _find_masters_sheet_path(zf: zipfile.ZipFile) -> str | None:
    """Locates the worksheet named 'Клиенты' (falls back to the first sheet)."""
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {
            rel.get("Id"): rel.get("Target", "")
            for rel in rels.findall(f"{_PKGRELNS}Relationship")
        }
        sheets = wb.findall(f".//{_XLSNS}sheet")
        first_target = None
        for sheet in sheets:
            name = (sheet.get("name") or "").strip().lower()
            target = rel_map.get(sheet.get(f"{_RELSNS}id") or "", "")
            if target and not target.startswith("/"):
                target = "xl/" + target.lstrip("/")
            if first_target is None:
                first_target = target
            if "клиент" in name and "запис" not in name:
                return target
        return first_target
    except Exception:
        return None


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    tree = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    shared: list[str] = []
    for si in tree.findall(f"{_XLSNS}si"):
        shared.append("".join(t.text or "" for t in si.iter(f"{_XLSNS}t")))
    return shared


def _xlsx_cell_text(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        is_el = cell.find(f"{_XLSNS}is")
        if is_el is None:
            return ""
        return "".join(t.text or "" for t in is_el.iter(f"{_XLSNS}t")).strip()
    v = cell.find(f"{_XLSNS}v")
    if v is None or v.text is None:
        return ""
    val = v.text
    if cell_type == "s":
        try:
            idx = int(val)
        except ValueError:
            return val.strip()
        return shared[idx].strip() if idx < len(shared) else ""
    return val.strip()


def _masters_field_for_header(header: str) -> str | None:
    """Maps a Masters export header ('Фамилия', 'Записей всего', ...) to a field."""
    h = header.strip().lower().rstrip(":").strip()
    if not h or h.startswith("№"):
        return None
    if "фамили" in h:
        return "last_name"
    if h == "имя" or h.startswith("имя "):
        return "first_name"
    if "тел" in h:
        return "phone"
    if "рожд" in h:
        return "birthday"
    if "регистрац" in h:
        return "masters_registered"
    if "отмен" in h:
        return "masters_cancelled_total"
    if "записей" in h:
        return "masters_appointments_total"
    if "оказано" in h:
        return "masters_revenue"
    if "групп" in h:
        return "masters_group"
    if h == "vk" or h.startswith("vk "):
        return "social_vk"
    if "instagram" in h:
        return "social_instagram"
    if "facebook" in h:
        return "social_facebook"
    if "email" in h or "почт" in h:
        return "email"
    if "коммент" in h:
        return "comment"
    return None


def parse_masters_clients_xlsx(file_bytes: bytes) -> list[dict[str, str]]:
    """
    Parses the 'Клиенты' sheet of a Masters CRM statistics export (.xlsx)
    into a list of dicts with keys: last_name, first_name, phone, birthday,
    masters_registered, masters_appointments_total, masters_cancelled_total,
    masters_revenue, masters_group, social_instagram, social_facebook,
    social_vk, email, comment.
    Uses only the standard library (zipfile/xml), no openpyxl dependency.
    """
    zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    sheet_path = _find_masters_sheet_path(zf)
    if not sheet_path or sheet_path not in zf.namelist():
        sheets = sorted(
            n for n in zf.namelist()
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )
        if not sheets:
            return []
        sheet_path = sheets[0]

    shared = _xlsx_shared_strings(zf)
    tree = ET.fromstring(zf.read(sheet_path))
    sheet_data = tree.find(f"{_XLSNS}sheetData")
    if sheet_data is None:
        return []

    # rows: {row_number: {col_index: text}}
    rows: dict[int, dict[int, str]] = {}
    for row_node in sheet_data.findall(f"{_XLSNS}row"):
        try:
            row_no = int(row_node.get("r") or "0")
        except ValueError:
            row_no = len(rows) + 1
        cells: dict[int, str] = {}
        for c in row_node.findall(f"{_XLSNS}c"):
            ref = c.get("r")
            col = _cell_col_index(ref) if ref else len(cells)
            text = _xlsx_cell_text(c, shared)
            if text:
                cells[col] = text
        if cells:
            rows[row_no] = cells

    # Find the header row: must contain phone and name columns.
    header_row_no = None
    col_map: dict[int, str] = {}
    for row_no in sorted(rows):
        candidate: dict[int, str] = {}
        for col, text in rows[row_no].items():
            field = _masters_field_for_header(text)
            if field:
                candidate[col] = field
        if "phone" in candidate.values() and (
            "first_name" in candidate.values() or "last_name" in candidate.values()
        ):
            header_row_no = row_no
            col_map = candidate
            break
    if header_row_no is None:
        return []

    clients: list[dict[str, str]] = []
    for row_no in sorted(rows):
        if row_no <= header_row_no:
            continue
        cells = rows[row_no]
        item: dict[str, str] = {}
        for col, field in col_map.items():
            val = cells.get(col, "").strip()
            if val:
                item[field] = val
        if item.get("phone"):
            clients.append(item)
    return clients


def _masters_int(val: Any) -> int:
    try:
        return int(round(float(str(val).replace(",", "."))))
    except (TypeError, ValueError):
        return 0


def _name_is_meaningful(name: str | None) -> bool:
    """True if the name is a real name (contains a letter, not a placeholder like 'Клиент')."""
    if not name:
        return False
    if name in ("Клиент", "Клиент (Masters)"):
        return False
    return bool(re.search(r"[^\W\d_]", name))


def import_masters_clients(clients: list[dict[str, Any]]) -> tuple[int, int, str]:
    """
    Upserts clients parsed from a Masters CRM export into the users table,
    matching by normalized phone. Masters-specific fields (birthday, groups,
    socials, email, comment, statistics) are stored as new columns of users.
    Returns (added, updated, message).
    """
    if not clients:
        return 0, 0, "Нет клиентов для импорта."

    text_fields = {
        "birthday": "birthday",
        "masters_registered": "masters_registered",
        "masters_group": "masters_group",
        "social_instagram": "social_instagram",
        "social_facebook": "social_facebook",
        "social_vk": "social_vk",
        "email": "email",
        "comment": "comment",
    }
    stat_fields = {
        "masters_appointments_total": "masters_appointments_total",
        "masters_cancelled_total": "masters_cancelled_total",
        "masters_revenue": "masters_revenue",
    }

    added = 0
    updated = 0
    skipped = 0

    with get_connection() as conn:
        for client in clients:
            norm_phone = normalize_client_phone(str(client.get("phone", "")))
            if not norm_phone:
                skipped += 1
                continue

            first_name = str(client.get("first_name", "")).strip()
            last_name = str(client.get("last_name", "")).strip()
            if not first_name and not last_name:
                first_name = "Клиент"
            if not first_name:
                first_name = last_name.split()[0] if last_name else "Клиент"
                if last_name.startswith(first_name):
                    last_name = last_name[len(first_name):].strip()

            # Text fields: only non-empty export values are written.
            text_vals = {
                col: str(client.get(field, "")).strip()
                for field, col in text_fields.items()
            }
            stat_vals = {col: _masters_int(client.get(field)) for field, col in stat_fields.items()}

            existing = conn.execute(
                "SELECT id, first_name, last_name FROM users WHERE phone = %s",
                (norm_phone,),
            ).fetchone()

            if existing:
                user_id = existing["id"]
                cur_first = (existing["first_name"] or "").strip()
                cur_last = (existing["last_name"] or "").strip()
                new_first = cur_first if _name_is_meaningful(cur_first) else (first_name or cur_first)
                new_last = cur_last if _name_is_meaningful(cur_last) else (last_name or cur_last)

                sets = ["first_name = %s", "last_name = %s"]
                params: list[Any] = [new_first, new_last]
                for col, val in text_vals.items():
                    if val:
                        sets.append(f"{col} = %s")
                        params.append(val)
                for col, val in stat_vals.items():
                    sets.append(f"{col} = %s")
                    params.append(val)
                sets.append("masters_imported_at = CURRENT_TIMESTAMP")
                sets.append("updated_at = CURRENT_TIMESTAMP")
                params.append(user_id)
                conn.execute(
                    f"UPDATE users SET {', '.join(sets)} WHERE id = %s",
                    tuple(params),
                )
                updated += 1
            else:
                cols = ["phone", "first_name", "last_name"]
                vals: list[Any] = [norm_phone, first_name, last_name]
                for col, val in text_vals.items():
                    if val:
                        cols.append(col)
                        vals.append(val)
                for col, val in stat_vals.items():
                    cols.append(col)
                    vals.append(val)
                cols.append("masters_imported_at")
                placeholders = ", ".join(["%s"] * (len(cols) - 1) + ["CURRENT_TIMESTAMP"])
                if conn.is_pg:
                    row = conn.execute(
                        f"INSERT INTO users ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
                        tuple(vals),
                    ).fetchone()
                    user_id = row["id"] if row else None
                else:
                    cur = conn.execute(
                        f"INSERT INTO users ({', '.join(cols)}) VALUES ({placeholders})",
                        tuple(vals),
                    )
                    user_id = cur.lastrowid
                added += 1

            # Link appointments that still have a generic client name.
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
    msg = f"Masters: обработано {total} клиентов (новых: {added}, обновлено: {updated})"
    if skipped:
        msg += f", пропущено без телефона: {skipped}"
    return added, updated, msg


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
    if is_master_phone(norm_phone):
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
        if is_master_phone(existing_phone):
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
