from __future__ import annotations

import itertools
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from support_bot.config import ConfigError
from support_bot.db import Database
from support_bot.service import DomainError, SupportService

BOT_ID = 42
OWNER = 9_000_000_001
ADMIN = 9_000_000_002
CLIENT = 9_000_000_003
OTHER_CLIENT = 9_000_000_004
OPERATOR_ONE = 9_000_000_005
OPERATOR_TWO = 9_000_000_006


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="support-service-")
        self.db = Database(Path(self.temp.name) / "test.sqlite3")
        self.db.initialize()
        self.service = SupportService(self.db, owner_id=OWNER)
        self.update_ids = itertools.count(100_000)
        for user_id in (
            OWNER,
            ADMIN,
            CLIENT,
            OTHER_CLIENT,
            OPERATOR_ONE,
            OPERATOR_TWO,
        ):
            self.service.touch_user(user_id, f"user_{user_id}")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def next_update(self) -> int:
        return next(self.update_ids)

    def grant_role(
        self,
        actor: int,
        role: str,
        target: int,
        action: str = "grant",
        *,
        private: bool = True,
    ) -> None:
        pending = self.service.begin_role_change(
            actor,
            action,
            role,
            private_chat=private,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertIsNotNone(pending.pending)
        assert pending.pending is not None
        submitted = self.service.submit_role_target(
            actor,
            str(target),
            private_chat=private,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertIsNotNone(submitted.pending)
        assert submitted.pending is not None and submitted.pending.token
        result = self.service.confirm_role_change(
            actor,
            submitted.pending.token,
            private_chat=private,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertTrue(result.applied)

    def create_request(
        self, requester: int = CLIENT, subject: str = "Проблема с VPN", details: str = "Не подключается к сети."
    ):
        started = self.service.start_draft(
            requester, BOT_ID, self.next_update()
        )
        self.assertIsNotNone(started.draft)
        after_subject = self.service.save_draft_subject(
            requester, subject, BOT_ID, self.next_update()
        )
        self.assertEqual(after_subject.draft.step, "details")
        after_details = self.service.save_draft_details(
            requester, details, BOT_ID, self.next_update()
        )
        self.assertEqual(after_details.draft.step, "preview")
        assert after_details.draft is not None and after_details.draft.token
        created = self.service.confirm_draft(
            requester,
            after_details.draft.token,
            BOT_ID,
            self.next_update(),
        )
        self.assertIsNotNone(created.request)
        assert created.request is not None
        return created.request

    def audit_count(self, action: str, object_id: int | str | None = None) -> int:
        with self.db.read() as conn:
            if object_id is None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE action = ?", (action,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE action = ? AND object_id = ?",
                    (action, str(object_id)),
                ).fetchone()
            return int(row[0])


class AccessAndRoleTest(ServiceTestBase):
    def test_first_user_is_not_owner_and_owner_cannot_be_targeted(self) -> None:
        state = self.service.actor_state(CLIENT)
        self.assertFalse(state.is_owner)
        self.assertFalse(state.is_admin)
        self.assertEqual(state.roles, frozenset())

        with self.assertRaises(DomainError) as caught:
            self.service.begin_role_change(
                CLIENT,
                "grant",
                "operator",
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "forbidden")

        pending = self.service.begin_role_change(
            OWNER,
            "grant",
            "admin",
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertIsNotNone(pending.pending)
        with self.assertRaises(DomainError) as caught:
            self.service.submit_role_target(
                OWNER,
                str(OWNER),
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "invalid_target")

    def test_only_owner_grants_admin_and_only_staff_grants_operator(self) -> None:
        with self.assertRaises(DomainError) as caught:
            self.service.begin_role_change(
                CLIENT,
                "grant",
                "operator",
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "forbidden")

        self.grant_role(OWNER, "admin", ADMIN)
        state = self.service.actor_state(ADMIN)
        self.assertTrue(state.is_admin)

        with self.assertRaises(DomainError) as caught:
            self.service.begin_role_change(
                ADMIN,
                "grant",
                "admin",
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "forbidden")

        self.grant_role(ADMIN, "operator", OPERATOR_ONE)
        self.assertTrue(self.service.actor_state(OPERATOR_ONE).is_operator)
        self.grant_role(ADMIN, "operator", OPERATOR_ONE, action="revoke")
        self.assertFalse(self.service.actor_state(OPERATOR_ONE).is_operator)

    def test_all_guarded_service_methods_reject_non_private_context(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        self.grant_role(ADMIN, "operator", OPERATOR_ONE)

        with self.assertRaises(DomainError) as caught:
            self.service.begin_role_change(
                ADMIN,
                "grant",
                "operator",
                private_chat=False,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "private_only")

        pending = self.service.begin_role_change(
            ADMIN,
            "grant",
            "operator",
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertIsNotNone(pending.pending)
        with self.assertRaises(DomainError) as caught:
            self.service.submit_role_target(
                ADMIN,
                str(OPERATOR_TWO),
                private_chat=False,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "private_only")

        self.service.begin_role_change(
            ADMIN,
            "grant",
            "operator",
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        submitted = self.service.submit_role_target(
            ADMIN,
            str(OPERATOR_TWO),
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertIsNotNone(submitted.pending)
        assert submitted.pending is not None and submitted.pending.token
        with self.assertRaises(DomainError) as caught:
            self.service.confirm_role_change(
                ADMIN,
                submitted.pending.token,
                private_chat=False,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "private_only")
        with self.assertRaises(DomainError) as caught:
            self.service.cancel_role_change(
                ADMIN,
                private_chat=False,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "private_only")

        request = self.create_request()
        with self.assertRaises(DomainError) as caught:
            self.service.assign_request(
                ADMIN,
                request.id,
                OPERATOR_ONE,
                1,
                private_chat=False,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "private_only")
        self.service.assign_request(
            ADMIN,
            request.id,
            OPERATOR_ONE,
            1,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        with self.assertRaises(DomainError) as caught:
            self.service.resolve_request(
                OPERATOR_ONE,
                request.id,
                2,
                private_chat=False,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "private_only")
        with self.assertRaises(DomainError) as caught:
            self.service.set_accept_new_requests(
                ADMIN,
                False,
                1,
                private_chat=False,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "private_only")

    def test_role_target_validation_and_private_chat(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        with self.assertRaises(DomainError) as caught:
            self.service.begin_role_change(
                ADMIN,
                "grant",
                "operator",
                private_chat=False,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "private_only")

        pending = self.service.begin_role_change(
            ADMIN,
            "grant",
            "operator",
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertIsNotNone(pending.pending)
        with self.assertRaises(DomainError) as caught:
            self.service.submit_role_target(
                ADMIN,
                "not-a-number",
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "invalid_target")
        with self.assertRaises(DomainError) as caught:
            self.service.submit_role_target(
                ADMIN,
                str(8_888_888_888),
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "unknown_target")

    def test_role_confirmation_token_is_single_use_and_expires(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        pending = self.service.begin_role_change(
            ADMIN,
            "grant",
            "operator",
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        assert pending.pending is not None
        submitted = self.service.submit_role_target(
            ADMIN,
            str(OPERATOR_ONE),
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        assert submitted.pending is not None and submitted.pending.token
        token = submitted.pending.token
        update_id = self.next_update()
        first = self.service.confirm_role_change(
            ADMIN,
            token,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=update_id,
        )
        self.assertFalse(first.duplicate)
        replay = self.service.confirm_role_change(
            ADMIN,
            token,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=update_id,
        )
        self.assertTrue(replay.duplicate)
        self.assertEqual(
            self.audit_count("role.grant", OPERATOR_ONE),
            1,
            "replayed delivery must not append a second role audit",
        )
        with self.assertRaises(DomainError) as caught:
            self.service.confirm_role_change(
                ADMIN,
                token,
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "role_stale")

        # Expired pending confirmation is rejected.
        pending = self.service.begin_role_change(
            ADMIN,
            "grant",
            "operator",
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        assert pending.pending is not None
        submitted = self.service.submit_role_target(
            ADMIN,
            str(OPERATOR_TWO),
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        assert submitted.pending is not None and submitted.pending.token
        with self.db.write() as conn:
            conn.execute(
                "UPDATE role_pending SET expires_at = ? WHERE actor_id = ?",
                ("2000-01-01T00:00:00+00:00", ADMIN),
            )
        with self.assertRaises(DomainError) as caught:
            self.service.confirm_role_change(
                ADMIN,
                submitted.pending.token,
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "role_expired")


class DraftTest(ServiceTestBase):
    def test_three_step_draft_and_duplicate_guards(self) -> None:
        request = self.create_request(
            subject="<b>VPN</b> & wifi",
            details="<script>alert(1)</script> не работает.",
        )
        self.assertEqual(request.subject, "<b>VPN</b> & wifi")
        self.assertEqual(request.status, "new")
        self.assertEqual(request.version, 1)
        self.assertEqual(self.service.get_draft(CLIENT), None)
        self.assertEqual(self.audit_count("request.create", request.id), 1)

        with self.db.read() as conn:
            count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        self.assertEqual(count, 1)

    def test_confirm_replay_and_new_click(self) -> None:
        started = self.service.start_draft(CLIENT, BOT_ID, self.next_update())
        assert started.draft is not None
        self.service.save_draft_subject(CLIENT, "Тема заявки", BOT_ID, self.next_update())
        preview = self.service.save_draft_details(
            CLIENT, "Достаточно длинное описание проблемы.", BOT_ID, self.next_update()
        )
        assert preview.draft is not None and preview.draft.token
        token = preview.draft.token
        update_id = self.next_update()
        first = self.service.confirm_draft(CLIENT, token, BOT_ID, update_id)
        self.assertIsNotNone(first.request)
        replay = self.service.confirm_draft(CLIENT, token, BOT_ID, update_id)
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.request.id, first.request.id)
        with self.assertRaises(DomainError) as caught:
            self.service.confirm_draft(
                CLIENT, token, BOT_ID, self.next_update()
            )
        self.assertEqual(caught.exception.code, "draft_missing")
        with self.db.read() as conn:
            count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        self.assertEqual(count, 1)

    def test_back_cancel_and_validation_preserve_accepted_input(self) -> None:
        self.service.start_draft(CLIENT, BOT_ID, self.next_update())
        self.service.save_draft_subject(
            CLIENT, "Первая тема", BOT_ID, self.next_update()
        )
        back = self.service.draft_back(CLIENT, BOT_ID, self.next_update())
        self.assertEqual(back.draft.step, "subject")
        self.assertEqual(back.draft.subject, "Первая тема")

        with self.assertRaises(DomainError) as caught:
            self.service.draft_back(CLIENT, BOT_ID, self.next_update())
        self.assertEqual(caught.exception.code, "draft_back_unavailable")

        with self.assertRaises(DomainError) as caught:
            self.service.save_draft_subject(
                CLIENT, "x", BOT_ID, self.next_update()
            )
        self.assertEqual(caught.exception.code, "invalid_subject")
        self.assertEqual(self.service.get_draft(CLIENT).step, "subject")

        self.service.save_draft_subject(
            CLIENT, "Вторая тема", BOT_ID, self.next_update()
        )
        preview = self.service.save_draft_details(
            CLIENT, "Описание достаточно длинное.", BOT_ID, self.next_update()
        )
        self.assertEqual(preview.draft.step, "preview")
        back = self.service.draft_back(CLIENT, BOT_ID, self.next_update())
        self.assertEqual(back.draft.step, "details")
        self.assertIsNone(back.draft.token)
        self.assertEqual(back.draft.details, "Описание достаточно длинное.")
        back = self.service.draft_back(CLIENT, BOT_ID, self.next_update())
        self.assertEqual(back.draft.step, "subject")

        self.assertTrue(self.service.cancel_inputs(CLIENT, BOT_ID, self.next_update()))
        self.assertIsNone(self.service.get_draft(CLIENT))

    def test_paused_confirmation_rolls_back_dedup_claim(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        self.service.start_draft(CLIENT, BOT_ID, self.next_update())
        self.service.save_draft_subject(
            CLIENT, "Тема заявки", BOT_ID, self.next_update()
        )
        preview = self.service.save_draft_details(
            CLIENT, "Описание проблемы для проверки.", BOT_ID, self.next_update()
        )
        assert preview.draft is not None and preview.draft.token
        token = preview.draft.token

        paused = self.service.set_accept_new_requests(
            ADMIN,
            False,
            1,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertFalse(paused.value)
        self.assertEqual(paused.version, 2)

        confirm_update = self.next_update()
        with self.assertRaises(DomainError) as caught:
            self.service.confirm_draft(CLIENT, token, BOT_ID, confirm_update)
        self.assertEqual(caught.exception.code, "requests_paused")
        with self.db.read() as conn:
            claimed = conn.execute(
                "SELECT 1 FROM processed_updates WHERE bot_id = ? AND update_id = ?",
                (BOT_ID, confirm_update),
            ).fetchone()
        self.assertIsNone(claimed, "failed mutation must roll back its delivery claim")

        with self.assertRaises(DomainError) as caught:
            self.service.start_draft(CLIENT, BOT_ID, self.next_update())
        self.assertEqual(caught.exception.code, "requests_paused")

        resumed = self.service.set_accept_new_requests(
            ADMIN,
            True,
            2,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertTrue(resumed.value)
        self.assertEqual(resumed.version, 3)
        created = self.service.confirm_draft(CLIENT, token, BOT_ID, confirm_update)
        self.assertIsNotNone(created.request)

    def test_restart_preserves_request_roles_and_delivery_dedup(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        self.service.start_draft(CLIENT, BOT_ID, self.next_update())
        self.service.save_draft_subject(
            CLIENT, "После рестарта", BOT_ID, self.next_update()
        )
        preview = self.service.save_draft_details(
            CLIENT, "Заявка должна пережить перезапуск.", BOT_ID, self.next_update()
        )
        assert preview.draft is not None and preview.draft.token
        confirm_update = self.next_update()
        created = self.service.confirm_draft(
            CLIENT, preview.draft.token, BOT_ID, confirm_update
        )
        assert created.request is not None
        request_id = created.request.id

        restarted_db = Database(self.db.path)
        restarted_db.initialize()
        restarted = SupportService(restarted_db, owner_id=OWNER)
        page = restarted.list_own_requests(CLIENT, 0)
        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].id, request_id)
        self.assertTrue(restarted.actor_state(ADMIN).is_admin)
        self.assertIsNone(restarted.get_draft(CLIENT))

        replay = restarted.confirm_draft(
            CLIENT, preview.draft.token, BOT_ID, confirm_update
        )
        self.assertTrue(replay.duplicate)
        self.assertIsNotNone(replay.request)
        self.assertEqual(replay.request.id, request_id)
        with restarted_db.read() as conn:
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]),
                1,
            )

    def test_accept_setting_is_cas_protected(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        update_id = self.next_update()
        first = self.service.set_accept_new_requests(
            ADMIN,
            False,
            1,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=update_id,
        )
        self.assertEqual(first.version, 2)
        replay = self.service.set_accept_new_requests(
            ADMIN,
            False,
            1,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=update_id,
        )
        self.assertTrue(replay.duplicate)
        with self.assertRaises(DomainError) as caught:
            self.service.set_accept_new_requests(
                ADMIN,
                False,
                1,
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "stale")
        with self.assertRaises(DomainError) as caught:
            self.service.set_accept_new_requests(
                ADMIN,
                True,
                1,
                private_chat=False,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "private_only")


class RequestLifecycleTest(ServiceTestBase):
    def test_own_visibility_list_and_cancel(self) -> None:
        first = self.create_request(subject="Первая", details="Первое описание проблемы.")
        second = self.create_request(subject="Вторая", details="Второе описание проблемы.")
        page = self.service.list_own_requests(CLIENT, 0)
        self.assertEqual(page.total, 2)
        self.assertEqual([item.id for item in page.items], [second.id, first.id])
        far = self.service.list_own_requests(CLIENT, 999)
        self.assertEqual(far.page, far.pages - 1)
        self.assertEqual(far.total, 2)

        with self.assertRaises(DomainError) as caught:
            self.service.get_own_request(OTHER_CLIENT, first.id)
        self.assertEqual(caught.exception.code, "not_found")

        cancelled = self.service.cancel_request(
            CLIENT, first.id, 1, BOT_ID, self.next_update()
        )
        self.assertEqual(cancelled.request.status, "cancelled")
        self.assertEqual(cancelled.request.version, 2)
        self.assertEqual(self.audit_count("request.cancel", first.id), 1)
        with self.assertRaises(DomainError) as caught:
            self.service.cancel_request(
                CLIENT, first.id, 2, BOT_ID, self.next_update()
            )
        self.assertEqual(caught.exception.code, "stale")

    def test_operator_scope_assignment_and_resolution(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        self.grant_role(ADMIN, "operator", OPERATOR_ONE)
        self.grant_role(ADMIN, "operator", OPERATOR_TWO)
        request = self.create_request()

        with self.assertRaises(DomainError) as caught:
            self.service.assign_request(
                CLIENT,
                request.id,
                OPERATOR_ONE,
                1,
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "forbidden")

        with self.assertRaises(DomainError) as caught:
            self.service.assign_request(
                ADMIN,
                request.id,
                CLIENT,
                1,
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "not_operator")

        assigned = self.service.assign_request(
            ADMIN,
            request.id,
            OPERATOR_ONE,
            1,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertEqual(assigned.request.status, "assigned")
        self.assertEqual(assigned.request.assignee_id, OPERATOR_ONE)
        self.assertEqual(assigned.request.version, 2)
        self.assertTrue(assigned.notify_requester)
        self.assertEqual(self.audit_count("request.assign", request.id), 1)

        self.assertEqual(self.service.list_operator_queue(OPERATOR_ONE, 0).total, 1)
        self.assertEqual(self.service.list_operator_queue(OPERATOR_TWO, 0).total, 0)
        with self.assertRaises(DomainError) as caught:
            self.service.get_operator_request(OPERATOR_TWO, request.id)
        self.assertEqual(caught.exception.code, "not_found")

        with self.assertRaises(DomainError) as caught:
            self.service.resolve_request(
                OPERATOR_ONE,
                request.id,
                1,
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "stale")

        resolve_update = self.next_update()
        resolved = self.service.resolve_request(
            OPERATOR_ONE,
            request.id,
            2,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=resolve_update,
        )
        self.assertEqual(resolved.request.status, "resolved")
        self.assertEqual(resolved.request.version, 3)
        self.assertTrue(resolved.notify_requester)
        self.assertEqual(self.audit_count("request.resolve", request.id), 1)
        replay = self.service.resolve_request(
            OPERATOR_ONE,
            request.id,
            2,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=resolve_update,
        )
        self.assertTrue(replay.duplicate)
        self.assertEqual(self.audit_count("request.resolve", request.id), 1)

    def test_revocation_invalidates_existing_assignment_buttons(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        self.grant_role(ADMIN, "operator", OPERATOR_ONE)
        self.grant_role(ADMIN, "operator", OPERATOR_TWO)
        request = self.create_request()
        self.service.assign_request(
            ADMIN,
            request.id,
            OPERATOR_ONE,
            1,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.grant_role(ADMIN, "operator", OPERATOR_ONE, action="revoke")

        with self.assertRaises(DomainError) as caught:
            self.service.list_operator_queue(OPERATOR_ONE, 0)
        self.assertEqual(caught.exception.code, "forbidden")
        with self.assertRaises(DomainError) as caught:
            self.service.resolve_request(
                OPERATOR_ONE,
                request.id,
                2,
                private_chat=True,
                bot_id=BOT_ID,
                update_id=self.next_update(),
            )
        self.assertEqual(caught.exception.code, "forbidden")

        reassigned = self.service.assign_request(
            ADMIN,
            request.id,
            OPERATOR_TWO,
            2,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertEqual(reassigned.request.assignee_id, OPERATOR_TWO)
        self.assertEqual(reassigned.request.version, 3)

    def test_two_admins_cannot_claim_the_same_version_twice(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        self.grant_role(ADMIN, "operator", OPERATOR_ONE)
        self.grant_role(ADMIN, "operator", OPERATOR_TWO)
        request = self.create_request()

        barrier = threading.Barrier(2)

        def attempt(operator_id: int, update_id: int) -> str:
            barrier.wait(timeout=5)
            try:
                self.service.assign_request(
                    ADMIN,
                    request.id,
                    operator_id,
                    1,
                    private_chat=True,
                    bot_id=BOT_ID,
                    update_id=update_id,
                )
            except DomainError as exc:
                return exc.code
            return "ok"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(attempt, OPERATOR_ONE, self.next_update())
            second = pool.submit(attempt, OPERATOR_TWO, self.next_update())
            results = sorted([first.result(timeout=10), second.result(timeout=10)])

        self.assertEqual(results, ["ok", "stale"])
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT status, assignee_id, version FROM requests WHERE id = ?",
                (request.id,),
            ).fetchone()
        self.assertEqual(row["status"], "assigned")
        self.assertIn(int(row["assignee_id"]), {OPERATOR_ONE, OPERATOR_TWO})
        self.assertEqual(int(row["version"]), 2)
        self.assertEqual(self.audit_count("request.assign", request.id), 1)


class SettingsAndAuditTest(ServiceTestBase):
    def test_notification_preference_controls_notify_flag(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        self.grant_role(ADMIN, "operator", OPERATOR_ONE)
        request = self.create_request()

        disabled = self.service.set_notifications(
            CLIENT,
            False,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertFalse(disabled.value)
        self.assertFalse(self.service.actor_state(CLIENT).notifications_enabled)
        assigned = self.service.assign_request(
            ADMIN,
            request.id,
            OPERATOR_ONE,
            1,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertFalse(assigned.notify_requester)

        resolved = self.service.resolve_request(
            OPERATOR_ONE,
            request.id,
            2,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertFalse(resolved.notify_requester)

        second = self.create_request(subject="Вторая", details="Другое описание проблемы.")
        self.service.set_notifications(
            CLIENT,
            True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        assigned = self.service.assign_request(
            ADMIN,
            second.id,
            OPERATOR_ONE,
            1,
            private_chat=True,
            bot_id=BOT_ID,
            update_id=self.next_update(),
        )
        self.assertTrue(assigned.notify_requester)

    def test_audit_is_admin_only_and_contains_no_request_content(self) -> None:
        self.grant_role(OWNER, "admin", ADMIN)
        self.create_request(subject="Секретная тема", details="Секретное описание.")
        page = self.service.audit_page(ADMIN, 0)
        self.assertGreaterEqual(page.total, 1)
        with self.db.read() as conn:
            details = " ".join(
                str(row["details"]) for row in conn.execute("SELECT details FROM audit_events")
            )
        self.assertNotIn("Секретная тема", details)
        self.assertNotIn("Секретное описание", details)

        with self.assertRaises(DomainError) as caught:
            self.service.audit_page(CLIENT, 0)
        self.assertEqual(caught.exception.code, "forbidden")
        with self.assertRaises(DomainError) as caught:
            self.service.get_admin_settings(CLIENT)
        self.assertEqual(caught.exception.code, "forbidden")
        accept, version = self.service.get_admin_settings(OWNER)
        self.assertTrue(accept)
        self.assertEqual(version, 1)


class MigrationTest(unittest.TestCase):
    def test_schema_version_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="support-migration-") as directory:
            path = Path(directory) / "migrate.sqlite3"
            database = Database(path)
            database.initialize()
            database.initialize()
            with database.read() as conn:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(version, 1)
            self.assertIn("requests", tables)
            self.assertIn("processed_updates", tables)

    def test_future_schema_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="support-migration-") as directory:
            path = Path(directory) / "future.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
            connection.close()
            with self.assertRaises(RuntimeError):
                Database(path).initialize()

    def test_active_writer_maps_to_clear_config_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="support-lock-") as directory:
            path = Path(directory) / "locked.sqlite3"
            Database(path).initialize()
            holder = sqlite3.connect(path, isolation_level=None)
            try:
                holder.execute("BEGIN IMMEDIATE")
                with self.assertRaises(ConfigError):
                    Database(path, busy_timeout_ms=50).initialize()
            finally:
                holder.rollback()
                holder.close()


if __name__ == "__main__":
    unittest.main()
