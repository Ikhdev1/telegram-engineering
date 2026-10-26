from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

from support_bot import app


class FakeBot:
    def __init__(self, batches, stop_event):
        self.batches = list(batches)
        self.stop_event = stop_event
        self.offsets: list[int | None] = []

    async def get_updates(self, *, offset, timeout, allowed_updates):
        self.offsets.append(offset)
        if self.batches:
            return self.batches.pop(0)
        self.stop_event.set()
        return []


class FakeDispatcher:
    def __init__(self, *, fail_once_on: int | None = None, always_fail: bool = False):
        self.fail_once_on = fail_once_on
        self.always_fail = always_fail
        self.feed_calls: list[int] = []

    async def feed_update(self, bot, update):
        self.feed_calls.append(update.update_id)
        if self.always_fail:
            raise RuntimeError("simulated handler failure")
        if self.fail_once_on == update.update_id:
            self.fail_once_on = None
            raise RuntimeError("simulated transient failure")


class PollingTest(unittest.IsolatedAsyncioTestCase):
    async def test_offset_advances_only_after_successful_retry(self) -> None:
        stop_event = asyncio.Event()
        first = types.SimpleNamespace(update_id=10)
        second = types.SimpleNamespace(update_id=11)
        bot = FakeBot([[first, second]], stop_event)
        dispatcher = FakeDispatcher(fail_once_on=11)

        with mock.patch.object(
            app, "_sleep_unless_stopped", new=mock.AsyncMock()
        ):
            await app.poll_forever(bot, dispatcher, stop_event=stop_event)

        self.assertEqual(bot.offsets, [None, 12])
        self.assertEqual(dispatcher.feed_calls, [10, 11, 11])

    async def test_exhausted_retries_do_not_advance_offset(self) -> None:
        stop_event = asyncio.Event()
        update = types.SimpleNamespace(update_id=20)
        bot = FakeBot([[update]], stop_event)
        dispatcher = FakeDispatcher(always_fail=True)

        with (
            mock.patch.object(app, "MAX_HANDLER_ATTEMPTS", 2),
            mock.patch.object(app, "_sleep_unless_stopped", new=mock.AsyncMock()),
            self.assertRaises(app.PollingError),
        ):
            await app.poll_forever(bot, dispatcher, stop_event=stop_event)

        self.assertEqual(bot.offsets, [None])
        self.assertEqual(dispatcher.feed_calls, [20, 20])
        self.assertFalse(stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
