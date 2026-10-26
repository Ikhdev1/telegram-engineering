"""Client drafts/requests, operator queue and admin assignment."""

from __future__ import annotations

import secrets
import sqlite3
from typing import Any

from .models import (
    STATUS_ASSIGNED,
    STATUS_NEW,
    ConfirmDraftResult,
    Draft,
    DraftMutation,
    Page,
    RequestMutationResult,
    RoleEntry,
    SupportRequest,
)
from .service_core import (
    PAGE_SIZE,
    REQUEST_COLUMNS,
    DomainError,
    draft_from_row,
    page_bounds,
    request_from_row,
    request_id_from_outcome,
)
from .util import utc_now


class RequestServiceMixin:
    def start_draft(
        self, telegram_id: int, bot_id: int, update_id: int
    ) -> DraftMutation:
        def operation(conn: sqlite3.Connection) -> tuple[Draft, dict[str, Any]]:
            self._require_accepting(conn)
            self._ensure_user(conn, telegram_id)
            now = utc_now()
            conn.execute(
                """
                INSERT INTO drafts
                    (telegram_id, step, subject, details, token, updated_at)
                VALUES (?, 'subject', '', '', NULL, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    step = 'subject',
                    subject = '',
                    details = '',
                    token = NULL,
                    updated_at = excluded.updated_at
                """,
                (telegram_id, now),
            )
            row = self._get_draft(conn, telegram_id)
            assert row is not None
            return draft_from_row(row), {"kind": "draft.started"}

        result, duplicate = self._mutate(
            bot_id, update_id, "draft.start", operation
        )
        if duplicate is not None:
            return DraftMutation(self.get_draft(telegram_id), duplicate=True)
        assert result is not None
        return DraftMutation(result)

    def save_draft_subject(
        self, telegram_id: int, text: str, bot_id: int, update_id: int
    ) -> DraftMutation:
        subject = " ".join(text.split())
        if "\x00" in subject or not (3 <= len(subject) <= 120):
            raise DomainError(
                "invalid_subject",
                "Тема должна быть текстом от 3 до 120 символов.",
            )

        def operation(conn: sqlite3.Connection) -> tuple[Draft, dict[str, Any]]:
            row = self._get_draft(conn, telegram_id)
            if row is None or str(row["step"]) != "subject":
                raise DomainError(
                    "draft_missing",
                    "Черновик не найден. Начните создание заявки заново.",
                )
            conn.execute(
                """
                UPDATE drafts
                SET subject = ?, step = 'details', token = NULL, updated_at = ?
                WHERE telegram_id = ? AND step = 'subject'
                """,
                (subject, utc_now(), telegram_id),
            )
            updated = self._get_draft(conn, telegram_id)
            assert updated is not None
            return draft_from_row(updated), {"step": "details"}

        result, duplicate = self._mutate(
            bot_id, update_id, "draft.subject", operation
        )
        if duplicate is not None:
            return DraftMutation(self.get_draft(telegram_id), duplicate=True)
        assert result is not None
        return DraftMutation(result)

    def save_draft_details(
        self, telegram_id: int, text: str, bot_id: int, update_id: int
    ) -> DraftMutation:
        details = text.strip()
        if "\x00" in details or not (10 <= len(details) <= 2000):
            raise DomainError(
                "invalid_details",
                "Описание должно быть текстом от 10 до 2000 символов.",
            )

        def operation(conn: sqlite3.Connection) -> tuple[Draft, dict[str, Any]]:
            row = self._get_draft(conn, telegram_id)
            if row is None or str(row["step"]) != "details":
                raise DomainError(
                    "draft_missing",
                    "Черновик не найден. Начните создание заявки заново.",
                )
            token = ""
            for _ in range(3):
                token = secrets.token_hex(10)
                try:
                    updated_count = conn.execute(
                        """
                        UPDATE drafts
                        SET details = ?, step = 'preview', token = ?, updated_at = ?
                        WHERE telegram_id = ? AND step = 'details'
                        """,
                        (details, token, utc_now(), telegram_id),
                    ).rowcount
                except sqlite3.IntegrityError:
                    continue
                if updated_count == 1:
                    break
            else:
                raise DomainError(
                    "draft_conflict",
                    "Не удалось подготовить подтверждение. Попробуйте отправить описание ещё раз.",
                )
            updated = self._get_draft(conn, telegram_id)
            assert updated is not None
            return draft_from_row(updated), {"step": "preview"}

        result, duplicate = self._mutate(
            bot_id, update_id, "draft.details", operation
        )
        if duplicate is not None:
            return DraftMutation(self.get_draft(telegram_id), duplicate=True)
        assert result is not None
        return DraftMutation(result)

    def draft_back(
        self, telegram_id: int, bot_id: int, update_id: int
    ) -> DraftMutation:
        def operation(conn: sqlite3.Connection) -> tuple[Draft, dict[str, Any]]:
            row = self._get_draft(conn, telegram_id)
            if row is None:
                raise DomainError(
                    "draft_missing",
                    "Черновик не найден. Начните создание заявки заново.",
                )
            step = str(row["step"])
            if step == "preview":
                next_step = "details"
            elif step == "details":
                next_step = "subject"
            else:
                raise DomainError(
                    "draft_back_unavailable",
                    "С первого шага нельзя вернуться назад; используйте отмену.",
                )
            conn.execute(
                """
                UPDATE drafts SET step = ?, token = NULL, updated_at = ?
                WHERE telegram_id = ?
                """,
                (next_step, utc_now(), telegram_id),
            )
            updated = self._get_draft(conn, telegram_id)
            assert updated is not None
            return draft_from_row(updated), {"step": next_step}

        result, duplicate = self._mutate(bot_id, update_id, "draft.back", operation)
        if duplicate is not None:
            return DraftMutation(self.get_draft(telegram_id), duplicate=True)
        assert result is not None
        return DraftMutation(result)

    def cancel_draft(self, telegram_id: int, bot_id: int, update_id: int) -> bool:
        def operation(conn: sqlite3.Connection) -> tuple[bool, dict[str, Any]]:
            conn.execute("DELETE FROM drafts WHERE telegram_id = ?", (telegram_id,))
            return True, {"kind": "draft.cancelled"}

        result, duplicate = self._mutate(
            bot_id, update_id, "draft.cancel", operation
        )
        return True if duplicate is not None else bool(result)

    def cancel_inputs(self, telegram_id: int, bot_id: int, update_id: int) -> bool:
        def operation(conn: sqlite3.Connection) -> tuple[bool, dict[str, Any]]:
            conn.execute("DELETE FROM drafts WHERE telegram_id = ?", (telegram_id,))
            conn.execute("DELETE FROM role_pending WHERE actor_id = ?", (telegram_id,))
            return True, {"kind": "inputs.cancelled"}

        result, duplicate = self._mutate(
            bot_id, update_id, "inputs.cancel", operation
        )
        return True if duplicate is not None else bool(result)

    def confirm_draft(
        self,
        telegram_id: int,
        token: str,
        bot_id: int,
        update_id: int,
    ) -> ConfirmDraftResult:
        def operation(conn: sqlite3.Connection) -> tuple[SupportRequest, dict[str, Any]]:
            self._require_accepting(conn)
            row = self._get_draft(conn, telegram_id)
            if row is None:
                raise DomainError(
                    "draft_missing",
                    "Кнопка устарела: черновик уже подтверждён или отменён. "
                    "Откройте «Мои заявки».",
                )
            if str(row["step"]) != "preview" or not row["token"]:
                raise DomainError(
                    "draft_stale",
                    "Подтверждение устарело. Вернитесь к предпросмотру.",
                )
            if not secrets.compare_digest(str(row["token"]), token):
                raise DomainError(
                    "draft_stale",
                    "Это подтверждение устарело или уже использовано.",
                )
            now = utc_now()
            cursor = conn.execute(
                """
                INSERT INTO requests
                    (requester_id, subject, details, status, assignee_id, version,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'new', NULL, 1, ?, ?)
                """,
                (
                    telegram_id,
                    str(row["subject"]),
                    str(row["details"]),
                    now,
                    now,
                ),
            )
            request_id = int(cursor.lastrowid)
            conn.execute("DELETE FROM drafts WHERE telegram_id = ?", (telegram_id,))
            self._audit(
                conn,
                telegram_id,
                "request.create",
                "request",
                request_id,
                "created",
                {"status": STATUS_NEW},
            )
            created = self._typed_request(
                conn, request_id, "request_missing", "Заявка не найдена."
            )
            return request_from_row(created), {"request_id": request_id}

        result, duplicate = self._mutate(
            bot_id, update_id, "draft.confirm", operation
        )
        if duplicate is not None:
            request_id = request_id_from_outcome(duplicate.outcome)
            if request_id is None:
                return ConfirmDraftResult(None, duplicate=True)
            with self.db.read() as conn:
                row = self._get_request(conn, request_id)
            return ConfirmDraftResult(
                request_from_row(row) if row is not None else None,
                duplicate=True,
            )
        assert result is not None
        return ConfirmDraftResult(result)

    def list_own_requests(self, telegram_id: int, page: int) -> Page[SupportRequest]:
        with self.db.read() as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE requester_id = ?",
                    (telegram_id,),
                ).fetchone()[0]
            )
            page, pages, offset = page_bounds(total, page, PAGE_SIZE)
            rows = conn.execute(
                f"""
                SELECT {REQUEST_COLUMNS} FROM requests
                WHERE requester_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (telegram_id, PAGE_SIZE, offset),
            ).fetchall()
            return Page(
                items=tuple(request_from_row(row) for row in rows),
                page=page,
                pages=pages,
                total=total,
            )

    def get_own_request(
        self, telegram_id: int, request_id: int
    ) -> SupportRequest:
        with self.db.read() as conn:
            row = conn.execute(
                f"SELECT {REQUEST_COLUMNS} FROM requests WHERE id = ? AND requester_id = ?",
                (request_id, telegram_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "not_found",
                    "Заявка не найдена или недоступна.",
                )
            return request_from_row(row)

    def cancel_request(
        self,
        telegram_id: int,
        request_id: int,
        expected_version: int,
        bot_id: int,
        update_id: int,
    ) -> RequestMutationResult:
        def operation(conn: sqlite3.Connection) -> tuple[SupportRequest, dict[str, Any]]:
            row = self._typed_request(
                conn, request_id, "not_found", "Заявка не найдена или недоступна."
            )
            if int(row["requester_id"]) != telegram_id:
                raise DomainError("not_found", "Заявка не найдена или недоступна.")
            if str(row["status"]) not in (STATUS_NEW, STATUS_ASSIGNED):
                raise DomainError(
                    "stale",
                    "Кнопка устарела: заявка уже закрыта. Откройте список заново.",
                )
            now = utc_now()
            changed = conn.execute(
                """
                UPDATE requests
                SET status = 'cancelled', version = version + 1, updated_at = ?
                WHERE id = ? AND requester_id = ? AND version = ?
                  AND status IN ('new', 'assigned')
                """,
                (now, request_id, telegram_id, expected_version),
            ).rowcount
            if changed != 1:
                raise DomainError(
                    "stale",
                    "Кнопка устарела: состояние заявки уже изменилось.",
                )
            self._audit(
                conn,
                telegram_id,
                "request.cancel",
                "request",
                request_id,
                "cancelled",
                {"version": expected_version + 1},
            )
            updated = self._typed_request(
                conn, request_id, "not_found", "Заявка не найдена."
            )
            return request_from_row(updated), {"request_id": request_id}

        result, duplicate = self._mutate(
            bot_id, update_id, "request.cancel", operation
        )
        if duplicate is not None:
            return RequestMutationResult(
                self._request_by_id_if_any(
                    request_id_from_outcome(duplicate.outcome)
                ),
                duplicate=True,
            )
        assert result is not None
        return RequestMutationResult(result)

    def list_operator_queue(
        self, actor_id: int, page: int
    ) -> Page[SupportRequest]:
        with self.db.read() as conn:
            self._require_operator(conn, actor_id)
            total = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM requests
                    WHERE assignee_id = ? AND status = 'assigned'
                    """,
                    (actor_id,),
                ).fetchone()[0]
            )
            page, pages, offset = page_bounds(total, page, PAGE_SIZE)
            rows = conn.execute(
                f"""
                SELECT {REQUEST_COLUMNS} FROM requests
                WHERE assignee_id = ? AND status = 'assigned'
                ORDER BY created_at ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (actor_id, PAGE_SIZE, offset),
            ).fetchall()
            return Page(
                items=tuple(request_from_row(row) for row in rows),
                page=page,
                pages=pages,
                total=total,
            )

    def get_operator_request(
        self, actor_id: int, request_id: int
    ) -> SupportRequest:
        with self.db.read() as conn:
            self._require_operator(conn, actor_id)
            row = conn.execute(
                f"""
                SELECT {REQUEST_COLUMNS} FROM requests
                WHERE id = ? AND assignee_id = ? AND status = 'assigned'
                """,
                (request_id, actor_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "not_found",
                    "Заявка не найдена или уже обработана.",
                )
            return request_from_row(row)

    def resolve_request(
        self,
        actor_id: int,
        request_id: int,
        expected_version: int,
        *,
        private_chat: bool,
        bot_id: int,
        update_id: int,
    ) -> RequestMutationResult:
        def operation(conn: sqlite3.Connection) -> tuple[SupportRequest, dict[str, Any]]:
            self._require_private(private_chat)
            self._require_operator(conn, actor_id)
            row = self._typed_request(
                conn, request_id, "not_found", "Заявка не найдена или недоступна."
            )
            if int(row["assignee_id"] or 0) != actor_id or str(row["status"]) != STATUS_ASSIGNED:
                raise DomainError(
                    "stale",
                    "Кнопка устарела: заявка уже обработана или переназначена.",
                )
            changed = conn.execute(
                """
                UPDATE requests
                SET status = 'resolved', version = version + 1, updated_at = ?
                WHERE id = ? AND assignee_id = ? AND version = ?
                  AND status = 'assigned'
                """,
                (utc_now(), request_id, actor_id, expected_version),
            ).rowcount
            if changed != 1:
                raise DomainError(
                    "stale",
                    "Кнопка устарела: состояние заявки уже изменилось.",
                )
            requester_id = int(row["requester_id"])
            self._audit(
                conn,
                actor_id,
                "request.resolve",
                "request",
                request_id,
                "resolved",
                {"assignee_id": actor_id, "version": expected_version + 1},
            )
            updated = self._typed_request(
                conn, request_id, "not_found", "Заявка не найдена."
            )
            return request_from_row(updated), {
                "request_id": request_id,
                "requester_id": requester_id,
            }

        result, duplicate = self._mutate(
            bot_id, update_id, "request.resolve", operation
        )
        if duplicate is not None:
            return RequestMutationResult(
                self._request_by_id_if_any(
                    request_id_from_outcome(duplicate.outcome)
                ),
                duplicate=True,
            )
        assert result is not None
        with self.db.read() as conn:
            notify = self._notifications_enabled(conn, result.requester_id)
        return RequestMutationResult(result, notify_requester=notify)

    def list_admin_queue(
        self, actor_id: int, page: int
    ) -> Page[SupportRequest]:
        with self.db.read() as conn:
            self._require_admin(conn, actor_id)
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE status IN ('new', 'assigned')"
                ).fetchone()[0]
            )
            page, pages, offset = page_bounds(total, page, PAGE_SIZE)
            rows = conn.execute(
                f"""
                SELECT {REQUEST_COLUMNS} FROM requests
                WHERE status IN ('new', 'assigned')
                ORDER BY created_at ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (PAGE_SIZE, offset),
            ).fetchall()
            return Page(
                items=tuple(request_from_row(row) for row in rows),
                page=page,
                pages=pages,
                total=total,
            )

    def get_admin_request(
        self, actor_id: int, request_id: int
    ) -> SupportRequest:
        with self.db.read() as conn:
            self._require_admin(conn, actor_id)
            row = self._typed_request(
                conn, request_id, "not_found", "Заявка не найдена."
            )
            return request_from_row(row)

    def list_operators(self, actor_id: int) -> tuple[RoleEntry, ...]:
        with self.db.read() as conn:
            self._require_admin(conn, actor_id)
            rows = conn.execute(
                """
                SELECT r.telegram_id, r.role, u.username, r.granted_by, r.granted_at
                FROM role_bindings AS r
                LEFT JOIN users AS u ON u.telegram_id = r.telegram_id
                WHERE r.role = 'operator'
                ORDER BY r.granted_at ASC, r.telegram_id ASC
                """
            ).fetchall()
            return tuple(
                RoleEntry(
                    telegram_id=int(row["telegram_id"]),
                    role=str(row["role"]),
                    username=str(row["username"]) if row["username"] else None,
                    granted_by=int(row["granted_by"]),
                    granted_at=str(row["granted_at"]),
                )
                for row in rows
            )

    def assign_request(
        self,
        actor_id: int,
        request_id: int,
        operator_id: int,
        expected_version: int,
        *,
        private_chat: bool,
        bot_id: int,
        update_id: int,
    ) -> RequestMutationResult:
        def operation(conn: sqlite3.Connection) -> tuple[SupportRequest, dict[str, Any]]:
            self._require_private(private_chat)
            self._require_admin(conn, actor_id)
            if operator_id == self.owner_id:
                raise DomainError(
                    "invalid_target",
                    "Владелец задаётся конфигурацией и не назначается оператором.",
                )
            if not self._is_operator(conn, operator_id):
                raise DomainError(
                    "not_operator",
                    "У выбранного пользователя нет активной роли оператора.",
                )
            row = self._typed_request(
                conn, request_id, "not_found", "Заявка не найдена."
            )
            status = str(row["status"])
            if status not in (STATUS_NEW, STATUS_ASSIGNED):
                raise DomainError(
                    "stale",
                    "Кнопка устарела: заявка уже закрыта.",
                )
            if status == STATUS_ASSIGNED and int(row["assignee_id"] or 0) == operator_id:
                raise DomainError(
                    "already_assigned",
                    "Заявка уже назначена этому оператору.",
                )
            changed = conn.execute(
                """
                UPDATE requests
                SET assignee_id = ?, status = 'assigned',
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND status IN ('new', 'assigned')
                """,
                (operator_id, utc_now(), request_id, expected_version),
            ).rowcount
            if changed != 1:
                raise DomainError(
                    "stale",
                    "Кнопка устарела: состояние заявки уже изменилось.",
                )
            requester_id = int(row["requester_id"])
            self._audit(
                conn,
                actor_id,
                "request.assign",
                "request",
                request_id,
                "assigned",
                {
                    "assignee_id": operator_id,
                    "previous_status": status,
                    "version": expected_version + 1,
                },
            )
            updated = self._typed_request(
                conn, request_id, "not_found", "Заявка не найдена."
            )
            return request_from_row(updated), {
                "request_id": request_id,
                "requester_id": requester_id,
            }

        result, duplicate = self._mutate(
            bot_id, update_id, "request.assign", operation
        )
        if duplicate is not None:
            return RequestMutationResult(
                self._request_by_id_if_any(
                    request_id_from_outcome(duplicate.outcome)
                ),
                duplicate=True,
            )
        assert result is not None
        with self.db.read() as conn:
            notify = self._notifications_enabled(conn, result.requester_id)
        return RequestMutationResult(result, notify_requester=notify)
