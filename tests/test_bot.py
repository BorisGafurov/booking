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
            conn.execute("DELETE FROM masters_session")

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

        save_user(vk_id=303, first_name="Friend")
        ok, _, _ = register_referral(new_user_vk_id=303, referrer_vk_id=202)
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
        in_day = now + datetime.timedelta(hours=2, minutes=30) if 9 <= now.hour < 21 else now + datetime.timedelta(hours=3)

        appt_id = create_or_update_appointment(
            phone="+79991112233",
            appointment_time=in_day,
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

        # If it's daytime (9 <= now.hour < 21), check followup list
        if 9 <= now.hour < 21:
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
        if 9 <= now.hour < 21:
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

    def test_masters_confirm_and_cancel_sync(self):
        from unittest.mock import MagicMock, patch
        from services.masters_sync import (
            confirm_masters_appointment,
            cancel_masters_appointment,
            disconnect_masters_session,
        )
        from database import get_connection

        # Ensure no active session
        disconnect_masters_session()

        test_appt = {
            "id": 999111,
            "masters_id": "test_m_123",
            "phone": "+79991234567",
            "appointment_time": datetime.datetime.now(),
        }

        # Without active session -> returns False with warning
        ok_c, msg_c = confirm_masters_appointment(test_appt)
        self.assertFalse(ok_c)
        self.assertIn("не подключена", msg_c)

        ok_d, msg_d = cancel_masters_appointment(test_appt)
        self.assertFalse(ok_d)
        self.assertIn("не подключена", msg_d)

        # Now insert active session
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO masters_session (cookie_phpsessid, cookie_csrf, master_phone, is_active) VALUES (%s, %s, %s, %s)",
                ("dummy_sess", "dummy_csrf", "+79991234567", True if conn.is_pg else 1),
            )
            conn.commit()

        # Mock urllib opener to return successful response
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp

        with patch("services.masters_sync._get_masters_opener", return_value=(mock_opener, "dummy_csrf")):
            # Test confirm with active session
            ok_conf, msg_conf = confirm_masters_appointment(test_appt)
            self.assertTrue(ok_conf)
            self.assertIn("успешно подтверждена", msg_conf)

            # Test cancel with active session
            ok_canc, msg_canc = cancel_masters_appointment(test_appt)
            self.assertTrue(ok_canc)
            self.assertIn("успешно отменена", msg_canc)

        # Cleanup session
        disconnect_masters_session()

    def test_master_complete_appointment_auto_rewards_referral(self):
        from services.appointments import complete_appointment, create_manual_appointment, get_appointment
        from services.referrals import get_active_rewards, register_referral

        # 1. Setup referrer and friend
        ref_id, _ = save_user(telegram_id=8881, username="ref_guy", first_name="Referrer")
        friend_id, _ = save_user(telegram_id=8882, username="friend_guy", first_name="Friend")

        reg_ok, reg_msg, _ = register_referral(8882, 8881)
        self.assertTrue(reg_ok)

        # 2. Friend creates an appointment
        appt_time = datetime.datetime.now() + datetime.timedelta(days=1)
        ok_cr, _, appt_id = create_manual_appointment(
            phone="+79170001122",
            appointment_time=appt_time,
            services="Мужская стрижка",
            total_price=1000,
            client_name="Friend Guy",
            platform="telegram",
        )
        self.assertTrue(ok_cr)
        self.assertIsNotNone(appt_id)

        # Update appointment user_id to match friend_id to test referral linking
        with get_connection() as conn:
            conn.execute("UPDATE appointments SET user_id = %s WHERE id = %s", (friend_id, appt_id))
            conn.commit()

        # 3. Master completes appointment
        comp_ok, comp_msg, appt_data, ref_notify = complete_appointment(appt_id)
        self.assertTrue(comp_ok)
        self.assertIsNotNone(ref_notify)
        self.assertEqual(ref_notify["amount"], 300)
        self.assertEqual(ref_notify["referrer_telegram_id"], 8881)

        # Check appointment status in DB
        updated_appt = get_appointment(appt_id)
        self.assertEqual(updated_appt["status"], "completed")

        # Check rewards balance for referrer
        rewards, total = get_active_rewards()
        self.assertEqual(total, 1)
        self.assertEqual(rewards[0]["user_db_id"], ref_id)
        self.assertEqual(rewards[0]["amount"], 300)

    def test_master_reschedule_appointment(self):
        from services.appointments import create_manual_appointment, get_appointment, reschedule_appointment

        appt_time = datetime.datetime.now() + datetime.timedelta(days=1)
        ok_cr, _, appt_id = create_manual_appointment(
            phone="+79179998877",
            appointment_time=appt_time,
            services="Стрижка бороды",
            total_price=600,
            client_name="Test Resched",
        )

        new_time = datetime.datetime.now() + datetime.timedelta(days=3, hours=2)
        resched_ok, resched_msg, updated = reschedule_appointment(appt_id, new_time)
        self.assertTrue(resched_ok)

        appt_in_db = get_appointment(appt_id)
        self.assertEqual(appt_in_db["status"], "confirmed")
        self.assertFalse(bool(appt_in_db["reminder_evening_sent"]))
        self.assertFalse(bool(appt_in_db["reminder_2h_sent"]))

    def test_master_schedule_management(self):
        from services.appointments import (
            adjust_master_day_hours,
            get_master_day_schedule,
            get_upcoming_days_schedule,
            set_master_day_schedule,
        )

        tuesday_day = "2026-11-17"  # Tuesday (working in Masters)
        sunday_day = "2026-11-15"   # Sunday (day off)

        # 1. Default schedule from Masters: Tuesday is working, Sunday is day off
        tue_sched = get_master_day_schedule(tuesday_day)
        self.assertTrue(tue_sched["is_working"])
        self.assertEqual(tue_sched["start_time"], "10:00")
        self.assertEqual(tue_sched["end_time"], "19:00")

        sun_sched = get_master_day_schedule(sunday_day)
        self.assertFalse(sun_sched["is_working"])

        # 2. Make Tuesday a day off (override in DB)
        set_master_day_schedule(tuesday_day, is_working=False)
        sched2 = get_master_day_schedule(tuesday_day)
        self.assertFalse(sched2["is_working"])

        # 3. Make working and adjust hours
        set_master_day_schedule(tuesday_day, is_working=True, start_time="09:00", end_time="21:00")
        adj = adjust_master_day_hours(tuesday_day, start_delta_hours=-1, end_delta_hours=1)
        self.assertEqual(adj["start_time"], "08:00")
        self.assertEqual(adj["end_time"], "22:00")

        # 4. Check upcoming schedule list
        upcoming = get_upcoming_days_schedule(days_ahead=7)
        self.assertEqual(len(upcoming), 7)

    def test_role_foundation_system(self):
        from services.referrals import assign_user_role, get_user_role, is_admin_user, is_master_user, revoke_user_role

        user_id = 999888
        # Unassigned non-admin user
        self.assertFalse(is_admin_user(user_id, platform="telegram"))
        self.assertFalse(is_master_user(user_id, platform="telegram"))

        # Assign master role
        assign_user_role("telegram", user_id, "master")
        self.assertTrue(is_master_user(user_id, platform="telegram"))
        self.assertFalse(is_admin_user(user_id, platform="telegram"))
        self.assertEqual(get_user_role(user_id, platform="telegram"), "master")

        # Revoke role
        revoke_user_role("telegram", user_id)
        self.assertFalse(is_master_user(user_id, platform="telegram"))

    def test_client_import_and_masters_services(self):
        from services.client_import import (
            get_clients_page,
            get_clients_stats,
            import_clients_from_text,
            update_appointment_client_details,
        )
        from services.masters import get_service_options_for_creation
        from services.appointments import create_or_update_appointment, get_appointment

        # 1. Services
        opts = get_service_options_for_creation()
        names = [o["name"] for o in opts]
        prices = [o["price"] for o in opts]
        self.assertIn("Стрижка (МУЖСКАЯ)", names)
        self.assertIn(2300, prices)
        self.assertNotIn(1000, prices)

        # 2. Text import
        text_data = (
            "Дмитрий Смирнов +79270001122\n"
            "Сергей Попов 89051112233"
        )
        added, updated, msg = import_clients_from_text(text_data)
        self.assertGreaterEqual(added, 2)

        stats = get_clients_stats()
        self.assertGreaterEqual(stats["total"], 2)

        clients, total = get_clients_page(search="Дмитрий")
        self.assertTrue(any("Дмитрий" in (c["first_name"] or "") for c in clients))

        # 3. Create appointment with generic name then update client details
        now = datetime.datetime.now(datetime.timezone.utc)
        appt_id = create_or_update_appointment(
            masters_id="mst_test_import_99",
            appointment_time=now + datetime.timedelta(days=1),
            services="Стрижка (МУЖСКАЯ)",
            total_price=2300,
            client_name="Клиент (Masters)",
            phone="+79270001122",
            platform="telegram",
        )
        ok, umsg = update_appointment_client_details(appt_id, "Дмитрий Смирнов", "+79270001122")
        self.assertTrue(ok)
        appt = get_appointment(appt_id)
        self.assertEqual(appt["client_name"], "Дмитрий Смирнов")

        # 4. Re-sync from masters with placeholder must NOT overwrite custom client name
        create_or_update_appointment(
            masters_id="mst_test_import_99",
            appointment_time=now + datetime.timedelta(days=1),
            services="Стрижка (МУЖСКАЯ)",
            total_price=2300,
            client_name="Клиент (Masters)",
            phone="+79173225155",
            platform="telegram",
        )
        appt_after = get_appointment(appt_id)
        self.assertEqual(appt_after["client_name"], "Дмитрий Смирнов")
        self.assertEqual(appt_after["phone"], "+79270001122")

        # 5. Price update and preservation
        from services.appointments import update_appointment_price_and_service
        ok_p, p_msg, p_appt = update_appointment_price_and_service(appt_id, 700, "Оформление бороды")
        self.assertTrue(ok_p)
        self.assertEqual(p_appt["total_price"], 700)
        self.assertEqual(p_appt["services"], "Оформление бороды")

        # Background masters sync must NOT overwrite this custom price back to 2300
        create_or_update_appointment(
            masters_id="mst_test_import_99",
            appointment_time=now + datetime.timedelta(days=1),
            services="Стрижка (МУЖСКАЯ)",
            total_price=2300,
            client_name="Клиент (Masters)",
            phone="+79173225155",
            platform="masters",
        )
        appt_final = get_appointment(appt_id)
        self.assertEqual(appt_final["total_price"], 700)
        self.assertEqual(appt_final["services"], "Оформление бороды")

    def test_link_user_phone_auto_creation_and_master_phone_prevention(self):
        from services.referrals import link_user_phone, get_user_phone
        from services.masters import initiate_booking

        # 1. Non-existent user sends phone -> should auto-create unified user and succeed
        success, msg = link_user_phone(999888, "+7 (999) 111-22-33", platform="telegram")
        self.assertTrue(success)
        self.assertEqual(get_user_phone(999888, platform="telegram"), "+79991112233")

        # 2. Master's own phone prevention
        ok, error_msg, session_data = initiate_booking("+79173225155", {"hair"}, int(datetime.datetime.now().timestamp()))
        self.assertFalse(ok)
        self.assertIn("номер самого мастера", error_msg)
        self.assertIsNone(session_data)

    def test_timezone_conversion_precision(self):
        from services.appointments import SARATOV_TZ, parse_to_saratov, create_or_update_appointment, get_appointment
        from zoneinfo import ZoneInfo

        # 1. UTC timestamp: 11:00 UTC = 15:00 Saratov (UTC+4)
        dt_utc = parse_to_saratov("2026-10-31T11:00:00Z")
        self.assertEqual(dt_utc.tzinfo, SARATOV_TZ)
        self.assertEqual(dt_utc.hour, 15)
        self.assertEqual(dt_utc.minute, 0)

        # 2. Moscow timestamp: 14:00 MSK (UTC+3) = 15:00 Saratov (UTC+4)
        dt_msk = parse_to_saratov("2026-10-31T14:00:00+03:00")
        self.assertEqual(dt_msk.tzinfo, SARATOV_TZ)
        self.assertEqual(dt_msk.hour, 15)
        self.assertEqual(dt_msk.minute, 0)

        # 3. Naive timestamp: 15:00 naive = 15:00 Saratov
        dt_naive = parse_to_saratov("2026-10-31 15:00:00")
        self.assertEqual(dt_naive.tzinfo, SARATOV_TZ)
        self.assertEqual(dt_naive.hour, 15)
        self.assertEqual(dt_naive.minute, 0)

        # 4. Save appointment with aware Moscow datetime -> ensure retrieved time is 15:00 Saratov
        moscow_tz = ZoneInfo("Europe/Moscow")
        dt_in_msk = datetime.datetime(2026, 10, 31, 14, 0, 0, tzinfo=moscow_tz)
        appt_id = create_or_update_appointment(
            phone="+79998887766",
            appointment_time=dt_in_msk,
            services="Стрижка",
            total_price=2300,
            client_name="Тест Часового Пояса",
            platform="telegram",
            masters_id="mst_tz_test_1",
        )
        saved = get_appointment(appt_id)
        saved_dt = parse_to_saratov(saved["appointment_time"])
        self.assertEqual(saved_dt.hour, 15)

    def test_masters_sync_platform_and_user_preservation(self):
        from services.appointments import create_or_update_appointment, get_appointment

        # 1. Book appointment via Telegram with user_id=123456
        appt_time = datetime.datetime(2026, 11, 5, 14, 0, 0)
        appt_id = create_or_update_appointment(
            phone="+79271112233",
            appointment_time=appt_time,
            services="Стрижка",
            total_price=2300,
            client_name="Иван Клиент",
            platform="telegram",
            platform_user_id=123456,
            masters_id="mst_sync_preserve_99",
        )
        first_state = get_appointment(appt_id)
        self.assertEqual(first_state["platform"], "telegram")
        self.assertEqual(first_state["platform_user_id"], 123456)

        # 2. Background Masters sync runs with platform='masters' and platform_user_id=0
        create_or_update_appointment(
            phone="",
            appointment_time=appt_time,
            services="Стрижка",
            total_price=2300,
            client_name="Клиент (Masters)",
            platform="masters",
            platform_user_id=0,
            masters_id="mst_sync_preserve_99",
        )
        second_state = get_appointment(appt_id)
        # Platform and user_id must remain preserved for Telegram reminders!
        self.assertEqual(second_state["platform"], "telegram")
        self.assertEqual(second_state["platform_user_id"], 123456)
        self.assertEqual(second_state["client_name"], "Иван Клиент")

    def test_user_merge_transfers_appointments_and_roles(self):
        from services.referrals import save_user, link_user_phone
        from services.appointments import create_or_update_appointment, get_appointment

        # Create user A (e.g. registered via TG without phone)
        user_a_id, _ = save_user(telegram_id=777001, first_name="UserA")
        # User A has an appointment
        appt_id = create_or_update_appointment(
            phone="",
            appointment_time=datetime.datetime(2026, 11, 10, 12, 0, 0),
            services="Стрижка",
            total_price=2300,
            client_name="UserA",
            platform="telegram",
            platform_user_id=777001,
        )
        # Create user B (has phone from Masters or VK)
        user_b_id, _ = save_user(telegram_id=888002, phone="+79997778899", first_name="UserB")

        # Now User A enters phone +79997778899 -> User A must merge into User B
        ok, msg = link_user_phone(777001, "+79997778899", platform="telegram")
        self.assertTrue(ok)

        # The appointment must now belong to User B (target_id), not be orphaned!
        appt = get_appointment(appt_id)
        self.assertEqual(appt["user_id"], user_b_id)

    def test_reminder_worker_excludes_master(self):
        from services.reminder_worker import _resolve_platform_user_id

        # 1. Appointment with master phone with spaces/brackets
        appt_master = {
            "id": 1,
            "phone": "+7 (917) 322-51-55",
            "platform": "telegram",
            "platform_user_id": 1010373026,
            "user_id": None,
        }
        self.assertIsNone(_resolve_platform_user_id(appt_master, "telegram"))

        # 2. Appointment with VK master ID
        appt_vk_master = {
            "id": 2,
            "phone": "",
            "platform": "vk",
            "platform_user_id": 90599236,
            "user_id": None,
        }
        self.assertIsNone(_resolve_platform_user_id(appt_vk_master, "vk"))


    def test_initiate_booking_existing_client_move_to_url(self):
        from unittest.mock import patch, MagicMock
        from services.masters import initiate_booking

        class DummyResponse:
            def __init__(self, data: bytes, url: str):
                self._data = data
                self._url = url

            def read(self):
                return self._data

            def geturl(self):
                return self._url

        mock_opener = MagicMock()

        def fake_open(req, timeout=10):
            url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
            if "/price" in url and "services=" not in url:
                return DummyResponse(b'var ONLINE_CID = "test_cid";', url)
            elif "/price" in url:
                return DummyResponse(b"OK", url)
            elif "/times" in url:
                return DummyResponse(b'<form action="/boris_gafurov/phone?test_cid"></form>', url)
            elif "/phone" in url:
                return DummyResponse(b'{"move_to_url": "/boris_gafurov/comment?cid=test_cid"}', url)
            elif "/comment" in url:
                if hasattr(req, "data") and req.data:
                    return DummyResponse(b"<html>Done</html>", "https://mst.link/boris_gafurov/finish?id=APPT-TEST-123")
                else:
                    return DummyResponse(b'<form id="online-info-form"><input name="_csrf" value="csrf123"/><input name="OnlineInfoForm[firstname]" value="Alexander"/></form>', url)
            return DummyResponse(b"", url)

        mock_opener.open.side_effect = fake_open

        with patch("urllib.request.build_opener", return_value=mock_opener):
            ok, msg, session_data = initiate_booking("+79991234567", {"hair"}, 1700000000, client_name="Alexander")
            self.assertTrue(ok)
            self.assertIsNotNone(session_data)
            self.assertFalse(session_data.get("needs_sms"))
            self.assertEqual(session_data.get("appointment_id"), "APPT-TEST-123")

    def test_initiate_booking_new_client_sms_flow_and_confirmation(self):
        from unittest.mock import patch, MagicMock
        from services.masters import initiate_booking, confirm_sms_code

        class DummyResponse:
            def __init__(self, data: bytes, url: str):
                self._data = data
                self._url = url

            def read(self):
                return self._data

            def geturl(self):
                return self._url

        mock_opener = MagicMock()

        def fake_open(req, timeout=10):
            url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
            if "/price" in url and "services=" not in url:
                return DummyResponse(b'var ONLINE_CID = "test_cid";', url)
            elif "/price" in url:
                return DummyResponse(b"OK", url)
            elif "/times" in url:
                return DummyResponse(b'<form action="/boris_gafurov/phone?test_cid"></form>', url)
            elif "/phone" in url:
                return DummyResponse(b'{"success": true, "verify_url": "/boris_gafurov/verify?v=123&cid=test_cid"}', url)
            elif "/verify" in url:
                if hasattr(req, "data") and req.data:
                    return DummyResponse(b'<form id="online-info-form"><input name="_csrf" value="csrf456"/><input name="OnlineInfoForm[firstname]" value=""/></form>', "https://mst.link/boris_gafurov/comment?cid=test_cid")
                else:
                    return DummyResponse(b'<form id="js-form-online-code"><input name="_csrf" value="csrf456"/></form>', url)
            elif "/comment" in url:
                return DummyResponse(b"<html>Done</html>", "https://mst.link/boris_gafurov/finish?id=APPT-SMS-456")
            return DummyResponse(b"", url)

        mock_opener.open.side_effect = fake_open

        with patch("urllib.request.build_opener", return_value=mock_opener):
            ok, msg, session_data = initiate_booking("+79998887766", {"hair"}, 1700000000, client_name="NewClient")
            self.assertTrue(ok)
            self.assertIsNotNone(session_data)
            self.assertTrue(session_data.get("needs_sms"))

            # Now confirm code
            confirm_ok, confirm_msg = confirm_sms_code(session_data, "1234")
            self.assertTrue(confirm_ok)
            self.assertEqual(session_data.get("appointment_id"), "APPT-SMS-456")

    def test_initiate_booking_masters_error_handling(self):
        from unittest.mock import patch, MagicMock
        from services.masters import initiate_booking

        class DummyResponse:
            def __init__(self, data: bytes, url: str):
                self._data = data
                self._url = url

            def read(self):
                return self._data

            def geturl(self):
                return self._url

        mock_opener = MagicMock()

        def fake_open(req, timeout=10):
            url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
            if "/price" in url and "services=" not in url:
                return DummyResponse(b'var ONLINE_CID = "test_cid";', url)
            elif "/price" in url:
                return DummyResponse(b"OK", url)
            elif "/times" in url:
                return DummyResponse(b'<form action="/boris_gafurov/phone?test_cid"></form>', url)
            elif "/phone" in url:
                return DummyResponse('{"error": "Выбранное время уже занято другим клиентом."}'.encode("utf-8"), url)
            return DummyResponse(b"", url)

        mock_opener.open.side_effect = fake_open

        with patch("urllib.request.build_opener", return_value=mock_opener):
            ok, msg, session_data = initiate_booking("+79990001122", {"hair"}, 1700000000)
            self.assertFalse(ok)
            self.assertEqual(msg, "Выбранное время уже занято другим клиентом.")
            self.assertIsNone(session_data)

    def test_idor_protection(self):
        from services.appointments import (
            create_or_update_appointment,
            cancel_appointment,
            confirm_appointment_by_client,
            reschedule_appointment,
        )
        t_now = datetime.datetime.now(datetime.timezone.utc)
        appt_id = create_or_update_appointment(
            phone="+79991112233",
            appointment_time=t_now,
            services="Стрижка",
            total_price=2300,
            platform="telegram",
            platform_user_id=111,
        )

        # Attacker with telegram_id=222 attempts to cancel
        ok_cancel, msg_cancel, _ = cancel_appointment(appt_id, platform="telegram", platform_user_id=222)
        self.assertFalse(ok_cancel)
        self.assertIn("У вас нет прав", msg_cancel)

        # Attacker with telegram_id=222 attempts to confirm
        ok_conf, msg_conf, _ = confirm_appointment_by_client(appt_id, platform="telegram", platform_user_id=222)
        self.assertFalse(ok_conf)
        self.assertIn("У вас нет прав", msg_conf)

        # Attacker with telegram_id=222 attempts to reschedule
        ok_resched, msg_resched, _ = reschedule_appointment(
            appt_id, t_now + datetime.timedelta(days=1), platform="telegram", platform_user_id=222
        )
        self.assertFalse(ok_resched)
        self.assertIn("У вас нет прав", msg_resched)

        # Legitimate owner cancels
        ok_owner, _, appt = cancel_appointment(appt_id, platform="telegram", platform_user_id=111)
        self.assertTrue(ok_owner)
        self.assertEqual(appt["status"], "cancelled")

    def test_cross_platform_user_isolation(self):
        from services.referrals import _find_user, get_user_by_platform_id, link_user_phone
        from database import get_connection

        # Save user on telegram with ID 77777
        save_user(telegram_id=77777, first_name="TGUser", phone="+79997777777")

        with get_connection() as conn:
            # Looking for 77777 on VK must return None
            vk_user = _find_user(conn, 77777, platform="vk")
            self.assertIsNone(vk_user)

            # Looking for 77777 on MAX must return None
            max_user = _find_user(conn, 77777, platform="max")
            self.assertIsNone(max_user)

            # Looking for 77777 on telegram must find the telegram user
            tg_user = _find_user(conn, 77777, platform="telegram")
            self.assertIsNotNone(tg_user)
            self.assertEqual(tg_user["first_name"], "TGUser")

        # Linking phone for a new VK user who happens to have VK ID 77777
        ok, msg = link_user_phone(77777, "+79998889900", platform="vk")
        self.assertTrue(ok)

        # Verify telegram user profile was NOT overwritten
        with get_connection() as conn:
            tg_user_after = _find_user(conn, 77777, platform="telegram")
            self.assertEqual(tg_user_after["phone"], "+79997777777")

            vk_user_after = _find_user(conn, 77777, platform="vk")
            self.assertEqual(vk_user_after["phone"], "+79998889900")
            self.assertNotEqual(tg_user_after["id"], vk_user_after["id"])

    def test_retention_reminders_include_completed(self):
        from services.appointments import (
            create_or_update_appointment,
            complete_appointment,
            cancel_appointment,
            get_due_retention_reminders,
            SARATOV_TZ,
        )
        from unittest.mock import patch

        mock_now = datetime.datetime(2026, 10, 27, 14, 0, tzinfo=SARATOV_TZ)
        time_18d_ago = mock_now - datetime.timedelta(days=18)
        time_future = mock_now + datetime.timedelta(days=2)

        # 1. Completed 18 days ago -> should be included
        appt_completed_id = create_or_update_appointment(
            phone="+79991110001",
            appointment_time=time_18d_ago,
            services="Стрижка",
            total_price=2300,
            platform="telegram",
            platform_user_id=1001,
        )
        complete_appointment(appt_completed_id)

        # 2. Cancelled 18 days ago -> should NOT be included
        appt_cancelled_id = create_or_update_appointment(
            phone="+79991110002",
            appointment_time=time_18d_ago,
            services="Стрижка",
            total_price=2300,
            platform="telegram",
            platform_user_id=1002,
        )
        cancel_appointment(appt_cancelled_id)

        # 3. Future appointment -> should NOT be included
        appt_future_id = create_or_update_appointment(
            phone="+79991110003",
            appointment_time=time_future,
            services="Стрижка",
            total_price=2300,
            platform="telegram",
            platform_user_id=1003,
        )

        with patch("core_bot.services.appointments.get_current_saratov_time", return_value=mock_now):
            due = get_due_retention_reminders(platform="telegram")
            due_ids = [d["id"] for d in due]
            self.assertIn(appt_completed_id, due_ids)
            self.assertNotIn(appt_cancelled_id, due_ids)
            self.assertNotIn(appt_future_id, due_ids)

    def test_sqlite_search_clients(self):
        from services.referrals import save_user, search_clients

        save_user(telegram_id=654321, first_name="Константин", last_name="Хабенский", phone="+79170001234")
        results = search_clients("константин")
        self.assertGreaterEqual(len(results), 1)
        found = any(r.get("first_name") == "Константин" for r in results)
        self.assertTrue(found)

    def test_create_or_update_preserves_terminal_status(self):
        from services.appointments import (
            create_or_update_appointment,
            complete_appointment,
            get_appointment,
        )
        t = datetime.datetime.now(datetime.timezone.utc)
        appt_id = create_or_update_appointment(
            phone="+79992223344",
            appointment_time=t,
            services="Стрижка",
            total_price=2300,
            platform="telegram",
            platform_user_id=888,
            masters_id="mst-terminal-test-1",
        )
        complete_appointment(appt_id)
        self.assertEqual(get_appointment(appt_id)["status"], "completed")

        # Sync comes in again with same masters_id
        create_or_update_appointment(
            phone="+79992223344",
            appointment_time=t,
            services="Стрижка",
            total_price=2300,
            platform="masters",
            masters_id="mst-terminal-test-1",
        )
        # Status MUST remain 'completed', not reset to 'confirmed'
        self.assertEqual(get_appointment(appt_id)["status"], "completed")

    def test_postgres_split_brain_protection(self):
        from unittest.mock import patch
        import core_bot.database.connection as conn_mod

        with patch.object(conn_mod, "IS_POSTGRES", True), \
             patch.object(conn_mod, "ALLOW_SQLITE_FALLBACK", False), \
             patch.object(conn_mod, "get_pg_pool", return_value=None), \
             patch("psycopg.connect", side_effect=Exception("Connection refused")):
            with self.assertRaises(RuntimeError) as ctx:
                with conn_mod.get_connection():
                    pass
            self.assertIn("SQLite fallback disabled", str(ctx.exception))

    def test_suggested_master_phone(self):
        from services.masters_sync import get_suggested_master_phone, DEFAULT_MASTER_PHONE
        # Default fallback
        self.assertEqual(get_suggested_master_phone(), "+79173225155")

        # After saving a session in masters_session
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO masters_session (master_phone, cookie_phpsessid, is_active) VALUES (%s, %s, %s)",
                ("+79173225155", "test_phpsessid", 1),
            )
            conn.commit()

        self.assertEqual(get_suggested_master_phone(), "+79173225155")

    async def test_admin_masters_flow(self):
        from handlers.referrals import (
            admin_masters_status,
            admin_masters_enter_phone,
            admin_masters_request_code,
            handle_admin_text_inputs,
            pending_master_logins,
        )
        from unittest.mock import patch

        admin_id = 802560333  # from ADMIN_IDS
        pending_master_logins.clear()

        # 1. Open admin:masters
        cb = MagicMock()
        cb.from_user.id = admin_id
        cb.data = "admin:masters"
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        await admin_masters_status(cb)
        cb.message.edit_text.assert_called_once()
        text_arg = cb.message.edit_text.call_args[0][0]
        self.assertIn("+79173225155", text_arg)
        kb_arg = cb.message.edit_text.call_args[1]["reply_markup"]
        btn_texts = [btn.text for row in kb_arg.inline_keyboard for btn in row]
        self.assertTrue(any("Ввести другой номер" in t for t in btn_texts))
        self.assertTrue(any("+79173225155" in t for t in btn_texts))

        # 2. Click "Ввести другой номер"
        cb_enter = MagicMock()
        cb_enter.from_user.id = admin_id
        cb_enter.data = "admin:masters_enter_phone"
        cb_enter.message.edit_text = AsyncMock()
        cb_enter.answer = AsyncMock()

        await admin_masters_enter_phone(cb_enter)
        self.assertTrue(pending_master_logins.get(admin_id, {}).get("awaiting_phone"))

        # 3. Type valid phone in chat
        msg = MagicMock()
        msg.from_user.id = admin_id
        msg.text = "+7 917 322 51 55"
        msg.answer = AsyncMock()

        with patch("handlers.referrals.request_master_login") as mock_req:
            mock_req.return_value = (
                True,
                "OK",
                {"phone": "+79173225155", "phone_formatted": "(917) 322-51-55", "csrf": "c1", "cookies": {}},
            )
            await handle_admin_text_inputs(msg)
            mock_req.assert_called_once_with("+7 917 322 51 55")

        self.assertFalse(pending_master_logins[admin_id].get("awaiting_phone"))
        self.assertEqual(msg.answer.call_count, 2)
        self.assertIn("Код подтверждения отправлен", msg.answer.call_args_list[1][0][0])


if __name__ == "__main__":
    unittest.main()



