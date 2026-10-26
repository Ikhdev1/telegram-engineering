"""aiogram router: thin transport layer over SupportService."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, Update

from . import callbacks, ui
from .callbacks import CallbackError
from .models import ROLE_ACTIONS, ROLE_NAMES, SupportRequest
from .service import DomainError, SupportService
from .util import redact_secrets

logger = logging.getLogger(__name__)

GROUP_NOTICE = (
    "Бот работает в личном чате. Откройте его напрямую и отправьте /start: "
    "карточки заявок и управление не публикуются в группах."
)


def _private(message: Message) -> bool:
    return message.chat.type == "private"


def _username(user) -> str | None:
    return getattr(user, "username", None)


def _log_delivery_failure(method: str, exc: TelegramAPIError) -> None:
    """Explicit best-effort policy for outgoing screens.

    A failed screen is not a reason to retry a committed business operation or
    to stop polling.  Flood control is logged with retry_after and deliberately
    not retried here; the next user action can request the screen again.
    """

    if isinstance(exc, TelegramRetryAfter):
        logger.warning(
            "Screen delivery %s skipped: flood control retry_after=%ss (not retried)",
            method,
            exc.retry_after,
        )
    elif isinstance(exc, TelegramForbiddenError):
        logger.warning(
            "Screen delivery %s skipped: forbidden: %s",
            method,
            redact_secrets(exc),
        )
    elif isinstance(exc, TelegramNetworkError):
        logger.warning(
            "Screen delivery %s skipped: network: %s",
            method,
            redact_secrets(exc),
        )
    else:
        logger.warning(
            "Screen delivery %s skipped: %s: %s",
            method,
            type(exc).__name__,
            redact_secrets(exc),
        )


async def _send(message: Message, screen: ui.Screen) -> None:
    try:
        await message.answer(
            screen.text,
            reply_markup=screen.keyboard,
            parse_mode="HTML",
        )
    except TelegramAPIError as exc:
        _log_delivery_failure("SendMessage", exc)


async def _send_plain(message: Message, text: str) -> None:
    try:
        await message.answer(text, parse_mode="HTML")
    except TelegramAPIError as exc:
        _log_delivery_failure("SendMessage", exc)


async def _answer(
    query: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    """Acknowledge once per delivery, tolerating a retry after a lost ack.

    A retried update can contain a callback query that Telegram already
    answered.  A repeated ``answerCallbackQuery`` failure must not prevent the
    idempotent database replay from finishing.
    """

    try:
        if text is None:
            await query.answer()
        else:
            await query.answer(text, show_alert=show_alert)
    except TelegramAPIError as exc:
        logger.debug("answerCallbackQuery failed: %s", redact_secrets(exc))


async def _render(query: CallbackQuery, screen: ui.Screen) -> None:
    message = query.message
    if not isinstance(message, Message):
        bot = query.bot
        if bot is None:
            return
        try:
            await bot.send_message(
                query.from_user.id,
                screen.text,
                reply_markup=screen.keyboard,
                parse_mode="HTML",
            )
        except TelegramAPIError as exc:
            _log_delivery_failure("SendMessage", exc)
        return
    try:
        await message.edit_text(
            screen.text,
            reply_markup=screen.keyboard,
            parse_mode="HTML",
        )
        return
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        _log_delivery_failure("EditMessageText", exc)
    except TelegramAPIError as exc:
        _log_delivery_failure("EditMessageText", exc)

    try:
        await message.answer(
            screen.text,
            reply_markup=screen.keyboard,
            parse_mode="HTML",
        )
    except TelegramAPIError as exc:
        _log_delivery_failure("SendMessage", exc)


async def _main(service: SupportService, telegram_id: int, notice: str | None = None) -> ui.Screen:
    actor = await asyncio.to_thread(service.actor_state, telegram_id)
    return ui.main_menu(actor, notice=notice)


async def _notify_assignment(bot: Bot, request: SupportRequest) -> None:
    text = (
        f"<b>Заявка #{request.id} назначена оператору.</b>\n"
        "Следить за статусом можно в разделе «Мои заявки»."
    )
    try:
        await bot.send_message(request.requester_id, text, parse_mode="HTML")
    except TelegramAPIError as exc:
        # Best effort only.  The database is the source of truth; Telegram
        # has no idempotency key that would make an outbox worker exactly-once.
        logger.warning(
            "Assignment notification for request %s failed: %s",
            request.id,
            redact_secrets(exc),
        )


async def _notify_resolved(bot: Bot, request: SupportRequest) -> None:
    text = (
        f"<b>Заявка #{request.id} решена.</b>\n"
        "Актуальный статус всегда доступен в «Мои заявки»."
    )
    try:
        await bot.send_message(request.requester_id, text, parse_mode="HTML")
    except TelegramAPIError as exc:
        logger.warning(
            "Resolution notification for request %s failed: %s",
            request.id,
            redact_secrets(exc),
        )


def build_router(service: SupportService) -> Router:
    router = Router(name="support")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not _private(message) or message.from_user is None:
            await _send_plain(message, GROUP_NOTICE)
            return
        user_id = message.from_user.id
        await asyncio.to_thread(service.touch_user, user_id, _username(message.from_user))
        await _send(message, await _main(service, user_id))

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        if not _private(message) or message.from_user is None:
            await _send_plain(message, GROUP_NOTICE)
            return
        user_id = message.from_user.id
        await asyncio.to_thread(service.touch_user, user_id, _username(message.from_user))
        await _send(message, await _main(service, user_id))

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        if not _private(message):
            await _send_plain(message, GROUP_NOTICE)
            return
        await _send(message, ui.help_screen())

    @router.message(Command("cancel"))
    async def cancel(
        message: Message, event_update: Update, bot: Bot
    ) -> None:
        if not _private(message) or message.from_user is None:
            await _send_plain(message, GROUP_NOTICE)
            return
        user_id = message.from_user.id
        await asyncio.to_thread(service.touch_user, user_id, _username(message.from_user))
        await asyncio.to_thread(
            service.cancel_inputs,
            user_id,
            bot.id,
            event_update.update_id,
        )
        await _send(
            message,
            await _main(service, user_id, notice="Ввод и ожидающие подтверждения отменены."),
        )

    @router.message(Command("new"))
    async def new_request(
        message: Message, event_update: Update, bot: Bot
    ) -> None:
        if not _private(message) or message.from_user is None:
            await _send_plain(message, GROUP_NOTICE)
            return
        user_id = message.from_user.id
        await asyncio.to_thread(service.touch_user, user_id, _username(message.from_user))
        try:
            result = await asyncio.to_thread(
                service.start_draft, user_id, bot.id, event_update.update_id
            )
        except DomainError as exc:
            await _send(
                message,
                await _main(service, user_id, notice=exc.message),
            )
            return
        if result.draft is None:
            await _send(message, await _main(service, user_id))
            return
        await _send(message, ui.draft_screen(result.draft))

    @router.message(Command("my"))
    async def my_requests(message: Message) -> None:
        if not _private(message) or message.from_user is None:
            await _send_plain(message, GROUP_NOTICE)
            return
        user_id = message.from_user.id
        await asyncio.to_thread(service.touch_user, user_id, _username(message.from_user))
        page = await asyncio.to_thread(service.list_own_requests, user_id, 0)
        actor = await asyncio.to_thread(service.actor_state, user_id)
        await _send(message, ui.own_list(actor, page))

    @router.message(Command("settings"))
    async def settings_command(message: Message) -> None:
        if not _private(message) or message.from_user is None:
            await _send_plain(message, GROUP_NOTICE)
            return
        user_id = message.from_user.id
        actor = await asyncio.to_thread(service.actor_state, user_id)
        await _send(message, ui.user_settings(actor))

    @router.message(Command("queue"))
    async def queue(message: Message) -> None:
        if not _private(message) or message.from_user is None:
            await _send_plain(message, GROUP_NOTICE)
            return
        user_id = message.from_user.id
        actor = await asyncio.to_thread(service.actor_state, user_id)
        try:
            if actor.is_admin:
                page = await asyncio.to_thread(service.list_admin_queue, user_id, 0)
                await _send(message, ui.admin_queue(page))
                return
            if actor.is_operator:
                page = await asyncio.to_thread(service.list_operator_queue, user_id, 0)
                await _send(message, ui.operator_queue(page))
                return
        except DomainError as exc:
            await _send(message, ui.error_screen(exc.message, ("menu", "main")))
            return
        await _send(
            message,
            await _main(service, user_id, notice="Раздел очереди доступен только сотрудникам."),
        )

    @router.message(F.text)
    async def text_input(
        message: Message, event_update: Update, bot: Bot
    ) -> None:
        if not _private(message) or message.from_user is None:
            await _send_plain(message, GROUP_NOTICE)
            return
        user_id = message.from_user.id
        text = message.text or ""
        await asyncio.to_thread(service.touch_user, user_id, _username(message.from_user))

        pending = await asyncio.to_thread(service.get_pending_role, user_id)
        if pending is not None:
            if pending.stage == "confirm":
                await _send(
                    message,
                    ui.role_confirmation(
                        pending,
                        notice="Сначала подтвердите или отмените действие кнопкой.",
                    ),
                )
                return
            try:
                result = await asyncio.to_thread(
                    service.submit_role_target,
                    user_id,
                    text,
                    private_chat=_private(message),
                    bot_id=bot.id,
                    update_id=event_update.update_id,
                )
            except DomainError as exc:
                current = await asyncio.to_thread(service.get_pending_role, user_id)
                if current is not None and current.stage == "confirm":
                    screen = ui.role_confirmation(current, notice=exc.message)
                elif current is not None:
                    screen = ui.make_role_prompt(
                        current.action, current.role, notice=exc.message
                    )
                else:
                    screen = ui.error_screen(exc.message, ("adm", "roles"), "К ролям")
                await _send(message, screen)
                return
            if result.pending is None:
                await _send(
                    message,
                    ui.error_screen(
                        "Ожидающее действие больше не актуально.",
                        ("adm", "roles"),
                        "К ролям",
                    ),
                )
            elif result.pending.stage == "confirm":
                await _send(message, ui.role_confirmation(result.pending))
            else:
                await _send(
                    message, ui.make_role_prompt(result.pending.action, result.pending.role)
                )
            return

        draft = await asyncio.to_thread(service.get_draft, user_id)
        if draft is None:
            if text.startswith("/"):
                notice = "Неизвестная команда. Используйте /menu, /new, /my или /cancel."
            else:
                notice = "Нет активного ввода. Начните с кнопки «Создать заявку»."
            await _send(
                message,
                await _main(service, user_id, notice=notice),
            )
            return

        if draft.step == "preview":
            await _send(
                message,
                ui.draft_screen(
                    draft,
                    notice="Проверьте заявку и используйте кнопки под предпросмотром.",
                ),
            )
            return

        try:
            if draft.step == "subject":
                result = await asyncio.to_thread(
                    service.save_draft_subject,
                    user_id,
                    text,
                    bot.id,
                    event_update.update_id,
                )
            else:
                result = await asyncio.to_thread(
                    service.save_draft_details,
                    user_id,
                    text,
                    bot.id,
                    event_update.update_id,
                )
        except DomainError as exc:
            current = await asyncio.to_thread(service.get_draft, user_id)
            if current is None:
                await _send(message, await _main(service, user_id, notice=exc.message))
            else:
                await _send(message, ui.draft_screen(current, notice=exc.message))
            return
        if result.draft is None:
            await _send(message, await _main(service, user_id))
            return
        await _send(message, ui.draft_screen(result.draft))

    @router.message()
    async def unsupported_message(message: Message) -> None:
        if not _private(message):
            await _send_plain(message, GROUP_NOTICE)
            return
        if message.from_user is None:
            return
        user_id = message.from_user.id
        await _send(
            message,
            await _main(
                service,
                user_id,
                notice="Поддерживается только текст. Создание заявки принимает тему и описание сообщением.",
            ),
        )

    @router.callback_query()
    async def on_callback(
        query: CallbackQuery, event_update: Update, bot: Bot
    ) -> None:
        message = query.message
        if not isinstance(message, Message):
            await _answer(
                query,
                "Сообщение недоступно. Откройте /menu.",
                show_alert=True,
            )
            return
        if message.chat.type != "private":
            await _answer(
                query,
                "Данные и управление доступны только в личном чате.",
                show_alert=True,
            )
            return
        await _answer(query)
        private_chat = message.chat.type == "private"

        try:
            parts = callbacks.parse(query.data or "")
        except CallbackError:
            await _render(
                query,
                ui.error_screen(
                    "Кнопка повреждена или устарела. Откройте меню заново.",
                    ("menu", "main"),
                ),
            )
            return

        user_id = query.from_user.id
        update_id = event_update.update_id

        try:
            if parts[0] == "menu":
                await _handle_menu(query, parts, service, user_id, bot, update_id)
            elif parts[0] == "usr":
                await _handle_user_settings(
                    query, parts, service, user_id, bot, update_id
                )
            elif parts[0] == "draft":
                await _handle_draft(query, parts, service, user_id, bot, update_id)
            elif parts[0] == "mine":
                await _handle_mine(query, parts, service, user_id)
            elif parts[0] == "req":
                await _handle_request(query, parts, service, user_id, bot, update_id)
            elif parts[0] == "op":
                await _handle_operator(
                    query, parts, service, user_id, bot, update_id, private_chat
                )
            elif parts[0] == "adm":
                await _handle_admin(
                    query, parts, service, user_id, bot, update_id, private_chat
                )
            elif parts[0] == "role":
                await _handle_role(
                    query, parts, service, user_id, bot, update_id, private_chat
                )
            else:
                await _render(
                    query,
                    ui.error_screen(
                        "Неизвестная кнопка. Откройте меню заново.",
                        ("menu", "main"),
                    ),
                )
        except DomainError as exc:
            await _render(
                query,
                ui.error_screen(exc.message, ("menu", "main")),
            )
        except CallbackError:
            await _render(
                query,
                ui.error_screen(
                    "Кнопка устарела или содержит неверные данные.",
                    ("menu", "main"),
                ),
            )

    return router


async def _handle_menu(
    query: CallbackQuery,
    parts: tuple[str, ...],
    service: SupportService,
    user_id: int,
    bot: Bot,
    update_id: int,
) -> None:
    if len(parts) != 2:
        raise CallbackError("Malformed menu callback.")
    action = parts[1]
    if action == "main":
        await _render(query, await _main(service, user_id))
        return
    if action == "help":
        await _render(query, ui.help_screen())
        return
    if action == "settings":
        actor = await asyncio.to_thread(service.actor_state, user_id)
        await _render(query, ui.user_settings(actor))
        return
    if action == "mine":
        actor = await asyncio.to_thread(service.actor_state, user_id)
        page = await asyncio.to_thread(service.list_own_requests, user_id, 0)
        await _render(query, ui.own_list(actor, page))
        return
    if action == "new":
        result = await asyncio.to_thread(
            service.start_draft,
            user_id,
            bot.id,
            update_id,
        )
        if result.draft is None:
            await _render(query, await _main(service, user_id))
        else:
            await _render(query, ui.draft_screen(result.draft))
        return
    if action == "manage":
        actor = await asyncio.to_thread(service.actor_state, user_id)
        if not actor.is_admin:
            raise DomainError(
                "forbidden",
                "Недостаточно прав: доступ мог быть отозван.",
            )
        await _render(query, ui.admin_panel(actor))
        return
    raise CallbackError("Unknown menu action.")


async def _handle_user_settings(
    query: CallbackQuery,
    parts: tuple[str, ...],
    service: SupportService,
    user_id: int,
    bot: Bot,
    update_id: int,
) -> None:
    if len(parts) != 3 or parts[1] != "notify":
        raise CallbackError("Malformed user settings callback.")
    enabled = callbacks.parse_uint(parts[2], maximum=1) == 1
    result = await asyncio.to_thread(
        service.set_notifications,
        user_id,
        enabled,
        bot_id=bot.id,
        update_id=update_id,
    )
    actor = await asyncio.to_thread(service.actor_state, user_id)
    notice = "Настройка уже была применена." if result.duplicate else "Настройка сохранена."
    await _render(query, ui.user_settings(actor, notice=notice))


async def _handle_draft(
    query: CallbackQuery,
    parts: tuple[str, ...],
    service: SupportService,
    user_id: int,
    bot: Bot,
    update_id: int,
) -> None:
    if len(parts) < 2:
        raise CallbackError("Malformed draft callback.")
    action = parts[1]
    if action == "back":
        result = await asyncio.to_thread(
            service.draft_back, user_id, bot.id, update_id
        )
        if result.draft is None:
            raise DomainError(
                "draft_missing",
                "Черновик не найден. Начните создание заявки заново.",
            )
        await _render(query, ui.draft_screen(result.draft))
        return
    if action == "cancel":
        await asyncio.to_thread(service.cancel_draft, user_id, bot.id, update_id)
        await _render(
            query,
            await _main(service, user_id, notice="Создание заявки отменено."),
        )
        return
    if action == "ok":
        if len(parts) != 3:
            raise CallbackError("Malformed draft confirmation.")
        result = await asyncio.to_thread(
            service.confirm_draft, user_id, parts[2], bot.id, update_id
        )
        if result.request is None:
            raise DomainError(
                "not_found",
                "Заявка не найдена. Откройте «Мои заявки».",
            )
        notice = (
            "Это подтверждение уже было обработано."
            if result.duplicate
            else "Заявка создана."
        )
        await _render(query, ui.own_card(result.request, 0, notice=notice))
        return
    raise CallbackError("Unknown draft action.")


async def _handle_mine(
    query: CallbackQuery,
    parts: tuple[str, ...],
    service: SupportService,
    user_id: int,
) -> None:
    if len(parts) != 2:
        raise CallbackError("Malformed own-list callback.")
    page_number = callbacks.parse_uint(parts[1], maximum=10_000)
    actor = await asyncio.to_thread(service.actor_state, user_id)
    page = await asyncio.to_thread(service.list_own_requests, user_id, page_number)
    await _render(query, ui.own_list(actor, page))


async def _handle_request(
    query: CallbackQuery,
    parts: tuple[str, ...],
    service: SupportService,
    user_id: int,
    bot: Bot,
    update_id: int,
) -> None:
    if len(parts) < 2:
        raise CallbackError("Malformed request callback.")
    action = parts[1]
    if action == "cancel" and len(parts) == 4:
        request_id = callbacks.parse_id(parts[2])
        page_number = callbacks.parse_uint(parts[3], maximum=10_000)
        request = await asyncio.to_thread(service.get_own_request, user_id, request_id)
        await _render(query, ui.cancel_confirmation(request, page_number))
        return
    if action == "cancelok" and len(parts) == 5:
        request_id = callbacks.parse_id(parts[2])
        version = callbacks.parse_uint(parts[3], maximum=2**31)
        page_number = callbacks.parse_uint(parts[4], maximum=10_000)
        result = await asyncio.to_thread(
            service.cancel_request,
            user_id,
            request_id,
            version,
            bot.id,
            update_id,
        )
        if result.request is None:
            raise DomainError(
                "not_found",
                "Заявка не найдена. Откройте «Мои заявки».",
            )
        notice = (
            "Это действие уже было обработано."
            if result.duplicate
            else "Заявка отменена."
        )
        await _render(query, ui.own_card(result.request, page_number, notice=notice))
        return
    if len(parts) != 3:
        raise CallbackError("Malformed request card.")
    request_id = callbacks.parse_id(parts[1])
    page_number = callbacks.parse_uint(parts[2], maximum=10_000)
    request = await asyncio.to_thread(service.get_own_request, user_id, request_id)
    await _render(query, ui.own_card(request, page_number))


async def _handle_operator(
    query: CallbackQuery,
    parts: tuple[str, ...],
    service: SupportService,
    user_id: int,
    bot: Bot,
    update_id: int,
    private_chat: bool,
) -> None:
    if len(parts) < 2:
        raise CallbackError("Malformed operator callback.")
    action = parts[1]
    if action == "list" and len(parts) == 3:
        page_number = callbacks.parse_uint(parts[2], maximum=10_000)
        page = await asyncio.to_thread(service.list_operator_queue, user_id, page_number)
        await _render(query, ui.operator_queue(page))
        return
    if action == "card" and len(parts) == 4:
        request_id = callbacks.parse_id(parts[2])
        page_number = callbacks.parse_uint(parts[3], maximum=10_000)
        request = await asyncio.to_thread(service.get_operator_request, user_id, request_id)
        await _render(query, ui.operator_card(request, page_number))
        return
    if action == "done" and len(parts) == 5:
        request_id = callbacks.parse_id(parts[2])
        version = callbacks.parse_uint(parts[3], maximum=2**31)
        page_number = callbacks.parse_uint(parts[4], maximum=10_000)
        result = await asyncio.to_thread(
            service.resolve_request,
            user_id,
            request_id,
            version,
            private_chat=private_chat,
            bot_id=bot.id,
            update_id=update_id,
        )
        if result.request is None:
            raise DomainError(
                "not_found",
                "Заявка не найдена. Откройте очередь заново.",
            )
        if result.notify_requester and not result.duplicate:
            await _notify_resolved(bot, result.request)
        notice = (
            "Этот результат уже был обработан ранее."
            if result.duplicate
            else None
        )
        await _render(
            query,
            ui.operator_resolved(result.request, notice=notice),
        )
        return
    raise CallbackError("Unknown operator action.")


async def _handle_admin(
    query: CallbackQuery,
    parts: tuple[str, ...],
    service: SupportService,
    user_id: int,
    bot: Bot,
    update_id: int,
    private_chat: bool,
) -> None:
    if len(parts) < 2:
        raise CallbackError("Malformed admin callback.")
    action = parts[1]
    if action == "panel" and len(parts) == 2:
        actor = await asyncio.to_thread(service.actor_state, user_id)
        if not actor.is_admin:
            raise DomainError(
                "forbidden",
                "Недостаточно прав: доступ мог быть отозван.",
            )
        await _render(query, ui.admin_panel(actor))
        return
    if action == "queue" and len(parts) == 3:
        page_number = callbacks.parse_uint(parts[2], maximum=10_000)
        page = await asyncio.to_thread(service.list_admin_queue, user_id, page_number)
        await _render(query, ui.admin_queue(page))
        return
    if action == "req" and len(parts) == 4:
        request_id = callbacks.parse_id(parts[2])
        page_number = callbacks.parse_uint(parts[3], maximum=10_000)
        request = await asyncio.to_thread(service.get_admin_request, user_id, request_id)
        await _render(query, ui.admin_card(request, page_number))
        return
    if action == "assign" and len(parts) == 4:
        request_id = callbacks.parse_id(parts[2])
        page_number = callbacks.parse_uint(parts[3], maximum=10_000)
        request = await asyncio.to_thread(service.get_admin_request, user_id, request_id)
        operators = await asyncio.to_thread(service.list_operators, user_id)
        await _render(query, ui.assign_picker(request, operators, page_number))
        return
    if action == "pick" and len(parts) == 6:
        request_id = callbacks.parse_id(parts[2])
        operator_id = callbacks.parse_id(parts[3])
        version = callbacks.parse_uint(parts[4], maximum=2**31)
        page_number = callbacks.parse_uint(parts[5], maximum=10_000)
        result = await asyncio.to_thread(
            service.assign_request,
            user_id,
            request_id,
            operator_id,
            version,
            private_chat=private_chat,
            bot_id=bot.id,
            update_id=update_id,
        )
        if result.request is None:
            raise DomainError(
                "not_found",
                "Заявка не найдена. Откройте очередь заново.",
            )
        if result.notify_requester and not result.duplicate:
            await _notify_assignment(bot, result.request)
        notice = (
            "Это назначение уже было обработано ранее."
            if result.duplicate
            else "Оператор назначен."
        )
        await _render(
            query,
            ui.admin_card(result.request, page_number, notice=notice),
        )
        return
    if action == "roles" and len(parts) == 2:
        actor = await asyncio.to_thread(service.actor_state, user_id)
        entries = await asyncio.to_thread(service.roles_overview, user_id)
        await _render(query, ui.roles_screen(entries, actor))
        return
    if action == "role" and len(parts) == 4:
        role_action = callbacks.parse_choice(parts[2], ROLE_ACTIONS)
        role = callbacks.parse_choice(parts[3], ROLE_NAMES)
        result = await asyncio.to_thread(
            service.begin_role_change,
            user_id,
            role_action,
            role,
            private_chat=private_chat,
            bot_id=bot.id,
            update_id=update_id,
        )
        if result.pending is not None and result.pending.stage == "confirm":
            await _render(query, ui.role_confirmation(result.pending))
        else:
            await _render(query, ui.make_role_prompt(role_action, role))
        return
    if action == "settings" and len(parts) == 2:
        accept, version = await asyncio.to_thread(service.get_admin_settings, user_id)
        await _render(query, ui.admin_settings(accept, version))
        return
    if action == "set" and len(parts) == 5 and parts[2] == "accept":
        enabled = callbacks.parse_uint(parts[3], maximum=1) == 1
        version = callbacks.parse_uint(parts[4], maximum=2**31)
        try:
            result = await asyncio.to_thread(
                service.set_accept_new_requests,
                user_id,
                enabled,
                version,
                private_chat=private_chat,
                bot_id=bot.id,
                update_id=update_id,
            )
        except DomainError as exc:
            current_value, current_version = await asyncio.to_thread(
                service.get_admin_settings, user_id
            )
            await _render(
                query,
                ui.admin_settings(
                    current_value,
                    current_version,
                    notice=exc.message,
                ),
            )
            return
        notice = (
            "Настройка уже была применена."
            if result.duplicate
            else "Настройка сохранена."
        )
        await _render(
            query,
            ui.admin_settings(result.value, result.version, notice=notice),
        )
        return
    if action == "audit" and len(parts) == 3:
        page_number = callbacks.parse_uint(parts[2], maximum=10_000)
        page = await asyncio.to_thread(service.audit_page, user_id, page_number)
        await _render(query, ui.audit_screen(page))
        return
    raise CallbackError("Unknown admin action.")


async def _handle_role(
    query: CallbackQuery,
    parts: tuple[str, ...],
    service: SupportService,
    user_id: int,
    bot: Bot,
    update_id: int,
    private_chat: bool,
) -> None:
    if len(parts) < 2:
        raise CallbackError("Malformed role callback.")
    if parts[1] == "cancel" and len(parts) == 2:
        await asyncio.to_thread(
            service.cancel_role_change,
            user_id,
            private_chat=private_chat,
            bot_id=bot.id,
            update_id=update_id,
        )
        actor = await asyncio.to_thread(service.actor_state, user_id)
        entries = await asyncio.to_thread(service.roles_overview, user_id)
        await _render(
            query,
            ui.roles_screen(entries, actor, notice="Действие с ролью отменено."),
        )
        return
    if parts[1] == "ok" and len(parts) == 3:
        result = await asyncio.to_thread(
            service.confirm_role_change,
            user_id,
            parts[2],
            private_chat=private_chat,
            bot_id=bot.id,
            update_id=update_id,
        )
        actor = await asyncio.to_thread(service.actor_state, user_id)
        entries = await asyncio.to_thread(service.roles_overview, user_id)
        notice = (
            "Это изменение роли уже было обработано."
            if result.duplicate
            else "Роль изменена."
        )
        await _render(query, ui.roles_screen(entries, actor, notice=notice))
        return
    raise CallbackError("Unknown role action.")
