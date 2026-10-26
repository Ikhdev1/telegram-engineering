"""Shared service primitives: errors, delivery dedup, permissions, rows."""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Callable, TypeVar

from .db import Database
from .models import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ActorState,
    Draft,
    PendingRole,
    SupportRequest,
)
from .util import validate_numeric_id, utc_now

PAGE_SIZE = 5
AUDIT_PAGE_SIZE = 8

_T = TypeVar("_T")

REQUEST_COLUMNS = (
    "id, requester_id, subject, details, status, assignee_id, version, "
    "created_at, updated_at"
)


class DomainError(Exception):
    """Expected, user-safe rejection of an operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DuplicateUpdate(Exception):
    """The (bot_id, update_id) delivery was already committed."""

    def __init__(self, action: str, outcome: dict[str, Any] | None) -> None:
        super().__init__(f"Update already processed: {action}")
        self.action = action
        self.outcome = outcome


def request_from_row(row: sqlite3.Row) -> SupportRequest:
    return SupportRequest(
        id=int(row["id"]),
        requester_id=int(row["requester_id"]),
        subject=str(row["subject"]),
        details=str(row["details"]),
        status=str(row["status"]),
        assignee_id=int(row["assignee_id"]) if row["assignee_id"] is not None else None,
        version=int(row["version"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def draft_from_row(row: sqlite3.Row) -> Draft:
    return Draft(
        telegram_id=int(row["telegram_id"]),
        step=str(row["step"]),
        subject=str(row["subject"]),
        details=str(row["details"]),
        token=str(row["token"]) if row["token"] is not None else None,
        updated_at=str(row["updated_at"]),
    )


def pending_from_row(row: sqlite3.Row) -> PendingRole:
    return PendingRole(
        actor_id=int(row["actor_id"]),
        action=str(row["action"]),
        role=str(row["role"]),
        stage=str(row["stage"]),
        target_id=int(row["target_id"]) if row["target_id"] is not None else None,
        target_username=(
            str(row["target_username"]) if row["target_username"] is not None else None
        ),
        token=str(row["token"]) if row["token"] is not None else None,
        expires_at=str(row["expires_at"]) if row["expires_at"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def outcome_json(outcome: dict[str, Any] | None) -> str:
    return json.dumps(outcome or {}, ensure_ascii=False, separators=(",", ":"))


def load_outcome(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def page_bounds(total: int, requested_page: int, size: int) -> tuple[int, int, int]:
    pages = max(1, math.ceil(total / size))
    page = max(0, min(requested_page, pages - 1))
    return page, pages, page * size


def request_id_from_outcome(outcome: dict[str, Any] | None) -> int | None:
    if not outcome:
        return None
    value = outcome.get("request_id")
    return value if isinstance(value, int) else None


class ServiceCore:
    def __init__(self, db: Database, owner_id: int) -> None:
        self.db = db
        self.owner_id = validate_numeric_id(owner_id)

    def _mutate(
        self,
        bot_id: int,
        update_id: int,
        action: str,
        operation: Callable[[sqlite3.Connection], tuple[_T, dict[str, Any]]],
    ) -> tuple[_T | None, DuplicateUpdate | None]:
        """Claim one delivery and commit its mutation in the same transaction.

        A validation error rolls the claim back, so a redelivered update can
        be attempted again.  A duplicate committed delivery returns the stored
        outcome without executing the operation a second time.
        """

        try:
            with self.db.write() as conn:
                try:
                    conn.execute(
                        """
                        INSERT INTO processed_updates
                            (bot_id, update_id, action, outcome, processed_at)
                        VALUES (?, ?, ?, '', ?)
                        """,
                        (bot_id, update_id, action, utc_now()),
                    )
                except sqlite3.IntegrityError as exc:
                    row = conn.execute(
                        """
                        SELECT action, outcome FROM processed_updates
                        WHERE bot_id = ? AND update_id = ?
                        """,
                        (bot_id, update_id),
                    ).fetchone()
                    raise DuplicateUpdate(
                        str(row["action"]) if row is not None else action,
                        load_outcome(str(row["outcome"])) if row is not None else None,
                    ) from exc

                result, outcome = operation(conn)
                conn.execute(
                    """
                    UPDATE processed_updates SET outcome = ?
                    WHERE bot_id = ? AND update_id = ?
                    """,
                    (outcome_json(outcome), bot_id, update_id),
                )
                return result, None
        except DuplicateUpdate as exc:
            return None, exc

    @staticmethod
    def _require_private(private_chat: bool) -> None:
        if not private_chat:
            raise DomainError(
                "private_only",
                "Управление и данные доступны только в личном чате с ботом.",
            )

    @staticmethod
    def _ensure_user(conn: sqlite3.Connection, telegram_id: int) -> None:
        now = utc_now()
        conn.execute(
            """
            INSERT INTO users (telegram_id, username, created_at, updated_at)
            VALUES (?, NULL, ?, ?)
            ON CONFLICT(telegram_id) DO NOTHING
            """,
            (telegram_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO user_settings (telegram_id, notifications, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(telegram_id) DO NOTHING
            """,
            (telegram_id, now),
        )

    @staticmethod
    def _get_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
        return conn.execute(
            f"SELECT {REQUEST_COLUMNS} FROM requests WHERE id = ?",
            (request_id,),
        ).fetchone()

    @staticmethod
    def _get_draft(conn: sqlite3.Connection, telegram_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM drafts WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

    @staticmethod
    def _is_admin(conn: sqlite3.Connection, telegram_id: int, owner_id: int) -> bool:
        if telegram_id == owner_id:
            return True
        row = conn.execute(
            "SELECT 1 FROM role_bindings WHERE telegram_id = ? AND role = ?",
            (telegram_id, ROLE_ADMIN),
        ).fetchone()
        return row is not None

    @staticmethod
    def _is_operator(conn: sqlite3.Connection, telegram_id: int) -> bool:
        row = conn.execute(
            "SELECT 1 FROM role_bindings WHERE telegram_id = ? AND role = ?",
            (telegram_id, ROLE_OPERATOR),
        ).fetchone()
        return row is not None

    def _require_admin(self, conn: sqlite3.Connection, telegram_id: int) -> None:
        if not self._is_admin(conn, telegram_id, self.owner_id):
            raise DomainError(
                "forbidden",
                "Недостаточно прав: доступ мог быть отозван. Откройте меню заново.",
            )

    def _require_operator(self, conn: sqlite3.Connection, telegram_id: int) -> None:
        if not self._is_operator(conn, telegram_id):
            raise DomainError(
                "forbidden",
                "Недостаточно прав: роль оператора не активна.",
            )

    def _require_role_manager(
        self, conn: sqlite3.Connection, actor_id: int, role: str
    ) -> None:
        if role == ROLE_ADMIN:
            if actor_id != self.owner_id:
                raise DomainError(
                    "forbidden",
                    "Выдавать и снимать администраторов может только владелец.",
                )
            return
        if not self._is_admin(conn, actor_id, self.owner_id):
            raise DomainError(
                "forbidden",
                "Выдавать и снимать операторов может администратор или владелец.",
            )

    def _require_accepting(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'accept_new_requests'"
        ).fetchone()
        if row is None or str(row["value"]) != "1":
            raise DomainError(
                "requests_paused",
                "Приём новых заявок приостановлен администратором. Попробуйте позже.",
            )

    @staticmethod
    def _notifications_enabled(conn: sqlite3.Connection, telegram_id: int) -> bool:
        row = conn.execute(
            "SELECT notifications FROM user_settings WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return row is not None and bool(row["notifications"])

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        actor_id: int,
        action: str,
        object_type: str,
        object_id: int | str | None,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_events
                (actor_id, action, object_type, object_id, outcome, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_id,
                action,
                object_type,
                None if object_id is None else str(object_id),
                outcome,
                outcome_json(details),
                utc_now(),
            ),
        )

    @staticmethod
    def _typed_request(
        conn: sqlite3.Connection, request_id: int, error_code: str, message: str
    ) -> sqlite3.Row:
        row = ServiceCore._get_request(conn, request_id)
        if row is None:
            raise DomainError(error_code, message)
        return row

    @staticmethod
    def _load_pending(
        conn: sqlite3.Connection, actor_id: int
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT p.*, u.username AS target_username
            FROM role_pending AS p
            LEFT JOIN users AS u ON u.telegram_id = p.target_id
            WHERE p.actor_id = ?
            """,
            (actor_id,),
        ).fetchone()

    def touch_user(self, telegram_id: int, username: str | None) -> None:
        telegram_id = validate_numeric_id(telegram_id)
        safe_username = username.strip()[:64] if username else None
        now = utc_now()
        with self.db.write() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, username, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    updated_at = excluded.updated_at
                """,
                (telegram_id, safe_username, now, now),
            )
            conn.execute(
                """
                INSERT INTO user_settings (telegram_id, notifications, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(telegram_id) DO NOTHING
                """,
                (telegram_id, now),
            )

    def actor_state(self, telegram_id: int) -> ActorState:
        with self.db.read() as conn:
            roles = frozenset(
                str(row["role"])
                for row in conn.execute(
                    "SELECT role FROM role_bindings WHERE telegram_id = ?",
                    (telegram_id,),
                )
            )
            notification_row = conn.execute(
                "SELECT notifications FROM user_settings WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            accept_row = conn.execute(
                "SELECT value, version FROM settings WHERE key = 'accept_new_requests'"
            ).fetchone()
            return ActorState(
                telegram_id=telegram_id,
                is_owner=telegram_id == self.owner_id,
                roles=roles,
                notifications_enabled=(
                    bool(notification_row["notifications"])
                    if notification_row is not None
                    else True
                ),
                accept_new_requests=(
                    bool(accept_row is not None and str(accept_row["value"]) == "1")
                ),
                accept_version=(
                    int(accept_row["version"]) if accept_row is not None else 0
                ),
            )

    def get_draft(self, telegram_id: int) -> Draft | None:
        with self.db.read() as conn:
            row = self._get_draft(conn, telegram_id)
            return draft_from_row(row) if row is not None else None

    def _request_by_id_if_any(self, request_id: int | None) -> SupportRequest | None:
        if request_id is None:
            return None
        with self.db.read() as conn:
            row = self._get_request(conn, request_id)
        return request_from_row(row) if row is not None else None
