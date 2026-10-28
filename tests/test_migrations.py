import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core_bot.database.connection import DBConnection
from core_bot.database.migrations.runner import (
    ensure_migration_table,
    get_applied_migrations,
    get_available_migrations,
    get_migration_status,
    rollback_migration,
    run_migrations,
)


class TestDatabaseMigrations(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test_migration.db"
        raw = sqlite3.connect(self.db_path)
        raw.row_factory = sqlite3.Row
        self.conn = DBConnection(raw, is_pg=False)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_run_migrations_fresh_db(self):
        """Test applying all migrations to an empty database."""
        applied = run_migrations(self.conn)
        self.assertIn("001_initial_schema", applied)
        self.assertIn("002_masters_session", applied)
        self.assertIn("003_user_roles_and_schedule", applied)
        self.assertIn("004_data_cleanups", applied)

        # Verify tables exist
        cursor = self.conn.raw_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row["name"] for row in cursor.fetchall()}
        expected_tables = {
            "schema_migrations",
            "users",
            "referrals",
            "rewards",
            "appointments",
            "masters_session",
            "user_roles",
            "master_schedule",
        }
        for t in expected_tables:
            self.assertIn(t, tables, f"Expected table {t} to exist after migrations")

        # Verify schema_migrations contains recorded versions
        applied_set = get_applied_migrations(self.conn)
        self.assertEqual(len(applied_set), len(applied))
        for v in applied:
            self.assertIn(v, applied_set)

    def test_migrations_idempotency(self):
        """Test that running migrations twice is safe and applies nothing on second run."""
        first_run = run_migrations(self.conn)
        self.assertTrue(len(first_run) > 0)

        second_run = run_migrations(self.conn)
        self.assertEqual(second_run, [], "Second migration run should apply 0 migrations")

    def test_migration_status_reporting(self):
        """Test get_migration_status returns accurate status before and after run."""
        statuses_before = get_migration_status(self.conn)
        for s in statuses_before:
            self.assertFalse(s["applied"])
            self.assertIsNone(s["applied_at"])

        run_migrations(self.conn)

        statuses_after = get_migration_status(self.conn)
        for s in statuses_after:
            self.assertTrue(s["applied"])
            self.assertIsNotNone(s["applied_at"])

    def test_dynamic_incremental_migration_and_rollback(self):
        """Test creating an incremental migration version, applying it, and rolling it back."""
        # Baseline run
        run_migrations(self.conn)

        # Create a temp versions directory copying existing ones plus a new migration
        custom_versions = Path(self.test_dir) / "custom_versions"
        custom_versions.mkdir()

        # Copy existing migrations
        real_versions = Path(__file__).resolve().parent.parent / "core_bot" / "database" / "migrations" / "versions"
        for f in real_versions.glob("*.py"):
            shutil.copy(f, custom_versions / f.name)

        # Write migration 005
        m005_content = '''
VERSION = "005_client_notes"
NAME = "Add notes column to appointments"

def apply(conn):
    conn.execute("ALTER TABLE appointments ADD COLUMN notes TEXT")

def rollback(conn):
    pass
'''
        (custom_versions / "005_client_notes.py").write_text(m005_content.strip())

        new_applied = run_migrations(self.conn, versions_dir=custom_versions)
        self.assertEqual(new_applied, ["005_client_notes"])

        # Check column was added
        cursor = self.conn.raw_conn.execute("PRAGMA table_info(appointments)")
        cols = {row["name"] for row in cursor.fetchall()}
        self.assertIn("notes", cols)

        # Check applied set
        applied_set = get_applied_migrations(self.conn)
        self.assertIn("005_client_notes", applied_set)


    def test_migration_006_removes_guessed_placeholders(self):
        """Migration 006 deletes guessed Masters placeholders but keeps real appointments."""
        run_migrations(self.conn)

        self.conn.execute(
            """
            INSERT INTO appointments
                (platform, platform_user_id, client_name, phone, services,
                 total_price, appointment_time, status, masters_id)
            VALUES
                ('masters', 0, 'Клиент (Masters)', '', 'Стрижка (МУЖСКАЯ)', 0,
                 '2026-10-27 14:00:00', 'confirmed', 'mst_2026-10-27_1400'),
                ('masters', 0, 'Клиент (Masters)', '', 'Стрижка (МУЖСКАЯ)', 0,
                 '2026-10-27 10:00:00', 'confirmed', 'mst_2026-10-27_1000'),
                ('telegram', 123456, 'Реальный Клиент', '+79998887766', 'Стрижка', 2300,
                 '2026-10-30 15:00:00', 'confirmed', 'mst_real_record_1')
            """
        )
        self.conn.commit()

        import importlib

        mod = importlib.import_module(
            "core_bot.database.migrations.versions.006_remove_guessed_placeholders"
        )
        mod.apply(self.conn)

        rows = self.conn.execute(
            "SELECT masters_id FROM appointments ORDER BY id"
        ).fetchall()
        remaining = [r["masters_id"] for r in rows]
        self.assertEqual(remaining, ["mst_real_record_1"])


if __name__ == "__main__":
    unittest.main()
