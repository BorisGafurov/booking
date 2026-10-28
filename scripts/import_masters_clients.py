#!/usr/bin/env python3
"""Import clients from a Masters CRM statistics export (.xlsx, sheet 'Клиенты').

Usage:
    .venv/bin/python scripts/import_masters_clients.py <export.xlsx> [--dry-run]

- Applies pending database migrations first (adds Masters client columns).
- Upserts clients into `users` by normalized phone and stores the same fields
  Masters keeps: birthday, registration status, groups, socials, email,
  comment and visit statistics.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core_bot.database import get_connection, run_migrations  # noqa: E402
from core_bot.services.client_import import (  # noqa: E402
    import_masters_clients,
    parse_masters_clients_xlsx,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import clients from a Masters CRM xlsx export")
    parser.add_argument("file", help="Path to the .xlsx export from Masters")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only parse and show what would be imported, without writing to the DB",
    )
    args = parser.parse_args()

    path = Path(args.file).expanduser()
    if not path.is_file():
        print(f"Файл не найден: {path}")
        return 1

    try:
        clients = parse_masters_clients_xlsx(path.read_bytes())
    except Exception as e:
        print(f"Не удалось разобрать файл: {e}")
        return 1

    if not clients:
        print("В файле не найдено клиентов (лист «Клиенты» с колонками Имя/Фамилия/Телефон).")
        return 1

    print(f"Разобрано клиентов: {len(clients)}")
    if args.dry_run:
        for c in clients[:5]:
            print("  пример:", c)
        print("(dry-run: изменения в базу не записываются)")
        return 0

    # Ensure the Masters client columns exist before importing.
    with get_connection() as conn:
        applied = run_migrations(conn)
    if applied:
        print(f"Применены миграции: {', '.join(applied)}")

    added, updated, msg = import_masters_clients(clients)
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
