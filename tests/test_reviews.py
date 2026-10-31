import shutil
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from core_bot.database.connection import DBConnection
from core_bot.database.migrations.runner import run_migrations
from core_bot.services import reviews


class TestReviewValidation(unittest.TestCase):
    def test_comment_normalisation(self):
        self.assertEqual(
            reviews.normalize_review_comment("  отличная   стрижка!!  "),
            "Отличная стрижка!!",
        )
        self.assertEqual(
            reviews.normalize_review_comment("всё супер. мастер внимательный! рекомендую?"),
            "Всё супер. Мастер внимательный! Рекомендую?",
        )
        self.assertEqual(
            reviews.normalize_review_comment("первая строка\nвторая строка"),
            "Первая строка вторая строка",
        )

    def test_rating_must_be_integer_1_to_5(self):
        self.assertEqual(reviews.parse_rating(5), 5)
        self.assertEqual(reviews.parse_rating("4"), 4)
        self.assertIsNone(reviews.parse_rating(0))
        self.assertIsNone(reviews.parse_rating(6))
        self.assertIsNone(reviews.parse_rating(4.5))
        self.assertIsNone(reviews.parse_rating(True))
        self.assertIsNone(reviews.parse_rating("5.0"))

    def test_comment_validation_rules(self):
        ok, text, error = reviews.validate_review_comment("  отличная стрижка  ")
        self.assertTrue(ok)
        self.assertEqual(text, "Отличная стрижка")
        self.assertIsNone(error)

        for bad in (
            "коротко",
            "а" * 501,
            "Отличная стрижка 😀",
            "Заходите на https://example.com",
            "Заходите на сайт example.ru",
            "<b>Отличная стрижка</b>",
            "1234567890",
        ):
            ok, _, error = reviews.validate_review_comment(bad)
            self.assertFalse(ok, bad)
            self.assertTrue(error)


class TestReviewStorage(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "reviews.db"
        raw = sqlite3.connect(self.db_path)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        self.conn = DBConnection(raw, is_pg=False)
        run_migrations(self.conn)
        self.conn.commit()

        @contextmanager
        def test_connection():
            raw_conn = sqlite3.connect(self.db_path, timeout=5)
            raw_conn.row_factory = sqlite3.Row
            raw_conn.execute("PRAGMA foreign_keys = ON")
            conn = DBConnection(raw_conn, is_pg=False)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        self.connection_patcher = patch.object(reviews, "get_connection", test_connection)
        self.connection_patcher.start()

        self.conn.execute(
            """
            INSERT INTO users (phone, first_name, last_name, telegram_id)
            VALUES (%s, %s, %s, %s)
            """,
            ("+79990000001", "Иван", "Петров", 1001),
        )
        self.user_id = self.conn.execute(
            "SELECT id FROM users WHERE phone = %s", ("+79990000001",)
        ).fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO appointments (
                user_id, platform, platform_user_id, client_name, phone, services,
                total_price, appointment_time, duration_min, status,
                payment_confirmed, paid_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.user_id,
                "telegram",
                1001,
                "Иван Петров",
                "+79990000001",
                "Мужская стрижка",
                2300,
                "2026-10-20 12:00:00",
                75,
                "completed",
                1,
                "2026-10-20 13:00:00",
            ),
        )
        self.appointment_id = self.conn.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]
        self.conn.commit()

    def tearDown(self):
        self.connection_patcher.stop()
        self.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _add_appointment(self, status: str = "completed", paid: int = 1) -> int:
        self.conn.execute(
            """
            INSERT INTO appointments (
                user_id, platform, platform_user_id, client_name, phone, services,
                total_price, appointment_time, duration_min, status,
                payment_confirmed, paid_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.user_id,
                "telegram",
                1001,
                "Иван Петров",
                "+79990000001",
                "Мужская стрижка",
                2300,
                "2026-10-21 12:00:00",
                75,
                status,
                paid,
                "2026-10-21 13:00:00" if paid else None,
            ),
        )
        appointment_id = self.conn.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]
        self.conn.commit()
        return appointment_id

    def test_create_review_requires_paid_completed_visit(self):
        pending_id = self._add_appointment(status="confirmed", paid=0)
        ok, message, _ = reviews.create_review(
            self.user_id, pending_id, 5, "Отличная стрижка, всё понравилось"
        )
        self.assertFalse(ok)
        self.assertIn("завершённого", message)

        unpaid_id = self._add_appointment(status="completed", paid=0)
        ok, message, _ = reviews.create_review(
            self.user_id, unpaid_id, 5, "Отличная стрижка, всё понравилось"
        )
        self.assertFalse(ok)
        self.assertIn("оплаты", message)

    def test_one_review_per_visit_and_edit_window(self):
        ok, message, review = reviews.create_review(
            self.user_id,
            self.appointment_id,
            5,
            "  отличная стрижка.   мастер внимательный!!  ",
        )
        self.assertTrue(ok, message)
        self.assertEqual(review["rating"], 5)
        self.assertEqual(review["comment"], "Отличная стрижка. Мастер внимательный!!")

        ok, message, _ = reviews.create_review(
            self.user_id, self.appointment_id, 4, "Хочу оставить второй отзыв"
        )
        self.assertFalse(ok)
        self.assertIn("уже оставили", message)

        ok, message, updated = reviews.update_review(
            self.user_id, review["id"], 4, "стало ещё лучше. рекомендую!"
        )
        self.assertTrue(ok, message)
        self.assertEqual(updated["rating"], 4)
        self.assertEqual(updated["comment"], "Стало ещё лучше. Рекомендую!")

        self.conn.execute(
            "UPDATE reviews SET created_at = %s WHERE id = %s",
            ("2026-10-01 10:00:00", review["id"]),
        )
        self.conn.commit()
        ok, message, _ = reviews.update_review(
            self.user_id, review["id"], 5, "Поздняя попытка изменить отзыв"
        )
        self.assertFalse(ok)
        self.assertIn("24 часов", message)

    def test_mark_paid_queues_invite_once(self):
        appointment_id = self._add_appointment(status="completed", paid=0)

        ok, message, appointment, invite = reviews.mark_appointment_paid(appointment_id)
        self.assertTrue(ok, message)
        self.assertTrue(appointment["payment_confirmed"])
        self.assertEqual(invite["platform"], "telegram")
        self.assertEqual(invite["recipient_id"], 1001)

        ok, _, _, invite = reviews.mark_appointment_paid(appointment_id)
        self.assertTrue(ok)
        self.assertTrue(invite["already_queued"])

        rows = self.conn.execute(
            "SELECT * FROM review_notifications WHERE kind = 'review_invite'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_public_and_master_lists(self):
        ok, _, review = reviews.create_review(
            self.user_id,
            self.appointment_id,
            5,
            "Отличная стрижка, обязательно приду ещё",
        )
        self.assertTrue(ok)

        public = reviews.get_public_reviews()
        self.assertEqual(public["stats"]["total"], 1)
        self.assertEqual(public["reviews"][0]["author_name"], "Иван")
        self.assertNotIn("Петров", public["reviews"][0]["author_name"])

        master = reviews.get_master_reviews()
        self.assertEqual(master["reviews"][0]["client"]["last_name"], "Петров")
        self.assertEqual(master["reviews"][0]["client"]["phone"], "+79990000001")
        self.assertEqual(master["reviews"][0]["id"], review["id"])

    def test_platform_review_flow(self):
        can, _, appt = reviews.can_review_platform_appointment("telegram", 1001, self.appointment_id)
        self.assertTrue(can)
        self.assertIsNotNone(appt)

        ok, msg, created = reviews.create_review_from_platform(
            "telegram",
            1001,
            self.appointment_id,
            5,
            "Супер стрижка и отличная атмосфера в салоне",
        )
        self.assertTrue(ok, msg)
        self.assertEqual(created["rating"], 5)
        self.assertEqual(created["source"], "telegram")

        # After review, can_review returns False
        can_again, _, _ = reviews.can_review_platform_appointment("telegram", 1001, self.appointment_id)
        self.assertFalse(can_again)


if __name__ == "__main__":
    unittest.main()
