from __future__ import annotations

import unittest
from dataclasses import replace

from support_bot import ui
from support_bot.callbacks import CallbackError, encode, parse, parse_id, parse_uint
from support_bot.models import (
    ActorState,
    AuditEntry,
    Draft,
    Page,
    PendingRole,
    RoleEntry,
    SupportRequest,
)
from support_bot.util import MAX_TELEGRAM_ID, utc_after, utc_now


def all_callbacks(screen: ui.Screen) -> list[str]:
    if screen.keyboard is None:
        return []
    result: list[str] = []
    for row in screen.keyboard.inline_keyboard:
        for button in row:
            if button.callback_data:
                result.append(button.callback_data)
    return result


class CallbackCodecTest(unittest.TestCase):
    def test_boundary_and_validation(self) -> None:
        self.assertEqual(encode("a" * 64), "a" * 64)
        with self.assertRaises(CallbackError):
            encode("a" * 65)
        with self.assertRaises(CallbackError):
            encode("a" * 40, "b" * 30)
        with self.assertRaises(CallbackError):
            encode("bad part")
        with self.assertRaises(CallbackError):
            encode("")
        with self.assertRaises(CallbackError):
            parse("a" * 65)
        with self.assertRaises(CallbackError):
            parse("a::b")
        with self.assertRaises(CallbackError):
            parse("")
        with self.assertRaises(CallbackError):
            parse("a b:c")

    def test_id_and_page_parsing(self) -> None:
        self.assertEqual(parse_id("1"), 1)
        self.assertEqual(parse_id(str(MAX_TELEGRAM_ID)), MAX_TELEGRAM_ID)
        for value in ("0", "01", "-1", "+1", "abc", str(MAX_TELEGRAM_ID + 1)):
            with self.subTest(value=value):
                with self.assertRaises(CallbackError):
                    parse_id(value)
        self.assertEqual(parse_uint("0"), 0)
        self.assertEqual(parse_uint("10", maximum=10), 10)
        with self.assertRaises(CallbackError):
            parse_uint("00")
        with self.assertRaises(CallbackError):
            parse_uint("11", maximum=10)
        with self.assertRaises(CallbackError):
            parse_uint("-1")


class ScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.request = SupportRequest(
            id=17,
            requester_id=123,
            subject="<b>VIP</b> & <script>",
            details="Строка <i>с HTML</i>",
            status="assigned",
            assignee_id=456,
            version=3,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.draft = Draft(
            telegram_id=123,
            step="preview",
            subject="<b>Тема</b> &",
            details="Описание с <script>.",
            token="a" * 20,
            updated_at=utc_now(),
        )
        self.operator = RoleEntry(
            telegram_id=456,
            role="operator",
            username="operator_name",
            granted_by=1,
            granted_at=utc_now(),
        )
        self.pending = PendingRole(
            actor_id=1,
            action="grant",
            role="operator",
            stage="confirm",
            target_id=456,
            target_username="operator_name",
            token="b" * 20,
            expires_at=utc_after(5),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.owner = ActorState(
            telegram_id=1,
            is_owner=True,
            roles=frozenset(),
            notifications_enabled=True,
            accept_new_requests=True,
            accept_version=1,
        )
        self.client = ActorState(
            telegram_id=123,
            is_owner=False,
            roles=frozenset(),
            notifications_enabled=False,
            accept_new_requests=True,
            accept_version=1,
        )

    def assert_screen_callbacks(self, screen: ui.Screen) -> None:
        for data in all_callbacks(screen):
            self.assertLessEqual(len(data.encode("utf-8")), 64, data)
            self.assertTrue(parse(data), data)

    def test_every_rendered_callback_fits_and_parses(self) -> None:
        page = Page(items=(self.request,), page=0, pages=1, total=1)
        audit = Page(
            items=(
                AuditEntry(
                    id=1,
                    actor_id=1,
                    action="request.assign",
                    object_type="request",
                    object_id="17",
                    outcome="assigned",
                    details="{}",
                    created_at=utc_now(),
                ),
            ),
            page=0,
            pages=1,
            total=1,
        )
        screens = [
            ui.main_menu(self.owner),
            ui.main_menu(self.client),
            ui.help_screen(),
            ui.draft_screen(self.draft),
            ui.own_list(self.client, page),
            ui.own_card(self.request, 0),
            ui.cancel_confirmation(self.request, 0),
            ui.operator_queue(page),
            ui.operator_card(self.request, 0),
            ui.operator_resolved(self.request),
            ui.admin_panel(self.owner),
            ui.admin_queue(page),
            ui.admin_card(self.request, 0),
            ui.assign_picker(self.request, (self.operator,), 0),
            ui.roles_screen((self.operator,), self.owner),
            ui.make_role_prompt("grant", "operator"),
            ui.role_confirmation(self.pending),
            ui.user_settings(self.client),
            ui.admin_settings(True, 1),
            ui.audit_screen(audit),
            ui.error_screen("Тестовая ошибка"),
        ]
        for screen in screens:
            self.assert_screen_callbacks(screen)

    def test_user_content_is_escaped(self) -> None:
        card = ui.own_card(self.request, 0)
        self.assertIn("&lt;b&gt;VIP&lt;/b&gt; &amp; &lt;script&gt;", card.text)
        self.assertNotIn("<b>VIP</b>", card.text)
        self.assertIn("Строка &lt;i&gt;с HTML&lt;/i&gt;", card.text)
        preview = ui.draft_screen(self.draft)
        self.assertIn("&lt;b&gt;Тема&lt;/b&gt; &amp;", preview.text)
        self.assertNotIn("<b>Тема</b>", preview.text)
        confirmation = ui.role_confirmation(self.pending)
        self.assertIn("operator_name", confirmation.text)

    def test_max_special_characters_are_not_falsely_truncated(self) -> None:
        pairs = (("<", "&lt;"), ("&", "&amp;"), (">", "&gt;"))
        for character, entity in pairs:
            with self.subTest(character=character):
                details = character * 2000
                draft = replace(self.draft, subject="Тема", details=details)
                screens = [
                    ui.draft_screen(draft),
                    ui.draft_screen(
                        replace(draft, step="details", token=None)
                    ),
                    ui.own_card(replace(self.request, subject="Тема", details=details), 0),
                    ui.operator_card(
                        replace(self.request, subject="Тема", details=details), 0
                    ),
                    ui.admin_card(
                        replace(self.request, subject="Тема", details=details), 0
                    ),
                ]
                for screen in screens:
                    self.assertEqual(screen.text.count(entity), 2000, screen.text[:80])
                    self.assertLessEqual(
                        ui.visible_utf16_length(screen.text),
                        ui.TELEGRAM_TEXT_LIMIT,
                    )
                    self.assertNotIn(ui.TRUNCATION_MARKER, screen.text)

    def test_local_budget_marks_astral_truncation_explicitly(self) -> None:
        # Legacy/manual rows can exceed the validated 2000-character limit.
        details = "😀" * 2200
        draft = replace(self.draft, subject="Тема", details=details)
        screen = ui.draft_screen(draft)
        self.assertIn(ui.TRUNCATION_MARKER, screen.text)
        self.assertLessEqual(
            ui.visible_utf16_length(screen.text),
            ui.SCREEN_VISIBLE_LIMIT,
        )
        self.assertGreater(screen.text.count("😀"), 0)
        self.assertLess(screen.text.count("😀"), 2200)

    def test_main_menu_reflects_server_roles_and_paused_state(self) -> None:
        owner_callbacks = all_callbacks(ui.main_menu(self.owner))
        self.assertIn("menu:manage", owner_callbacks)
        client_callbacks = all_callbacks(ui.main_menu(self.client))
        self.assertNotIn("menu:manage", client_callbacks)
        paused = ActorState(
            telegram_id=1,
            is_owner=False,
            roles=frozenset(),
            notifications_enabled=True,
            accept_new_requests=False,
            accept_version=2,
        )
        paused_callbacks = all_callbacks(ui.main_menu(paused))
        self.assertNotIn("menu:new", paused_callbacks)


if __name__ == "__main__":
    unittest.main()
