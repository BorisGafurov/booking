import asyncio
import datetime
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

test_dir = tempfile.mkdtemp()
os.environ["DATA_DIR"] = test_dir
os.environ["DATABASE_URL"] = ""

import database
from database import get_connection, init_db
from handlers.advice import router as advice_router
from handlers.booking import router as booking_router
from handlers.navigation import router as navigation_router
from handlers.referrals import (
    active_rewards_keyboard,
    admin_menu_keyboard,
    pending_referrals_keyboard,
    router as referrals_router,
)
from handlers.start import router as start_router, start
from keyboards.advice import (
    get_article_detail_keyboard,
    get_articles_keyboard,
    get_product_detail_keyboard,
    get_products_keyboard,
    get_recommendations_hub_keyboard,
)
from keyboards.booking import get_booking_keyboard
from keyboards.menu import get_main_menu
from keyboards.navigation import get_navigation_keyboard
from main import fallback_router
from aiogram import Dispatcher
from aiogram.filters import CommandObject
from services.recommendations import get_article, get_articles, get_product, get_products
from services.referrals import (
    get_active_rewards,
    get_pending_referrals,
    get_referral_stats,
    mark_referral_visited,
    mark_reward_used,
    register_referral,
    save_user,
)


class TestBookingBot(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(test_dir, ignore_errors=True)

    def setUp(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM rewards")
            conn.execute("DELETE FROM referrals")
            conn.execute("DELETE FROM users")

    def test_database_and_referral_cycle(self):
        # 1. Save referrer
        referrer_id, is_new1 = save_user(
            telegram_id=1001,
            username="referrer_user",
            first_name="Boris",
            last_name="Gafurov",
        )
        self.assertTrue(is_new1)

        # Update existing user
        ref_id2, is_new1_updated = save_user(
            telegram_id=1001,
            username="referrer_user_updated",
            first_name="Boris",
            last_name="Gafurov",
        )
        self.assertEqual(referrer_id, ref_id2)
        self.assertFalse(is_new1_updated)

        # 2. Save new friend
        friend_id, is_new2 = save_user(
            telegram_id=2002,
            username="friend_user",
            first_name="Alex",
            last_name="Smith",
        )
        self.assertTrue(is_new2)

        # 3. Test self-referral prevention
        success_self, msg_self, _ = register_referral(1001, 1001)
        self.assertFalse(success_self)
        self.assertIn("самого себя", msg_self)

        # 4. Register valid referral
        success_ref, msg_ref, notify_data = register_referral(
            new_user_telegram_id=2002, referrer_telegram_id=1001
        )
        self.assertTrue(success_ref)
        self.assertIsNotNone(notify_data)
        self.assertEqual(notify_data["referrer_telegram_id"], 1001)
        self.assertEqual(notify_data["invited_first_name"], "Alex")

        # 5. Prevent duplicate referral
        success_dup, msg_dup, _ = register_referral(
            new_user_telegram_id=2002, referrer_telegram_id=1001
        )
        self.assertFalse(success_dup)

        # 6. Check stats before visit
        stats = get_referral_stats(1001)
        self.assertEqual(stats["started"], 1)
        self.assertEqual(stats["confirmed"], 0)
        self.assertEqual(stats["active_rewards_count"], 0)
        self.assertEqual(stats["active_rewards_sum"], 0)

        # 7. Check pending referrals for admin
        pending_list, total_pending = get_pending_referrals(limit=10, offset=0)
        self.assertGreaterEqual(total_pending, 1)
        ref_entry = [p for p in pending_list if p["invited_telegram_id"] == 2002][0]

        # 8. Mark visited (confirm referral)
        v_success, v_msg, v_notify = mark_referral_visited(ref_entry["id"])
        self.assertTrue(v_success)
        self.assertEqual(v_notify["referrer_telegram_id"], 1001)
        self.assertEqual(v_notify["amount"], 300)
        self.assertEqual(v_notify["new_balance"], 300)

        # Try to confirm again (idempotent / should fail)
        v_again, _, _ = mark_referral_visited(ref_entry["id"])
        self.assertFalse(v_again)

        # 9. Check stats after confirmation
        stats_after = get_referral_stats(1001)
        self.assertEqual(stats_after["confirmed"], 1)
        self.assertEqual(stats_after["active_rewards_count"], 1)
        self.assertEqual(stats_after["active_rewards_sum"], 300)

        # 10. Check active rewards for admin
        rewards_list, total_rewards = get_active_rewards(limit=10, offset=0)
        self.assertGreaterEqual(total_rewards, 1)
        reward_entry = [r for r in rewards_list if r["telegram_id"] == 1001][0]

        # 11. Mark reward used
        u_success, u_msg, u_notify = mark_reward_used(reward_entry["id"])
        self.assertTrue(u_success)
        self.assertEqual(u_notify["user_telegram_id"], 1001)
        self.assertEqual(u_notify["amount"], 300)
        self.assertEqual(u_notify["remaining_balance"], 0)

        # Try to use again
        u_again, _, _ = mark_reward_used(reward_entry["id"])
        self.assertFalse(u_again)

        # 12. Stats after reward used
        stats_final = get_referral_stats(1001)
        self.assertEqual(stats_final["active_rewards_count"], 0)
        self.assertEqual(stats_final["active_rewards_sum"], 0)

    def test_recommendations(self):
        articles = get_articles()
        self.assertIn("styling", articles)
        self.assertIn("beard", articles)

        styling = get_article("styling")
        self.assertIsNotNone(styling)
        self.assertIn("укладывать", styling["title"].lower())

        products = get_products()
        self.assertIn("paste", products)
        self.assertIn("pomade", products)
        self.assertIn("shampoo", products)

        paste = get_product("paste")
        self.assertIsNotNone(paste)
        self.assertTrue(len(paste["links"]) > 0)

        pomade = get_product("pomade")
        self.assertIsNotNone(pomade)
        self.assertTrue(any("morgans" in label.lower() for label, _ in pomade["links"]))

    def test_keyboards(self):
        menu_admin = get_main_menu(is_admin=True)
        self.assertIsNotNone(menu_admin)
        self.assertEqual(len(menu_admin.keyboard), 3)

        menu_user = get_main_menu(is_admin=False)
        self.assertEqual(len(menu_user.keyboard), 2)

        booking_kb = get_booking_keyboard()
        self.assertIsNotNone(booking_kb)
        self.assertTrue(len(booking_kb.inline_keyboard) >= 1)

        nav_kb = get_navigation_keyboard()
        self.assertIsNotNone(nav_kb)
        self.assertTrue(len(nav_kb.inline_keyboard) >= 1)

        hub_kb = get_recommendations_hub_keyboard()
        self.assertIsNotNone(hub_kb)

        art_kb = get_articles_keyboard()
        self.assertIsNotNone(art_kb)

        prod_kb = get_products_keyboard()
        self.assertIsNotNone(prod_kb)

        art_det_kb = get_article_detail_keyboard("styling")
        self.assertIsNotNone(art_det_kb)

        prod_det_kb = get_product_detail_keyboard([("Помада Morgans", "https://ozon.ru/p/o3357125")])
        self.assertIsNotNone(prod_det_kb)

        adm_kb = admin_menu_keyboard()
        self.assertIsNotNone(adm_kb)

        pending_kb = pending_referrals_keyboard([], 0, 0)
        self.assertIsNotNone(pending_kb)

        rewards_kb = active_rewards_keyboard([], 0, 0)
        self.assertIsNotNone(rewards_kb)

    async def test_dispatcher_and_handlers_integration(self):
        dp = Dispatcher()
        dp.include_router(start_router)
        dp.include_router(booking_router)
        dp.include_router(navigation_router)
        dp.include_router(advice_router)
        dp.include_router(referrals_router)
        dp.include_router(fallback_router)

        self.assertEqual(len(dp.sub_routers), 6)

        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock()

        mock_message = MagicMock()
        mock_message.bot = mock_bot
        mock_message.answer = AsyncMock()

        mock_user = MagicMock()
        mock_user.id = 5555
        mock_user.username = "test_user"
        mock_user.first_name = "Test"
        mock_user.last_name = "User"
        mock_message.from_user = mock_user

        command = CommandObject(prefix="/", command="start", args="ref_1001")
        await start(mock_message, command)
        mock_message.answer.assert_called_once()
        mock_bot.send_message.assert_called_once()

    def test_phone_linking_and_account_merging(self):
        from services.referrals import (
            _save_user_unified,
            get_referral_stats,
            get_user_phone,
            is_phone_number,
            link_user_phone,
            normalize_phone,
            register_referral,
            save_user,
        )

        self.assertTrue(is_phone_number("+79991234567"))
        self.assertTrue(is_phone_number("89991234567"))
        self.assertEqual(normalize_phone("89991234567"), "+79991234567")

        save_user(telegram_id=101, first_name="BorisTG")
        _save_user_unified(vk_id=202, first_name="BorisVK")

        save_user(telegram_id=303, first_name="Friend")
        ok, _, _ = register_referral(303, 202)
        self.assertTrue(ok)

        stats_tg = get_referral_stats(101, "telegram")
        stats_vk = get_referral_stats(202, "vk")
        self.assertEqual(stats_tg["started"], 0)
        self.assertEqual(stats_vk["started"], 1)

        link_user_phone(202, "+79991112233", platform="vk")
        self.assertEqual(get_user_phone(202, "vk"), "+79991112233")

        ok_merge, _ = link_user_phone(101, "89991112233", platform="telegram")
        self.assertTrue(ok_merge)

        stats_tg_after = get_referral_stats(101, "telegram")
        stats_vk_after = get_referral_stats(202, "vk")
        self.assertEqual(stats_tg_after["started"], 1)
        self.assertEqual(stats_vk_after["started"], 1)
        self.assertEqual(stats_tg_after["started"], stats_vk_after["started"])

    def test_appointment_lifecycle_and_reminders(self):
        import datetime
        from services.appointments import (
            create_or_update_appointment,
            cancel_appointment,
            get_appointment,
            get_user_upcoming_appointments,
            get_due_2h_reminders,
            mark_reminder_sent,
            get_current_saratov_time,
        )

        now = get_current_saratov_time()
        in_1h = now + datetime.timedelta(hours=1)

        appt_id = create_or_update_appointment(
            phone="+79998887766",
            appointment_time=in_1h,
            services="Мужская стрижка",
            total_price=1200,
            client_name="Клиент Тест",
            platform="telegram",
            platform_user_id=123456,
        )
        self.assertIsNotNone(appt_id)

        appt = get_appointment(appt_id)
        self.assertIsNotNone(appt)
        self.assertEqual(appt["status"], "confirmed")
        self.assertEqual(appt["client_name"], "Клиент Тест")

        # Due 2h reminders should find this appointment
        due_2h = get_due_2h_reminders()
        self.assertTrue(any(a["id"] == appt_id for a in due_2h))

        # Mark sent
        mark_reminder_sent(appt_id, "2h")
        due_2h_after = get_due_2h_reminders()
        self.assertFalse(any(a["id"] == appt_id for a in due_2h_after))

        # Cancel appointment
        ok, msg, cancelled = cancel_appointment(appt_id)
        self.assertTrue(ok)
        self.assertEqual(cancelled["status"], "cancelled")

        # Upcoming appointments should now be empty
        upcoming = get_user_upcoming_appointments(platform="telegram", platform_user_id=123456)
        self.assertEqual(len(upcoming), 0)

    def test_masters_session_and_phone_formatting(self):
        from services.masters_sync import (
            format_master_phone,
            get_active_masters_session,
            disconnect_masters_session,
        )
        from database import get_connection

        self.assertEqual(format_master_phone("+79991234567"), "(999) 123-45-67")
        self.assertEqual(format_master_phone("89991234567"), "(999) 123-45-67")

        with get_connection() as conn:
            conn.execute("DELETE FROM masters_session")
            conn.commit()

        self.assertIsNone(get_active_masters_session())

        with get_connection() as conn:
            conn.execute(
                "INSERT INTO masters_session (cookie_phpsessid, master_phone, is_active) VALUES (%s, %s, %s)",
                ("test_session_token", "+79991234567", True if conn.is_pg else 1),
            )
            conn.commit()

        session = get_active_masters_session()
        self.assertIsNotNone(session)
        self.assertEqual(session["master_phone"], "+79991234567")

        disconnect_masters_session()
        self.assertIsNone(get_active_masters_session())

    def test_broadcast_recipients(self):
        from services.referrals import get_broadcast_recipients, save_user
        save_user(telegram_id=999888, username="test_bc")
        recipients = get_broadcast_recipients("telegram")
        self.assertIn(999888, recipients)

    def test_client_confirmation_and_followup_reminders(self):
        from services.appointments import (
            create_or_update_appointment,
            confirm_appointment_by_client,
            cancel_appointment,
            get_appointment,
            get_due_confirmation_followup_reminders,
            mark_reminder_sent,
            get_current_saratov_time,
        )

        now = get_current_saratov_time()
        in_3h = now + datetime.timedelta(hours=3)

        appt_id = create_or_update_appointment(
            phone="+79991112233",
            appointment_time=in_3h,
            services="Мужская стрижка",
            total_price=1200,
            client_name="Подтверждающий Клиент",
            platform="telegram",
            platform_user_id=888777,
        )
        self.assertIsNotNone(appt_id)

        appt = get_appointment(appt_id)
        self.assertFalse(bool(appt["client_confirmed"]))

        # Mark evening sent so it can be eligible for morning follow-up
        mark_reminder_sent(appt_id, "evening")

        # If it's daytime (>= 09:00), check followup list
        if now.hour >= 9:
            due = get_due_confirmation_followup_reminders()
            self.assertTrue(any(a["id"] == appt_id for a in due))

        # Client confirms the appointment
        ok, msg, confirmed_appt = confirm_appointment_by_client(appt_id)
        self.assertTrue(ok)
        self.assertEqual(msg, "Запись успешно подтверждена")
        self.assertTrue(bool(confirmed_appt["client_confirmed"]))

        appt_after = get_appointment(appt_id)
        self.assertTrue(bool(appt_after["client_confirmed"]))

        # Once confirmed, it should no longer be in morning followup list
        if now.hour >= 9:
            due = get_due_confirmation_followup_reminders()
            self.assertFalse(any(a["id"] == appt_id for a in due))

        # Cancel appointment
        ok_cancel, _, cancelled = cancel_appointment(appt_id)
        self.assertTrue(ok_cancel)
        self.assertEqual(cancelled["status"], "cancelled")

        # Trying to confirm a cancelled appointment should fail
        ok_conf_again, msg_conf, _ = confirm_appointment_by_client(appt_id)
        self.assertFalse(ok_conf_again)
        self.assertIn("отменена", msg_conf)


if __name__ == "__main__":
    unittest.main()
