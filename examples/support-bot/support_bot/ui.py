"""Russian HTML screens and inline keyboards.

Every dynamic value is escaped here.  Callbacks are built only through
``callbacks.encode`` so the 64-byte limit cannot be forgotten at a call site.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .callbacks import encode
from .models import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    STATUS_ASSIGNED,
    STATUS_CANCELLED,
    STATUS_NEW,
    STATUS_RESOLVED,
    ActorState,
    AuditEntry,
    Draft,
    Page,
    PendingRole,
    RoleEntry,
    SupportRequest,
)
from .util import display_utc

STATUS_LABELS = {
    STATUS_NEW: "новая",
    STATUS_ASSIGNED: "назначена",
    STATUS_RESOLVED: "решена",
    STATUS_CANCELLED: "отменена",
}

ROLE_LABELS = {
    ROLE_ADMIN: "администратор",
    ROLE_OPERATOR: "оператор",
}

TELEGRAM_TEXT_LIMIT = 4096
# Local conservative budget, not a claim about Telegram's exact counting of
# astral characters.  Telegram entities definitely use UTF-16 offsets and the
# sendMessage limit is documented in characters; counting UTF-16 units keeps a
# safety margin for emoji without shrinking the accepted 2000 '<'/'&'/'>' case.
# A cut caused only by this local budget is marked explicitly in the screen.
SCREEN_VISIBLE_LIMIT = TELEGRAM_TEXT_LIMIT
TRUNCATION_MARKER = "\n… [текст сокращён]"
_TRUNCATION_UNITS = len(TRUNCATION_MARKER.encode("utf-16-le")) // 2

_TAG_RE = re.compile(r"<[^>]*>")
_TAG_NAME_RE = re.compile(r"</?\s*([A-Za-z][A-Za-z0-9]*)")
_TOKEN_RE = re.compile(
    r"<[^>]*>|&(?:#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);"
)


def _utf16_units(value: str) -> int:
    # Conservative local accounting for astral characters and entity offsets.
    # The rendered source is still checked after entity parsing; this is not a
    # claim that Telegram rejects a valid 2000-emoji payload by contract.
    return len(value.encode("utf-16-le")) // 2


def visible_text(value: str) -> str:
    """Return text as Telegram sees it after entity parsing."""

    return html.unescape(_TAG_RE.sub("", value))


def visible_utf16_length(value: str) -> int:
    return _utf16_units(visible_text(value))


def _fit_html(text: str, limit: int = SCREEN_VISIBLE_LIMIT) -> str:
    """Bound visible text without cutting an HTML tag or entity in half.

    The raw source is intentionally allowed to be larger than 4096 because
    escaped '<', '>', '&' expand to multiple source characters while counting
    as one visible character to Telegram.  Truncation is only a last-resort
    guard for legacy/manual data beyond the validated input limits; it closes
    open tags and inserts an explicit local truncation marker.
    """

    if visible_utf16_length(text) <= limit:
        return text

    output: list[str] = []
    used = 0
    open_tags: list[str] = []
    position = 0
    truncated = False

    def append_chunk(chunk: str) -> bool:
        nonlocal used
        for character in chunk:
            units = _utf16_units(character)
            if used + units > limit - _TRUNCATION_UNITS:
                output.append(TRUNCATION_MARKER)
                return False
            output.append(character)
            used += units
        return True

    for match in _TOKEN_RE.finditer(text):
        if not append_chunk(text[position : match.start()]):
            truncated = True
            break
        token = match.group(0)
        if token.startswith("<"):
            output.append(token)
            name_match = _TAG_NAME_RE.match(token)
            if name_match:
                name = name_match.group(1).lower()
                if token.startswith("</"):
                    for index in range(len(open_tags) - 1, -1, -1):
                        if open_tags[index] == name:
                            del open_tags[index]
                            break
                elif not token.endswith("/>"):
                    open_tags.append(name)
        else:
            if used + 1 > limit - _TRUNCATION_UNITS:
                output.append(TRUNCATION_MARKER)
                truncated = True
                break
            output.append(token)
            used += 1
        position = match.end()

    if not truncated:
        append_chunk(text[position:])

    for name in reversed(open_tags):
        output.append(f"</{name}>")
    return "".join(output)

ACTION_LABELS = {
    "grant": "выдать",
    "revoke": "снять",
}


@dataclass(frozen=True)
class Screen:
    text: str
    keyboard: InlineKeyboardMarkup | None = None


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def _short(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _button(label: str, *parts: object) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=encode(*parts))


def _screen(text: str, rows: list[list[InlineKeyboardButton]]) -> Screen:
    return Screen(
        text=_fit_html(text),
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


def _status(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def _user(telegram_id: int, username: str | None) -> str:
    rendered = f"<code>{telegram_id}</code>"
    if username:
        rendered += f" @{esc(username)}"
    return rendered


def _notice(text: str, notice: str | None) -> str:
    if not notice:
        return text
    return f"<b>Внимание.</b> {esc(notice)}\n\n{text}"


def main_menu(actor: ActorState, notice: str | None = None) -> Screen:
    lines = ["<b>Поддержка</b>", "Выберите раздел."]
    if not actor.accept_new_requests:
        lines.append("Приём новых заявок сейчас приостановлен.")
    rows: list[list[InlineKeyboardButton]] = []
    if actor.accept_new_requests:
        rows.append([_button("Создать заявку", "menu", "new")])
    rows.append(
        [
            _button("Мои заявки", "menu", "mine"),
            _button("Настройки", "menu", "settings"),
        ]
    )
    if actor.is_operator:
        rows.append([_button("Моя очередь", "op", "list", 0)])
    if actor.is_admin:
        rows.append([_button("Управление", "menu", "manage")])
    rows.append([_button("Помощь", "menu", "help")])
    return _screen(_notice("\n".join(lines), notice), rows)


def help_screen() -> Screen:
    text = (
        "<b>Как это работает</b>\n"
        "Заявка создаётся в личном чате за три шага: тема, описание, "
        "предпросмотр. После подтверждения её видит администратор очереди "
        "и назначенный оператор.\n\n"
        "«Мои заявки» показывает только ваши обращения и позволяет отменить "
        "ещё открытую заявку. Смена статуса отражается в карточке.\n\n"
        "Кнопки в старых сообщениях могут устареть: бот каждый раз проверяет "
        "роль и версию заявки заново. Открывайте раздел повторно."
    )
    return _screen(text, [[_button("Главное меню", "menu", "main")]])


def draft_screen(draft: Draft, notice: str | None = None) -> Screen:
    if draft.step == "subject":
        text = (
            "<b>Новая заявка · шаг 1 из 3</b>\n"
            "Отправьте тему одним сообщением (3–120 символов)."
        )
        if draft.subject:
            text += f"\n\nСохранённая тема: <b>{esc(draft.subject)}</b>"
        rows = [[_button("Отменить", "draft", "cancel")]]
        return _screen(_notice(text, notice), rows)
    if draft.step == "details":
        text = (
            "<b>Новая заявка · шаг 2 из 3</b>\n"
            f"Тема: <b>{esc(draft.subject)}</b>\n"
            "Отправьте описание одним сообщением (10–2000 символов)."
        )
        if draft.details:
            text += f"\n\nСохранённое описание:\n{esc(draft.details)}"
        rows = [
            [_button("Назад", "draft", "back")],
            [_button("Отменить", "draft", "cancel")],
        ]
        return _screen(_notice(text, notice), rows)
    if draft.step == "preview":
        if not draft.token:
            raise ValueError("Preview draft must have a confirmation token.")
        text = (
            "<b>Новая заявка · шаг 3 из 3</b>\n"
            f"<b>Тема:</b> {esc(draft.subject)}\n"
            f"<b>Описание:</b>\n{esc(draft.details)}\n\n"
            "После подтверждения заявка попадёт в очередь. "
            "Подтверждение одноразовое."
        )
        rows = [
            [_button("Подтвердить", "draft", "ok", draft.token)],
            [_button("Назад", "draft", "back")],
            [_button("Отменить", "draft", "cancel")],
        ]
        return _screen(_notice(text, notice), rows)
    raise ValueError(f"Unknown draft step: {draft.step}")


def own_list(actor: ActorState, page: Page[SupportRequest]) -> Screen:
    if not page.items:
        text = (
            "<b>Мои заявки</b>\n"
            "Заявок пока нет."
        )
        rows: list[list[InlineKeyboardButton]] = []
        if actor.accept_new_requests:
            rows.append([_button("Создать заявку", "menu", "new")])
        rows.append([_button("Главное меню", "menu", "main")])
        return _screen(text, rows)
    lines = [
        f"<b>Мои заявки</b> · страница {page.page + 1} из {page.pages}",
        f"Всего: {page.total}",
    ]
    rows = []
    for request in page.items:
        rows.append(
            [
                _button(
                    f"#{request.id} · {_status(request.status)} · "
                    f"{_short(request.subject, 36)}",
                    "req",
                    request.id,
                    page.page,
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page.page > 0:
        navigation.append(_button("Назад", "mine", page.page - 1))
    if page.page + 1 < page.pages:
        navigation.append(_button("Далее", "mine", page.page + 1))
    if navigation:
        rows.append(navigation)
    rows.append([_button("Главное меню", "menu", "main")])
    return _screen("\n".join(lines), rows)


def own_card(
    request: SupportRequest, page: int, notice: str | None = None
) -> Screen:
    lines = [
        f"<b>Заявка #{request.id}</b>",
        f"Статус: <b>{_status(request.status)}</b>",
        f"Создана: {esc(display_utc(request.created_at))}",
    ]
    if request.assignee_id is not None:
        lines.append(f"Оператор: <code>{request.assignee_id}</code>")
    lines.extend(
        [
            "",
            f"<b>Тема:</b> {esc(request.subject)}",
            f"<b>Описание:</b>\n{esc(request.details)}",
        ]
    )
    rows: list[list[InlineKeyboardButton]] = []
    if request.status in (STATUS_NEW, STATUS_ASSIGNED):
        rows.append(
            [_button("Отменить заявку", "req", "cancel", request.id, page)]
        )
    rows.append([_button("К списку", "mine", page)])
    return _screen(_notice("\n".join(lines), notice), rows)


def cancel_confirmation(
    request: SupportRequest, page: int, notice: str | None = None
) -> Screen:
    text = (
        f"<b>Отменить заявку #{request.id}?</b>\n"
        "Заявка получит статус «отменена», история останется в базе. "
        "Это действие нельзя отменить кнопкой."
    )
    rows = [
        [
            _button(
                "Да, отменить",
                "req",
                "cancelok",
                request.id,
                request.version,
                page,
            )
        ],
        [_button("Оставить заявку", "req", request.id, page)],
    ]
    return _screen(_notice(text, notice), rows)


def operator_queue(page: Page[SupportRequest]) -> Screen:
    if not page.items:
        text = (
            "<b>Моя очередь</b>\n"
            "Назначенных заявок пока нет."
        )
        return _screen(
            text,
            [[_button("Главное меню", "menu", "main")]],
        )
    lines = [
        f"<b>Моя очередь</b> · страница {page.page + 1} из {page.pages}",
        f"Активных назначений: {page.total}",
    ]
    rows = []
    for request in page.items:
        rows.append(
            [
                _button(
                    f"#{request.id} · {_short(request.subject, 38)}",
                    "op",
                    "card",
                    request.id,
                    page.page,
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page.page > 0:
        navigation.append(_button("Назад", "op", "list", page.page - 1))
    if page.page + 1 < page.pages:
        navigation.append(_button("Далее", "op", "list", page.page + 1))
    if navigation:
        rows.append(navigation)
    rows.append([_button("Главное меню", "menu", "main")])
    return _screen("\n".join(lines), rows)


def operator_card(
    request: SupportRequest, page: int, notice: str | None = None
) -> Screen:
    lines = [
        f"<b>Заявка #{request.id}</b>",
        f"Статус: <b>{_status(request.status)}</b>",
        f"Клиент: <code>{request.requester_id}</code>",
        f"Создана: {esc(display_utc(request.created_at))}",
        "",
        f"<b>Тема:</b> {esc(request.subject)}",
        f"<b>Описание:</b>\n{esc(request.details)}",
    ]
    rows = [
        [
            _button(
                "Закрыть как решённую",
                "op",
                "done",
                request.id,
                request.version,
                page,
            )
        ],
        [_button("К очереди", "op", "list", page)],
    ]
    return _screen(_notice("\n".join(lines), notice), rows)


def operator_resolved(
    request: SupportRequest, notice: str | None = None
) -> Screen:
    text = (
        f"<b>Заявка #{request.id} решена.</b>\n"
        "Клиент увидит новый статус в карточке. "
        "Уведомление отправляется best-effort после фиксации изменения в базе; "
        "гарантии exactly-once у Telegram нет."
    )
    rows = [
        [_button("Моя очередь", "op", "list", 0)],
        [_button("Главное меню", "menu", "main")],
    ]
    return _screen(_notice(text, notice), rows)


def admin_panel(actor: ActorState, notice: str | None = None) -> Screen:
    lines = ["<b>Управление</b>", "Доступные административные разделы."]
    if not actor.accept_new_requests:
        lines.append("Приём новых заявок приостановлен.")
    rows = [
        [
            _button("Очередь заявок", "adm", "queue", 0),
            _button("Роли", "adm", "roles"),
        ],
        [
            _button("Настройки", "adm", "settings"),
            _button("Аудит", "adm", "audit", 0),
        ],
        [_button("Главное меню", "menu", "main")],
    ]
    return _screen(_notice("\n".join(lines), notice), rows)


def admin_queue(page: Page[SupportRequest]) -> Screen:
    if not page.items:
        text = (
            "<b>Очередь заявок</b>\n"
            "Активных заявок нет."
        )
        return _screen(
            text,
            [
                [_button("Управление", "adm", "panel")],
                [_button("Главное меню", "menu", "main")],
            ],
        )
    lines = [
        f"<b>Очередь заявок</b> · страница {page.page + 1} из {page.pages}",
        f"Новых и назначенных: {page.total}",
    ]
    rows = []
    for request in page.items:
        assignee = (
            f" → {request.assignee_id}"
            if request.assignee_id is not None
            else " → не назначена"
        )
        rows.append(
            [
                _button(
                    f"#{request.id} · {_status(request.status)}{assignee}",
                    "adm",
                    "req",
                    request.id,
                    page.page,
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page.page > 0:
        navigation.append(_button("Назад", "adm", "queue", page.page - 1))
    if page.page + 1 < page.pages:
        navigation.append(_button("Далее", "adm", "queue", page.page + 1))
    if navigation:
        rows.append(navigation)
    rows.append([_button("Управление", "adm", "panel")])
    return _screen("\n".join(lines), rows)


def admin_card(
    request: SupportRequest, page: int, notice: str | None = None
) -> Screen:
    lines = [
        f"<b>Заявка #{request.id}</b>",
        f"Статус: <b>{_status(request.status)}</b>",
        f"Клиент: <code>{request.requester_id}</code>",
    ]
    if request.assignee_id is not None:
        lines.append(f"Оператор: <code>{request.assignee_id}</code>")
    lines.extend(
        [
            f"Создана: {esc(display_utc(request.created_at))}",
            "",
            f"<b>Тема:</b> {esc(request.subject)}",
            f"<b>Описание:</b>\n{esc(request.details)}",
        ]
    )
    rows: list[list[InlineKeyboardButton]] = []
    if request.status in (STATUS_NEW, STATUS_ASSIGNED):
        rows.append(
            [_button("Назначить оператора", "adm", "assign", request.id, page)]
        )
    rows.append([_button("К очереди", "adm", "queue", page)])
    rows.append([_button("Управление", "adm", "panel")])
    return _screen(_notice("\n".join(lines), notice), rows)


def assign_picker(
    request: SupportRequest,
    operators: tuple[RoleEntry, ...],
    page: int,
    notice: str | None = None,
) -> Screen:
    lines = [
        f"<b>Назначение заявки #{request.id}</b>",
        "Выберите оператора. Заявка получит новую версию; "
        "кнопка, построенная по старой версии, будет отклонена.",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for operator in operators:
        label = str(operator.telegram_id)
        if operator.username:
            label = f"@{_short(operator.username, 32)} · {operator.telegram_id}"
        rows.append(
            [
                _button(
                    label,
                    "adm",
                    "pick",
                    request.id,
                    operator.telegram_id,
                    request.version,
                    page,
                )
            ]
        )
    if not operators:
        lines.append(
            "Активных операторов нет. Сначала выдайте роль в разделе «Роли»."
        )
        rows.append([_button("Роли", "adm", "roles")])
    rows.append([_button("К заявке", "adm", "req", request.id, page)])
    rows.append([_button("Управление", "adm", "panel")])
    return _screen(_notice("\n".join(lines), notice), rows)


def roles_screen(
    entries: tuple[RoleEntry, ...],
    actor: ActorState,
    notice: str | None = None,
) -> Screen:
    all_admins = [entry for entry in entries if entry.role == ROLE_ADMIN]
    all_operators = [entry for entry in entries if entry.role == ROLE_OPERATOR]
    admins = all_admins[:10]
    operators = all_operators[:15]
    lines = [
        "<b>Роли</b>",
        "Администраторы:",
    ]
    if admins:
        lines.extend(_user(entry.telegram_id, entry.username) for entry in admins)
    else:
        lines.append("нет")
    if len(all_admins) > len(admins):
        lines.append(f"… и ещё {len(all_admins) - len(admins)}")
    lines.append("\nОператоры:")
    if operators:
        lines.extend(_user(entry.telegram_id, entry.username) for entry in operators)
    else:
        lines.append("нет")
    if len(all_operators) > len(operators):
        lines.append(f"… и ещё {len(all_operators) - len(operators)}")
    lines.extend(
        [
            "",
            "Действие выполняется в два шага: numeric ID, затем подтверждение.",
        ]
    )
    if not actor.is_owner:
        lines.append("Выдавать и снимать администраторов может только владелец.")
    rows = [
        [
            _button("Выдать оператора", "adm", "role", "grant", ROLE_OPERATOR),
            _button("Снять оператора", "adm", "role", "revoke", ROLE_OPERATOR),
        ]
    ]
    if actor.is_owner:
        rows.append(
            [
                _button("Выдать админа", "adm", "role", "grant", ROLE_ADMIN),
                _button("Снять админа", "adm", "role", "revoke", ROLE_ADMIN),
            ]
        )
    rows.append([_button("Управление", "adm", "panel")])
    return _screen(_notice("\n".join(lines), notice), rows)


def role_target_prompt(title: str, notice: str | None = None) -> Screen:
    text = (
        f"<b>{esc(title)}</b>\n"
        "Отправьте numeric Telegram ID пользователя одним сообщением. "
        "Пользователь должен был хотя бы раз открыть бота. "
        "Username и @упоминание не подходят.\n\n"
        "Действие можно отменить кнопкой ниже или командой /cancel."
    )
    rows = [
        [_button("Отменить", "role", "cancel")],
        [_button("Роли", "adm", "roles")],
    ]
    return _screen(_notice(text, notice), rows)


def role_confirmation(pending: PendingRole, notice: str | None = None) -> Screen:
    if pending.target_id is None or not pending.token:
        raise ValueError("Confirmation screen requires a target and token.")
    action = ACTION_LABELS.get(pending.action, pending.action)
    role = ROLE_LABELS.get(pending.role, pending.role)
    text = (
        "<b>Подтвердите изменение роли</b>\n"
        f"Действие: <b>{esc(action)} роль «{esc(role)}»</b>\n"
        f"Пользователь: {_user(pending.target_id, pending.target_username)}\n"
        f"Подтверждение действует до: {esc(display_utc(pending.expires_at or ''))}\n\n"
        "Кнопка одноразовая. После изменения прав старые кнопки будут проверены заново."
    )
    rows = [
        [_button("Подтвердить", "role", "ok", pending.token)],
        [_button("Отменить", "role", "cancel")],
        [_button("Роли", "adm", "roles")],
    ]
    return _screen(_notice(text, notice), rows)


def user_settings(actor: ActorState, notice: str | None = None) -> Screen:
    state = "включены" if actor.notifications_enabled else "выключены"
    text = (
        "<b>Настройки</b>\n"
        f"Уведомления о статусе моих заявок: <b>{state}</b>.\n"
        "Уведомление отправляется после успешного изменения в базе; "
        "повторная доставка Telegram не гарантируется."
    )
    target_value = 0 if actor.notifications_enabled else 1
    label = "Выключить уведомления" if actor.notifications_enabled else "Включить уведомления"
    rows = [
        [_button(label, "usr", "notify", target_value)],
        [_button("Главное меню", "menu", "main")],
    ]
    return _screen(_notice(text, notice), rows)


def admin_settings(
    accept_new_requests: bool,
    version: int,
    notice: str | None = None,
) -> Screen:
    state = "включён" if accept_new_requests else "выключен"
    text = (
        "<b>Рабочие настройки</b>\n"
        f"Приём новых заявок: <b>{state}</b>.\n"
        f"Версия настройки: <code>{version}</code>.\n\n"
        "Выключение блокирует создание и подтверждение новых заявок, "
        "уже существующие заявки остаются доступны."
    )
    target_value = 0 if accept_new_requests else 1
    label = "Выключить приём" if accept_new_requests else "Включить приём"
    rows = [
        [_button(label, "adm", "set", "accept", target_value, version)],
        [_button("Управление", "adm", "panel")],
    ]
    return _screen(_notice(text, notice), rows)


def audit_screen(page: Page[AuditEntry]) -> Screen:
    if not page.items:
        text = (
            "<b>Аудит</b>\n"
            "Событий пока нет."
        )
        return _screen(
            text,
            [
                [_button("Управление", "adm", "panel")],
                [_button("Главное меню", "menu", "main")],
            ],
        )
    lines = [
        f"<b>Аудит</b> · страница {page.page + 1} из {page.pages}",
        f"Всего событий: {page.total}",
        "",
    ]
    for entry in page.items:
        target = f" {esc(entry.object_type)}"
        if entry.object_id:
            target += f" #{esc(entry.object_id)}"
        lines.append(
            f"<code>{esc(display_utc(entry.created_at))}</code> "
            f"actor <code>{entry.actor_id}</code>\n"
            f"{esc(entry.action)}{target} · {esc(entry.outcome)}"
        )
    rows: list[list[InlineKeyboardButton]] = []
    navigation: list[InlineKeyboardButton] = []
    if page.page > 0:
        navigation.append(_button("Назад", "adm", "audit", page.page - 1))
    if page.page + 1 < page.pages:
        navigation.append(_button("Далее", "adm", "audit", page.page + 1))
    if navigation:
        rows.append(navigation)
    rows.append([_button("Управление", "adm", "panel")])
    return _screen("\n".join(lines), rows)


def error_screen(
    message: str,
    back_parts: tuple[object, ...] = ("menu", "main"),
    back_label: str = "Главное меню",
) -> Screen:
    text = f"<b>Действие не выполнено</b>\n{esc(message)}"
    return _screen(text, [[_button(back_label, *back_parts)]])


def make_role_prompt(
    action: str, role: str, notice: str | None = None
) -> Screen:
    action_label = ACTION_LABELS.get(action, action)
    role_label = ROLE_LABELS.get(role, role)
    return role_target_prompt(
        f"{action_label.capitalize()} роль «{role_label}»", notice=notice
    )
