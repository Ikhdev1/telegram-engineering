"""Offline end-to-end smoke: real aiogram Dispatcher + temporary SQLite.

The fake session is used *only* here (and by the explicit tests).  It records
outgoing Bot API methods and returns small valid aiogram objects instead of
making network calls.  Ordinary application code always constructs a real Bot
with the default aiohttp session.
"""

from __future__ import annotations

import asyncio
import itertools
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods.base import TelegramMethod
from aiogram.types import Chat, InlineKeyboardMarkup, Message

from .app import build_bot, build_dispatcher
from .config import Settings
from .db import Database
from .service import SupportService

BOT_ID = 42
FAKE_TOKEN = "42:TEST"
OWNER_ID = 1_000_000_001
ADMIN_ID = 1_000_000_002
CLIENT_ID = 1_000_000_003
OTHER_CLIENT_ID = 1_000_000_004
OPERATOR_ONE = 1_000_000_005
OPERATOR_TWO = 1_000_000_006


class SmokeFailure(AssertionError):
    pass


@dataclass(frozen=True)
class CapturedCall:
    method: str
    chat_id: int | None
    text: str | None
    message_id: int | None
    callback_query_id: str | None
    reply_markup: InlineKeyboardMarkup | None


class CapturedSession(BaseSession):
    """No-network BaseSession for smoke/tests only."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[CapturedCall] = []
        self.closed = False
        self._message_ids = itertools.count(1_500_000)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        method_name = type(method).__name__
        raw_chat = getattr(method, "chat_id", None)
        if hasattr(raw_chat, "id"):
            chat_id = int(raw_chat.id)
        elif raw_chat is not None:
            chat_id = int(raw_chat)
        else:
            chat_id = None
        text = getattr(method, "text", None)
        supplied_message_id = getattr(method, "message_id", None)
        reply_markup = getattr(method, "reply_markup", None)
        callback_query_id = getattr(method, "callback_query_id", None)

        self.calls.append(
            CapturedCall(
                method=method_name,
                chat_id=chat_id,
                text=str(text) if text is not None else None,
                message_id=(
                    int(supplied_message_id)
                    if supplied_message_id is not None
                    else None
                ),
                callback_query_id=(
                    str(callback_query_id)
                    if callback_query_id is not None
                    else None
                ),
                reply_markup=reply_markup,
            )
        )

        if method_name == "AnswerCallbackQuery":
            return True
        if method_name in {"SendMessage", "EditMessageText"}:
            message_id = (
                int(supplied_message_id)
                if supplied_message_id is not None
                else next(self._message_ids)
            )
            return Message(
                message_id=message_id,
                date=datetime.now(timezone.utc),
                chat=Chat(id=chat_id or 0, type="private"),
                text=str(text or ""),
            )
        if method_name == "DeleteMessage":
            return True
        raise RuntimeError(
            f"Offline fake session received unsupported method {method_name}. "
            "The smoke would otherwise need network access."
        )

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise RuntimeError("Offline smoke does not download files.")
        yield b""  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    def last_screen(self, start: int = 0) -> CapturedCall | None:
        for call in reversed(self.calls[start:]):
            if call.method in {"SendMessage", "EditMessageText"}:
                return call
        return None

    def screens(self, start: int = 0) -> list[CapturedCall]:
        return [
            call
            for call in self.calls[start:]
            if call.method in {"SendMessage", "EditMessageText"}
        ]

    def sent_texts_to(self, chat_id: int, start: int = 0) -> list[str]:
        return [
            call.text or ""
            for call in self.calls[start:]
            if call.method == "SendMessage" and call.chat_id == chat_id
        ]


def callbacks_in(markup: InlineKeyboardMarkup | None) -> list[str]:
    if markup is None:
        return []
    result: list[str] = []
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data:
                result.append(button.callback_data)
    return result


def find_callback(screen: CapturedCall | None, prefix: str) -> str:
    if screen is None:
        raise SmokeFailure("Expected a rendered screen, got none.")
    for data in callbacks_in(screen.reply_markup):
        if data.startswith(prefix):
            return data
    raise SmokeFailure(f"Callback with prefix {prefix!r} not found in {screen.text!r}")


def find_callbacks(screen: CapturedCall | None, prefix: str) -> list[str]:
    if screen is None:
        raise SmokeFailure("Expected a rendered screen, got none.")
    return [data for data in callbacks_in(screen.reply_markup) if data.startswith(prefix)]


def user_payload(user_id: int, username: str | None = None) -> dict[str, Any]:
    return {
        "id": user_id,
        "is_bot": False,
        "first_name": f"Smoke {user_id}",
        "username": username or f"smoke_{user_id}",
    }


def message_update(
    update_id: int,
    user_id: int,
    text: str,
    *,
    chat_type: str = "private",
    chat_id: int | None = None,
) -> dict[str, Any]:
    actual_chat_id = chat_id if chat_id is not None else user_id
    return {
        "update_id": update_id,
        "message": {
            "message_id": 700_000 + update_id,
            "from": user_payload(user_id),
            "chat": {"id": actual_chat_id, "type": chat_type},
            "date": int(time.time()),
            "text": text,
        },
    }


def callback_update(
    update_id: int,
    user_id: int,
    data: str,
    *,
    chat_type: str = "private",
    chat_id: int | None = None,
) -> dict[str, Any]:
    actual_chat_id = chat_id if chat_id is not None else user_id
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": user_payload(user_id),
            "chat_instance": f"chat-instance-{user_id}",
            "data": data,
            "message": {
                "message_id": 600_000 + update_id,
                "from": {
                    "id": BOT_ID,
                    "is_bot": True,
                    "first_name": "Support",
                    "username": "support_smoke_bot",
                },
                "chat": {"id": actual_chat_id, "type": chat_type},
                "date": int(time.time()),
                "text": "Smoke screen",
            },
        },
    }


@dataclass
class Runtime:
    settings: Settings
    database: Database
    service: SupportService
    capture: CapturedSession
    bot: Bot
    dispatcher: Dispatcher


def build_runtime(settings: Settings, session: CapturedSession) -> Runtime:
    database = Database(settings.db_path)
    database.initialize()
    service = SupportService(database, owner_id=settings.owner_id)
    bot = build_bot(settings, session=session)
    dispatcher = build_dispatcher(service)
    return Runtime(settings, database, service, session, bot, dispatcher)


class OfflineFeed:
    def __init__(
        self,
        runtime: Runtime,
        *,
        first_update_id: int = 10_000,
    ) -> None:
        self.runtime = runtime
        self._update_ids = itertools.count(first_update_id)

    def next_update_id(self) -> int:
        return next(self._update_ids)

    async def feed(self, update: dict[str, Any]) -> CapturedCall | None:
        marker = len(self.runtime.capture.calls)
        await self.runtime.dispatcher.feed_raw_update(
            bot=self.runtime.bot,
            update=update,
        )
        return self.runtime.capture.last_screen(marker)

    async def message(
        self, user_id: int, text: str
    ) -> tuple[dict[str, Any], CapturedCall | None]:
        update = message_update(self.next_update_id(), user_id, text)
        return update, await self.feed(update)

    async def callback(
        self, user_id: int, data: str
    ) -> tuple[dict[str, Any], CapturedCall | None]:
        update = callback_update(self.next_update_id(), user_id, data)
        return update, await self.feed(update)


@dataclass(frozen=True)
class SmokeReport:
    checks: tuple[str, ...]
    ok: bool = True
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.checks)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "count": self.count,
            "checks": list(self.checks),
        }
        if self.error:
            payload["error"] = self.error
        return payload


class _Checks:
    def __init__(self) -> None:
        self.items: list[str] = []

    def ok(self, condition: bool, message: str) -> None:
        if not condition:
            raise SmokeFailure(message)
        self.items.append(message)

    def text_has(
        self,
        screen: CapturedCall | None,
        expected: str,
        message: str,
    ) -> None:
        if screen is None:
            raise SmokeFailure(f"{message}: no screen captured")
        self.ok(expected in (screen.text or ""), f"{message} (found {expected!r})")


async def _grant_role(
    feed: OfflineFeed,
    checks: _Checks,
    actor_id: int,
    target_id: int,
    role: str,
    action: str = "grant",
) -> CapturedCall | None:
    _, roles_screen = await feed.callback(actor_id, "adm:roles")
    checks.text_has(roles_screen, "Роли", f"roles screen opened by {actor_id}")
    _, prompt = await feed.callback(actor_id, f"adm:role:{action}:{role}")
    checks.text_has(prompt, "numeric", f"{action} {role} asks for numeric ID")
    _, confirmation = await feed.message(actor_id, str(target_id))
    confirm_callback = find_callback(confirmation, "role:ok:")
    _, done = await feed.callback(actor_id, confirm_callback)
    checks.text_has(done, "Роли", f"{action} {role} returns to roles")
    return done


def _scalar(database: Database, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with database.read() as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row is not None else None


async def run_offline_smoke(
    *,
    database_path: str | Path | None = None,
) -> SmokeReport:
    """Exercise the actual dispatcher, renderers and durable SQLite state.

    No token, network, webhook or Telegram account is used.  A caller-provided
    ``database_path`` is left in place for inspection; otherwise a temporary
    directory is removed at the end.  Every fake transport is closed even when
    an assertion fails, and each SQLite operation uses and closes its own
    connection.
    """

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if database_path is None:
        temporary = tempfile.TemporaryDirectory(prefix="support-bot-smoke-")
        path = Path(temporary.name) / "smoke.sqlite3"
    else:
        path = Path(database_path)

    settings = Settings(
        bot_token=FAKE_TOKEN,
        owner_id=OWNER_ID,
        db_path=path,
    )
    checks = _Checks()
    all_callbacks: list[str] = []
    runtimes: list[Runtime] = []

    try:
        capture = CapturedSession()
        runtime = build_runtime(settings, capture)
        runtimes.append(runtime)
        feed = OfflineFeed(runtime, first_update_id=10_000)

        for user_id in (
            OWNER_ID,
            ADMIN_ID,
            CLIENT_ID,
            OTHER_CLIENT_ID,
            OPERATOR_ONE,
            OPERATOR_TWO,
        ):
            _, screen = await feed.message(user_id, "/start")
            checks.text_has(screen, "Поддержка", f"start menu rendered for {user_id}")
        _, owner_menu = await feed.message(OWNER_ID, "/menu")
        checks.ok(
            "menu:manage" in callbacks_in(owner_menu.reply_markup if owner_menu else None),
            "owner sees management button",
        )

        await _grant_role(feed, checks, OWNER_ID, ADMIN_ID, "admin")
        await _grant_role(feed, checks, ADMIN_ID, OPERATOR_ONE, "operator")
        await _grant_role(feed, checks, ADMIN_ID, OPERATOR_TWO, "operator")

        with runtime.database.read() as conn:
            roles = {
                (int(row["telegram_id"]), str(row["role"]))
                for row in conn.execute("SELECT telegram_id, role FROM role_bindings")
            }
        checks.ok(
            (ADMIN_ID, "admin") in roles,
            "owner granted admin through the two-step panel",
        )
        checks.ok(
            (OPERATOR_ONE, "operator") in roles
            and (OPERATOR_TWO, "operator") in roles,
            "admin granted both operator roles",
        )

        _, operator_menu = await feed.message(OPERATOR_ONE, "/menu")
        checks.ok(
            "op:list:0" in callbacks_in(operator_menu.reply_markup if operator_menu else None),
            "operator sees personal queue button",
        )

        # Client: multi-step draft, escaping, confirmation and ownership.
        _, prompt = await feed.message(CLIENT_ID, "/new")
        checks.text_has(prompt, "шаг 1 из 3", "client starts a three-step draft")
        subject = "<b>VPN</b> & wifi"
        details = "<script>alert(1)</script>\nНе подключается к корпоративной сети."
        _, prompt = await feed.message(CLIENT_ID, subject)
        checks.text_has(prompt, "шаг 2 из 3", "subject accepted, details requested")
        _, preview = await feed.message(CLIENT_ID, details)
        checks.text_has(preview, "шаг 3 из 3", "details accepted, preview rendered")
        preview_text = preview.text if preview else ""
        checks.ok(
            "&lt;b&gt;VPN&lt;/b&gt; &amp; wifi" in preview_text,
            "user subject is HTML-escaped in preview",
        )
        checks.ok(
            "<b>VPN</b>" not in preview_text,
            "raw user HTML is not injected into preview",
        )
        confirm_callback = find_callback(preview, "draft:ok:")
        confirm_update_id = feed.next_update_id()
        confirm_update = callback_update(confirm_update_id, CLIENT_ID, confirm_callback)
        marker = len(capture.calls)
        await feed.feed(confirm_update)
        created_screen = capture.last_screen(marker)
        checks.text_has(created_screen, "Заявка #", "confirmation renders request card")

        request_id = int(
            _scalar(runtime.database, "SELECT id FROM requests WHERE requester_id = ?", (CLIENT_ID,))
            or 0
        )
        checks.ok(request_id > 0, "request persisted in SQLite")
        checks.ok(
            _scalar(runtime.database, "SELECT COUNT(*) FROM requests") == 1,
            "exactly one request exists after confirmation",
        )
        checks.ok(
            _scalar(runtime.database, "SELECT COUNT(*) FROM drafts WHERE telegram_id = ?", (CLIENT_ID,))
            == 0,
            "draft is consumed atomically with request creation",
        )

        # Replay of the same raw Update.
        replay_screen = await feed.feed(confirm_update)
        checks.text_has(
            replay_screen,
            "уже было обработано",
            "same update_id is deduplicated",
        )
        checks.ok(
            _scalar(runtime.database, "SELECT COUNT(*) FROM requests") == 1,
            "deduplicated replay did not create another request",
        )
        # A new Update with the same old button is a semantic double click.
        _, new_click = await feed.callback(CLIENT_ID, confirm_callback)
        checks.text_has(
            new_click,
            "устарел",
            "new update id with consumed confirmation is rejected",
        )
        checks.ok(
            _scalar(runtime.database, "SELECT COUNT(*) FROM requests") == 1,
            "semantic double-click did not create another request",
        )

        _, own_list = await feed.message(CLIENT_ID, "/my")
        checks.ok(
            f"req:{request_id}:0"
            in callbacks_in(own_list.reply_markup if own_list else None),
            "client sees own request in list",
        )
        _, own_card = await feed.callback(CLIENT_ID, f"req:{request_id}:0")
        checks.ok(
            "&lt;b&gt;VPN&lt;/b&gt; &amp; wifi" in (own_card.text or ""),
            "own card keeps user content escaped",
        )
        _, denied = await feed.callback(OTHER_CLIENT_ID, f"req:{request_id}:0")
        checks.text_has(denied, "не найдена или недоступна", "other client is denied own-card")
        checks.ok(
            "VPN" not in (denied.text or ""),
            "denial screen does not leak the subject",
        )

        # Restart: a new Dispatcher/Bot over the same file, new fake transport.
        await runtime.bot.session.close()
        capture_two = CapturedSession()
        runtime_two = build_runtime(settings, capture_two)
        runtimes.append(runtime_two)
        feed_two = OfflineFeed(runtime_two, first_update_id=50_000)
        _, after_restart = await feed_two.message(CLIENT_ID, "/my")
        checks.ok(
            f"req:{request_id}:0"
            in callbacks_in(after_restart.reply_markup if after_restart else None),
            "request survives process restart",
        )
        replay_after_restart = await feed_two.feed(confirm_update)
        checks.text_has(
            replay_after_restart,
            "уже было обработано",
            "delivery dedup survives restart",
        )
        checks.ok(
            _scalar(runtime_two.database, "SELECT COUNT(*) FROM requests") == 1,
            "restart/replay still leaves exactly one request",
        )

        # Admin queue: open card, assign concurrently to two operators.
        _, admin_queue = await feed_two.callback(ADMIN_ID, "adm:queue:0")
        checks.ok(
            f"adm:req:{request_id}:0"
            in callbacks_in(admin_queue.reply_markup if admin_queue else None),
            "admin sees request in queue",
        )
        _, admin_card = await feed_two.callback(ADMIN_ID, f"adm:req:{request_id}:0")
        checks.ok(
            f"adm:assign:{request_id}:0"
            in callbacks_in(admin_card.reply_markup if admin_card else None),
            "admin card offers assignment",
        )
        _, picker = await feed_two.callback(ADMIN_ID, f"adm:assign:{request_id}:0")
        pick_callbacks = find_callbacks(picker, f"adm:pick:{request_id}:")
        checks.ok(
            len(pick_callbacks) == 2,
            "assignment picker contains both active operators",
        )
        for data in callbacks_in(picker.reply_markup if picker else None):
            all_callbacks.append(data)
            checks.ok(
                len(data.encode("utf-8")) <= 64,
                f"callback within 64 bytes: {data}",
            )

        concurrent_marker = len(capture_two.calls)
        first_update = callback_update(feed_two.next_update_id(), ADMIN_ID, pick_callbacks[0])
        second_update = callback_update(feed_two.next_update_id(), ADMIN_ID, pick_callbacks[1])
        await asyncio.gather(
            feed_two.feed(first_update),
            feed_two.feed(second_update),
        )
        with runtime_two.database.read() as conn:
            row = conn.execute(
                "SELECT status, assignee_id, version FROM requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        checks.ok(row is not None and str(row["status"]) == "assigned", "one concurrent assignment wins")
        checks.ok(
            int(row["assignee_id"]) in {OPERATOR_ONE, OPERATOR_TWO},
            "winner is one of the two operators",
        )
        checks.ok(int(row["version"]) == 2, "version advanced exactly once")
        checks.ok(
            _scalar(
                runtime_two.database,
                "SELECT COUNT(*) FROM audit_events WHERE action = 'request.assign' AND object_id = ?",
                (str(request_id),),
            )
            == 1,
            "exactly one assignment audit row",
        )
        assignment_notifications = [
            text
            for text in capture_two.sent_texts_to(CLIENT_ID, concurrent_marker)
            if "назначена" in text
        ]
        checks.ok(
            len(assignment_notifications) == 1,
            "requester receives one best-effort assignment notification",
        )
        winner = int(row["assignee_id"])
        loser = OPERATOR_TWO if winner == OPERATOR_ONE else OPERATOR_ONE

        # Operator role revocation invalidates already-sent buttons.
        await _grant_role(
            feed_two,
            checks,
            ADMIN_ID,
            loser,
            "operator",
            action="revoke",
        )
        _, revoked_screen = await feed_two.callback(loser, "op:list:0")
        checks.text_has(
            revoked_screen,
            "Недостаточно прав",
            "revoked operator is denied by the service",
        )
        checks.ok(
            "VPN" not in (revoked_screen.text or ""),
            "revoked operator denial does not expose request text",
        )

        # Winner opens the queue and closes the request.
        _, winner_queue = await feed_two.message(winner, "/queue")
        checks.ok(
            f"op:card:{request_id}:0"
            in callbacks_in(winner_queue.reply_markup if winner_queue else None),
            "assigned operator sees request",
        )
        _, operator_card = await feed_two.callback(winner, f"op:card:{request_id}:0")
        checks.ok(
            f"op:done:{request_id}:"
            in " ".join(callbacks_in(operator_card.reply_markup if operator_card else None)),
            "operator card offers resolution",
        )
        done_callback = find_callback(operator_card, f"op:done:{request_id}:")
        resolve_marker = len(capture_two.calls)
        await feed_two.callback(winner, done_callback)
        with runtime_two.database.read() as conn:
            after_resolve = conn.execute(
                "SELECT status, version FROM requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        checks.ok(
            after_resolve is not None and str(after_resolve["status"]) == "resolved",
            "operator resolution is committed",
        )
        checks.ok(int(after_resolve["version"]) == 3, "resolution advanced version once")
        checks.ok(
            _scalar(
                runtime_two.database,
                "SELECT COUNT(*) FROM audit_events WHERE action = 'request.resolve' AND object_id = ?",
                (str(request_id),),
            )
            == 1,
            "exactly one resolution audit row",
        )
        resolution_notifications = [
            text
            for text in capture_two.sent_texts_to(CLIENT_ID, resolve_marker)
            if "решена" in text
        ]
        checks.ok(
            len(resolution_notifications) == 1,
            "requester receives one best-effort resolution notification",
        )

        _, stale_screen = await feed_two.callback(winner, done_callback)
        checks.text_has(
            stale_screen,
            "устарел",
            "stale operator button is rejected after resolution",
        )
        checks.ok(
            _scalar(
                runtime_two.database,
                "SELECT COUNT(*) FROM audit_events WHERE action = 'request.resolve'",
            )
            == 1,
            "stale click did not append a second resolution audit",
        )

        # Admin setting: pause and resume new requests with CAS versions.
        _, settings_screen = await feed_two.callback(ADMIN_ID, "adm:settings")
        pause_callback = find_callback(settings_screen, "adm:set:accept:0:")
        _, paused = await feed_two.callback(ADMIN_ID, pause_callback)
        checks.text_has(paused, "выключен", "admin can pause new requests")
        checks.ok(
            _scalar(
                runtime_two.database,
                "SELECT value FROM settings WHERE key = 'accept_new_requests'",
            )
            == "0",
            "pause setting persisted",
        )
        _, paused_attempt = await feed_two.message(CLIENT_ID, "/new")
        checks.text_has(
            paused_attempt,
            "приостановлен",
            "client cannot start a draft while requests are paused",
        )
        resume_callback = find_callback(paused, "adm:set:accept:1:")
        _, resumed = await feed_two.callback(ADMIN_ID, resume_callback)
        checks.text_has(resumed, "включён", "admin can resume new requests")

        # Per-user setting is meaningful and durable.
        _, user_settings = await feed_two.callback(CLIENT_ID, "menu:settings")
        checks.text_has(user_settings, "включены", "notifications default to enabled")
        _, disabled = await feed_two.callback(CLIENT_ID, "usr:notify:0")
        checks.text_has(disabled, "выключены", "client can disable notifications")
        checks.ok(
            _scalar(
                runtime_two.database,
                "SELECT notifications FROM user_settings WHERE telegram_id = ?",
                (CLIENT_ID,),
            )
            == 0,
            "notification preference persisted",
        )

        # Owner revokes admin; an old admin callback is denied server-side.
        await _grant_role(
            feed_two,
            checks,
            OWNER_ID,
            ADMIN_ID,
            "admin",
            action="revoke",
        )
        _, revoked_admin = await feed_two.callback(ADMIN_ID, "adm:queue:0")
        checks.text_has(
            revoked_admin,
            "Недостаточно прав",
            "revoked admin cannot use an old management callback",
        )

        for call in capture.calls + capture_two.calls:
            all_callbacks.extend(callbacks_in(call.reply_markup))
        checks.ok(
            all(
                len(data.encode("utf-8")) <= 64
                for data in all_callbacks
            ),
            "every generated callback is at most 64 UTF-8 bytes",
        )
        checks.ok(capture.closed, "first fake transport is closed on restart")
        checks.ok(capture_two.closed is False, "second fake transport remains active until app close")
        await runtime_two.bot.session.close()
        checks.ok(capture_two.closed, "fake transport closes with the application")

        return SmokeReport(tuple(checks.items))
    except Exception as exc:  # noqa: BLE001 - report every smoke failure as JSON
        return SmokeReport(
            tuple(checks.items),
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        for runtime in runtimes:
            await runtime.bot.session.close()
            await runtime.dispatcher.storage.close()
        if temporary is not None:
            with suppress(OSError):
                temporary.cleanup()
