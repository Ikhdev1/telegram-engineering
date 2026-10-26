from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import Update

from support_bot import app, ui
from support_bot.config import Settings
from support_bot.offline import (
    CLIENT_ID,
    FAKE_TOKEN,
    OWNER_ID,
    CapturedSession,
    OfflineFeed,
    build_runtime,
    callback_update,
    find_callback,
    message_update,
)


class FailingScreenSession(CapturedSession):
    """Fake transport that still records non-screen API calls."""

    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure

    async def make_request(self, bot, method, timeout=None):
        if type(method).__name__ in {"SendMessage", "EditMessageText"}:
            if self.failure == "forbidden":
                raise TelegramForbiddenError(method=method, message="blocked")
            if self.failure == "retry":
                raise TelegramRetryAfter(
                    method=method,
                    message="flood control",
                    retry_after=7,
                )
            if self.failure == "network":
                raise TelegramNetworkError(method=method, message="network down")
            raise AssertionError(f"Unknown failure kind: {self.failure}")
        return await super().make_request(bot, method, timeout)


class DispatcherEdgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="support-edges-")
        self.settings = Settings(
            bot_token=FAKE_TOKEN,
            owner_id=OWNER_ID,
            db_path=Path(self.temporary.name) / "edges.sqlite3",
        )
        self.runtimes = []

    async def asyncTearDown(self) -> None:
        for runtime in self.runtimes:
            await runtime.bot.session.close()
            await runtime.dispatcher.storage.close()
        self.temporary.cleanup()

    def make_runtime(self, session=None):
        runtime = build_runtime(
            self.settings,
            session if session is not None else CapturedSession(),
        )
        self.runtimes.append(runtime)
        return runtime

    async def test_max_special_character_details_render_without_poison(self) -> None:
        runtime = self.make_runtime()
        feed = OfflineFeed(runtime, first_update_id=1_000)
        await feed.message(CLIENT_ID, "/new")
        await feed.message(CLIENT_ID, "Проблема")
        _, preview = await feed.message(CLIENT_ID, "<" * 2000)
        self.assertIsNotNone(preview)
        self.assertEqual((preview.text or "").count("&lt;"), 2000)
        self.assertLessEqual(
            ui.visible_utf16_length(preview.text or ""),
            ui.TELEGRAM_TEXT_LIMIT,
        )
        confirm = find_callback(preview, "draft:ok:")
        _, card = await feed.callback(CLIENT_ID, confirm)
        self.assertIsNotNone(card)
        self.assertEqual((card.text or "").count("&lt;"), 2000)
        self.assertLessEqual(
            ui.visible_utf16_length(card.text or ""),
            ui.TELEGRAM_TEXT_LIMIT,
        )

    async def test_slash_text_reaches_active_draft(self) -> None:
        runtime = self.make_runtime()
        feed = OfflineFeed(runtime, first_update_id=2_000)
        await feed.message(CLIENT_ID, "/new")
        subject = "/etc/nginx/nginx.conf returns 502"
        await feed.message(CLIENT_ID, subject)
        draft = runtime.service.get_draft(CLIENT_ID)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.step, "details")
        self.assertEqual(draft.subject, subject)

        details = "/usr/bin/env is missing from the image"
        await feed.message(CLIENT_ID, details)
        draft = runtime.service.get_draft(CLIENT_ID)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.step, "preview")
        self.assertEqual(draft.details, details)

    async def test_group_and_supergroup_message_and_callback_change_no_state(self) -> None:
        runtime = self.make_runtime()
        feed = OfflineFeed(runtime, first_update_id=3_000)
        for chat_type, chat_id in (("group", -10001), ("supergroup", -10002)):
            with self.subTest(chat_type=chat_type):
                update = message_update(
                    feed.next_update_id(),
                    CLIENT_ID,
                    "/new",
                    chat_type=chat_type,
                    chat_id=chat_id,
                )
                await feed.feed(update)
                self.assertIsNone(runtime.service.get_draft(CLIENT_ID))

                update = callback_update(
                    feed.next_update_id(),
                    CLIENT_ID,
                    "menu:new",
                    chat_type=chat_type,
                    chat_id=chat_id,
                )
                await feed.feed(update)
                self.assertIsNone(runtime.service.get_draft(CLIENT_ID))

    async def _assert_delivery_failure_advances_offset(self, failure: str) -> None:
        runtime = self.make_runtime(FailingScreenSession(failure))
        bot = runtime.bot
        update_id = 70_000
        update = Update.model_validate(
            message_update(update_id, OWNER_ID, "/start"),
            context={"bot": bot},
        )
        stop_event = asyncio.Event()
        offsets: list[int | None] = []
        delivered: list[bool] = []

        async def fake_get_updates(*, offset, timeout, allowed_updates):
            offsets.append(offset)
            if not delivered:
                delivered.append(True)
                return [update]
            stop_event.set()
            return []

        bot.get_updates = fake_get_updates
        with self.assertLogs("support_bot.handlers", level="WARNING") as logs:
            await app.poll_forever(bot, runtime.dispatcher, stop_event=stop_event)

        self.assertEqual(offsets, [None, update_id + 1])
        self.assertTrue(
            any("Screen delivery" in line for line in logs.output),
            logs.output,
        )
        expected = {
            "forbidden": "forbidden",
            "retry": "retry_after=7",
            "network": "network",
        }[failure]
        self.assertTrue(
            any(expected in line for line in logs.output),
            logs.output,
        )

    async def test_screen_delivery_failures_do_not_stop_polling(self) -> None:
        for failure in ("forbidden", "retry", "network"):
            with self.subTest(failure=failure):
                await self._assert_delivery_failure_advances_offset(failure)


if __name__ == "__main__":
    unittest.main()
