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

    def _columns(self, table: str) -> set[str]:
        rows = self.conn.raw_conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}

    def _tables(self) -> set[str]:
        cursor = self.conn.raw_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {row["name"] for row in cursor.fetchall()}

    def test_run_migrations_fresh_db(self):
        """Test applying all migrations to an empty database."""
        applied = run_migrations(self.conn)
        self.assertIn("001_initial_schema", applied)
        self.assertIn("003_user_roles_and_schedule", applied)
        self.assertIn("004_data_cleanups", applied)
        self.assertIn("007_client_extra_fields", applied)
        self.assertIn("008_site_sync_cleanup", applied)
        self.assertIn("009_reviews", applied)

        expected_tables = {
            "schema_migrations",
            "users",
            "referrals",
            "rewards",
            "appointments",
            "user_roles",
            "master_schedule",
            "reviews",
            "review_notifications",
        }
        self.assertTrue(expected_tables.issubset(self._tables()))

        user_columns = self._columns("users")
        for column in (
            "source_registered",
            "client_group",
            "visits_total",
            "cancelled_total",
            "revenue_total",
            "imported_at",
        ):
            self.assertIn(column, user_columns)
        self.assertFalse(any(column.startswith("masters_") for column in user_columns))
        self.assertNotIn("masters_session", self._tables())

        appointment_columns = self._columns("appointments")
        self.assertIn("duration_min", appointment_columns)
        self.assertIn("notes", appointment_columns)
        self.assertIn("payment_confirmed", appointment_columns)
        self.assertIn("paid_at", appointment_columns)
        self.assertNotIn("masters_id", appointment_columns)

        review_columns = self._columns("reviews")
        for column in (
            "id",
            "user_id",
            "appointment_id",
            "rating",
            "comment",
            "source",
            "status",
            "created_at",
            "updated_at",
        ):
            self.assertIn(column, review_columns)

        notification_columns = self._columns("review_notifications")
        for column in (
            "kind",
            "appointment_id",
            "review_id",
            "user_id",
            "platform",
            "recipient_id",
            "status",
            "attempts",
            "sent_at",
        ):
            self.assertIn(column, notification_columns)

        applied_set = get_applied_migrations(self.conn)
        self.assertEqual(len(applied_set), len(applied))
        for version in applied:
            self.assertIn(version, applied_set)

    def test_migrations_idempotency(self):
        """Test that running migrations twice is safe and applies nothing on second run."""
        first_run = run_migrations(self.conn)
        self.assertTrue(len(first_run) > 0)

        second_run = run_migrations(self.conn)
        self.assertEqual(second_run, [], "Second migration run should apply 0 migrations")

    def test_migration_status_reporting(self):
        """Test get_migration_status returns accurate status before and after run."""
        statuses_before = get_migration_status(self.conn)
        for status in statuses_before:
            self.assertFalse(status["applied"])
            self.assertIsNone(status["applied_at"])

        run_migrations(self.conn)

        statuses_after = get_migration_status(self.conn)
        for status in statuses_after:
            self.assertTrue(status["applied"])
            self.assertIsNotNone(status["applied_at"])

    def test_dynamic_incremental_migration_and_rollback(self):
        """Test creating an incremental migration version and applying it."""
        run_migrations(self.conn)

        custom_versions = Path(self.test_dir) / "custom_versions"
        custom_versions.mkdir()

        real_versions = (
            Path(__file__).resolve().parent.parent
            / "core_bot"
            / "database"
            / "migrations"
            / "versions"
        )
        for migration_file in real_versions.glob("*.py"):
            shutil.copy(migration_file, custom_versions / migration_file.name)

        m005_content = '''
VERSION = "005_client_notes"
NAME = "Add a custom note column to appointments"

def apply(conn):
    conn.execute("ALTER TABLE appointments ADD COLUMN legacy_note TEXT")

def rollback(conn):
    pass
'''
        (custom_versions / "005_client_notes.py").write_text(m005_content.strip())

        new_applied = run_migrations(self.conn, versions_dir=custom_versions)
        self.assertEqual(new_applied, ["005_client_notes"])
        self.assertIn("legacy_note", self._columns("appointments"))
        self.assertIn("005_client_notes", get_applied_migrations(self.conn))

    def test_migration_008_migrates_legacy_columns(self):
        """Migration 008 keeps legacy client data under neutral names."""
        run_migrations(self.conn)

        self.conn.execute(
            "ALTER TABLE users ADD COLUMN masters_registered TEXT"
        )
        self.conn.execute(
            "ALTER TABLE users ADD COLUMN masters_group TEXT"
        )
        self.conn.execute(
            "ALTER TABLE users ADD COLUMN masters_appointments_total INTEGER DEFAULT 0"
        )
        self.conn.execute(
            "ALTER TABLE users ADD COLUMN masters_imported_at TIMESTAMP"
        )
        self.conn.execute(
            "ALTER TABLE appointments ADD COLUMN masters_id TEXT"
        )
        self.conn.execute(
            "CREATE TABLE masters_session (id INTEGER PRIMARY KEY, phone TEXT)"
        )
        self.conn.execute(
            """
            INSERT INTO users
                (phone, first_name, masters_registered, masters_group,
                 masters_appointments_total, masters_imported_at)
            VALUES ('+79990000001', 'Legacy', '2024-01-01', 'VIP', 12,
                    '2026-01-01 10:00:00')
            """
        )
        self.conn.execute(
            """
            INSERT INTO appointments
                (platform, platform_user_id, client_name, phone, services,
                 total_price, appointment_time, status, masters_id)
            VALUES ('masters', 0, 'Legacy Client', '+79990000001',
                    'Стрижка + Борода', 3000, '2026-12-01 14:00:00',
                    'confirmed', 'legacy-1')
            """
        )
        self.conn.commit()

        import importlib

        migration = importlib.import_module(
            "core_bot.database.migrations.versions.008_site_sync_cleanup"
        )
        migration.apply(self.conn)

        user_row = self.conn.execute(
            "SELECT * FROM users WHERE phone = %s", ("+79990000001",)
        ).fetchone()
        self.assertEqual(user_row["source_registered"], "2024-01-01")
        self.assertEqual(user_row["client_group"], "VIP")
        self.assertEqual(user_row["visits_total"], 12)
        self.assertFalse(any(column.startswith("masters_") for column in self._columns("users")))

        appointment_row = self.conn.execute(
            "SELECT * FROM appointments WHERE phone = %s", ("+79990000001",)
        ).fetchone()
        self.assertEqual(appointment_row["duration_min"], 135)
        self.assertEqual(appointment_row["platform"], "manual")
        self.assertNotIn("masters_id", self._columns("appointments"))
        self.assertNotIn("masters_session", self._tables())


if __name__ == "__main__":
    unittest.main()
