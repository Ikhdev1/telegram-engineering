"""Runtime wiring and an explicit, offset-safe polling loop."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramConflictError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import Update

from .config import ConfigError, Settings
from .db import Database
from .handlers import build_router
from .service import SupportService

logger = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]
POLLING_TIMEOUT_SECONDS = 10
MAX_HANDLER_ATTEMPTS = 3
MAX_POLL_BACKOFF_SECONDS = 10.0


class PollingError(RuntimeError):
    """Polling stopped because an update could not be processed safely."""


def build_bot(settings: Settings, session: BaseSession | None = None) -> Bot:
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )


def build_dispatcher(service: SupportService) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(service))
    return dispatcher


async def _sleep_unless_stopped(stop_event: asyncio.Event, delay: float) -> None:
    if delay <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        pass


async def _handle_update_safely(
    bot: Bot,
    dispatcher: Dispatcher,
    update: Update,
    stop_event: asyncio.Event,
) -> None:
    """Run one update, retrying without advancing the getUpdates offset.

    ``Dispatcher.feed_update`` propagates handler exceptions (unlike aiogram's
    polling ``_process_update`` helper, which logs and swallows them).  A
    retry is safe because every mutation and its ``(bot_id, update_id)`` claim
    commit in one SQLite transaction.  If the database already committed and
    rendering failed, the replay sees the stored outcome instead of repeating
    the side effect.
    """

    for attempt in range(1, MAX_HANDLER_ATTEMPTS + 1):
        try:
            await dispatcher.feed_update(bot, update)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry boundary
            logger.exception(
                "Update %s failed (attempt %s/%s)",
                update.update_id,
                attempt,
                MAX_HANDLER_ATTEMPTS,
            )
            if attempt >= MAX_HANDLER_ATTEMPTS:
                raise PollingError(
                    f"Update {update.update_id} was not processed after "
                    f"{MAX_HANDLER_ATTEMPTS} attempts; offset was not advanced."
                ) from exc
            await _sleep_unless_stopped(stop_event, float(attempt))


async def poll_forever(
    bot: Bot,
    dispatcher: Dispatcher,
    *,
    stop_event: asyncio.Event,
) -> None:
    """Consume updates, advancing the offset only after successful handling.

    A getUpdates call with offset N confirms every update below N.  This loop
    does not call getUpdates after a failed update, so Telegram keeps the
    update for redelivery.  On restart, delivery dedup makes the replay
    harmless even if the database mutation had already committed.
    """

    offset: int | None = None
    network_failures = 0
    while not stop_event.is_set():
        try:
            updates = await bot.get_updates(
                offset=offset,
                timeout=POLLING_TIMEOUT_SECONDS,
                allowed_updates=ALLOWED_UPDATES,
            )
        except TelegramRetryAfter as exc:
            logger.warning("Telegram asked to retry getUpdates after %ss", exc.retry_after)
            await _sleep_unless_stopped(stop_event, float(exc.retry_after))
            continue
        except (TelegramUnauthorizedError, TelegramConflictError) as exc:
            raise ConfigError(
                "Telegram отклонил getUpdates. Проверьте токен и убедитесь, что "
                "webhook снят, а второй polling-процесс не использует этот токен. "
                "Приложение не удаляет webhook автоматически."
            ) from exc
        except TelegramAPIError as exc:
            network_failures += 1
            delay = min(
                MAX_POLL_BACKOFF_SECONDS,
                2.0 ** min(network_failures - 1, 3),
            )
            logger.warning(
                "getUpdates failed (%s); retrying in %.0fs without advancing offset",
                type(exc).__name__,
                delay,
            )
            await _sleep_unless_stopped(stop_event, delay)
            continue

        network_failures = 0
        if not updates:
            continue

        for update in updates:
            if stop_event.is_set():
                return
            await _handle_update_safely(bot, dispatcher, update, stop_event)
            # Only now may a later getUpdates call confirm this update.
            offset = update.update_id + 1


async def run_polling(settings: Settings) -> None:
    """Run one long-polling consumer for this token and database.

    Polling never deletes an existing webhook.  If one is configured, startup
    fails explicitly so the operator can decide which transport should own the
    token.  The getUpdates offset is advanced only after a handler succeeds;
    the offset is not persisted separately because Telegram itself retains
    unconfirmed updates and delivery dedup covers redelivery after a crash.
    """

    database = Database(settings.db_path)
    database.initialize()
    service = SupportService(database, owner_id=settings.owner_id)
    bot = build_bot(settings)
    dispatcher: Dispatcher | None = None
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []

    # POSIX gets graceful SIGINT/SIGTERM.  Windows asyncio does not implement
    # add_signal_handler; there only Ctrl+C/KeyboardInterrupt reaches this
    # finally path.  SIGTERM/taskkill on Windows terminates without it.
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal_name, stop_event.set)
            registered_signals.append(signal_name)

    try:
        dispatcher = build_dispatcher(service)
        try:
            webhook = await bot.get_webhook_info()
        except TelegramUnauthorizedError as exc:
            raise ConfigError(
                "Telegram отклонил BOT_TOKEN при проверке webhook. "
                "Проверьте токен локально; он не выводится в лог."
            ) from exc
        if webhook.url:
            raise ConfigError(
                "У бота уже настроен webhook. Polling не запускается и webhook "
                "не удаляется автоматически: сначала явно выберите один транспорт."
            )
        logger.info("Starting polling for bot id %s", bot.id)
        polling_task = asyncio.create_task(
            poll_forever(bot, dispatcher, stop_event=stop_event),
            name="support-bot-polling",
        )
        stop_task = asyncio.create_task(stop_event.wait(), name="support-bot-stop")
        try:
            done, _ = await asyncio.wait(
                {polling_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if polling_task in done:
                await polling_task
        finally:
            stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task
            if not polling_task.done():
                polling_task.cancel()
                with suppress(asyncio.CancelledError):
                    await polling_task
    finally:
        for signal_name in registered_signals:
            with suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signal_name)
        await bot.session.close()
        if dispatcher is not None:
            await dispatcher.storage.close()
