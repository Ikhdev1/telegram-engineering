"""Role management, workspace settings and audit reading."""

from __future__ import annotations

import secrets
import sqlite3
from typing import Any

from .models import (
    ROLE_ACTIONS,
    ROLE_NAMES,
    AuditEntry,
    Page,
    PendingRole,
    PendingRoleResult,
    RoleEntry,
    RoleMutationResult,
    SettingsMutationResult,
)
from .service_core import (
    AUDIT_PAGE_SIZE,
    DomainError,
    page_bounds,
    pending_from_row,
)
from .util import is_expired, parse_numeric_id, utc_after, utc_now

ROLE_CONFIRM_TTL_MINUTES = 10


class GovernanceServiceMixin:
    def begin_role_change(
        self,
        actor_id: int,
        action: str,
        role: str,
        *,
        private_chat: bool,
        bot_id: int,
        update_id: int,
    ) -> PendingRoleResult:
        if action not in ROLE_ACTIONS or role not in ROLE_NAMES:
            raise DomainError("invalid_role_action", "Неизвестное действие с ролью.")

        def operation(conn: sqlite3.Connection) -> tuple[PendingRole, dict[str, Any]]:
            self._require_private(private_chat)
            self._require_role_manager(conn, actor_id, role)
            self._ensure_user(conn, actor_id)
            now = utc_now()
            conn.execute(
                """
                INSERT INTO role_pending
                    (actor_id, action, role, stage, target_id, token,
                     expires_at, created_at, updated_at)
                VALUES (?, ?, ?, 'target', NULL, NULL, NULL, ?, ?)
                ON CONFLICT(actor_id) DO UPDATE SET
                    action = excluded.action,
                    role = excluded.role,
                    stage = 'target',
                    target_id = NULL,
                    token = NULL,
                    expires_at = NULL,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (actor_id, action, role, now, now),
            )
            row = self._load_pending(conn, actor_id)
            assert row is not None
            return pending_from_row(row), {"action": action, "role": role}

        result, duplicate = self._mutate(
            bot_id, update_id, f"role.begin.{action}.{role}", operation
        )
        if duplicate is not None:
            return PendingRoleResult(self.get_pending_role(actor_id), duplicate=True)
        assert result is not None
        return PendingRoleResult(result)

    def submit_role_target(
        self,
        actor_id: int,
        raw_target: str,
        *,
        private_chat: bool,
        bot_id: int,
        update_id: int,
    ) -> PendingRoleResult:
        def operation(conn: sqlite3.Connection) -> tuple[PendingRole, dict[str, Any]]:
            self._require_private(private_chat)
            row = self._load_pending(conn, actor_id)
            if row is None or str(row["stage"]) != "target":
                raise DomainError(
                    "role_no_pending",
                    "Нет ожидающего действия с ролью. Откройте раздел «Роли» заново.",
                )
            self._require_role_manager(conn, actor_id, str(row["role"]))
            try:
                target_id = parse_numeric_id(raw_target)
            except ValueError as exc:
                raise DomainError("invalid_target", str(exc)) from exc
            if target_id == self.owner_id:
                raise DomainError(
                    "invalid_target",
                    "Владелец задаётся конфигурацией; менять его через роли нельзя.",
                )
            target = conn.execute(
                "SELECT username FROM users WHERE telegram_id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise DomainError(
                    "unknown_target",
                    "Пользователь с таким ID ещё не открывал бота.",
                )
            role = str(row["role"])
            action = str(row["action"])
            has_role = conn.execute(
                """
                SELECT 1 FROM role_bindings
                WHERE telegram_id = ? AND role = ?
                """,
                (target_id, role),
            ).fetchone()
            if action == "grant" and has_role is not None:
                raise DomainError(
                    "role_exists",
                    "У пользователя уже есть эта роль. Введите другой ID или отмените.",
                )
            if action == "revoke" and has_role is None:
                raise DomainError(
                    "role_missing",
                    "У пользователя нет этой роли. Введите другой ID или отмените.",
                )
            token = secrets.token_hex(10)
            expires_at = utc_after(ROLE_CONFIRM_TTL_MINUTES)
            conn.execute(
                """
                UPDATE role_pending
                SET stage = 'confirm', target_id = ?, token = ?,
                    expires_at = ?, updated_at = ?
                WHERE actor_id = ? AND stage = 'target'
                """,
                (target_id, token, expires_at, utc_now(), actor_id),
            )
            updated = self._load_pending(conn, actor_id)
            assert updated is not None
            return pending_from_row(updated), {"target_id": target_id}

        result, duplicate = self._mutate(
            bot_id, update_id, "role.target", operation
        )
        if duplicate is not None:
            return PendingRoleResult(self.get_pending_role(actor_id), duplicate=True)
        assert result is not None
        return PendingRoleResult(result)

    def confirm_role_change(
        self,
        actor_id: int,
        token: str,
        *,
        private_chat: bool,
        bot_id: int,
        update_id: int,
    ) -> RoleMutationResult:
        def operation(conn: sqlite3.Connection) -> tuple[RoleMutationResult, dict[str, Any]]:
            self._require_private(private_chat)
            row = self._load_pending(conn, actor_id)
            if (
                row is None
                or str(row["stage"]) != "confirm"
                or not row["token"]
                or not secrets.compare_digest(str(row["token"]), token)
            ):
                raise DomainError(
                    "role_stale",
                    "Подтверждение устарело или уже использовано. Откройте «Роли» заново.",
                )
            if is_expired(str(row["expires_at"]) if row["expires_at"] else None):
                raise DomainError(
                    "role_expired",
                    "Срок подтверждения истёк. Начните действие с ролью заново.",
                )
            action = str(row["action"])
            role = str(row["role"])
            target_id = int(row["target_id"])
            self._require_role_manager(conn, actor_id, role)
            if target_id == self.owner_id:
                raise DomainError(
                    "invalid_target",
                    "Владелец задаётся конфигурацией; менять его через роли нельзя.",
                )
            if conn.execute(
                "SELECT 1 FROM users WHERE telegram_id = ?", (target_id,)
            ).fetchone() is None:
                raise DomainError(
                    "unknown_target",
                    "Пользователь с таким ID ещё не открывал бота.",
                )
            exists = conn.execute(
                "SELECT 1 FROM role_bindings WHERE telegram_id = ? AND role = ?",
                (target_id, role),
            ).fetchone()
            if action == "grant":
                if exists is not None:
                    raise DomainError(
                        "role_exists",
                        "Роль уже выдана. Откройте «Роли» и проверьте состояние.",
                    )
                conn.execute(
                    """
                    INSERT INTO role_bindings
                        (telegram_id, role, granted_by, granted_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (target_id, role, actor_id, utc_now()),
                )
            else:
                if exists is None:
                    raise DomainError(
                        "role_missing",
                        "Роль уже снята. Откройте «Роли» и проверьте состояние.",
                    )
                conn.execute(
                    "DELETE FROM role_bindings WHERE telegram_id = ? AND role = ?",
                    (target_id, role),
                )
            self._audit(
                conn,
                actor_id,
                f"role.{action}",
                "user",
                target_id,
                "applied",
                {"role": role, "action": action},
            )
            conn.execute("DELETE FROM role_pending WHERE actor_id = ?", (actor_id,))
            result = RoleMutationResult(
                applied=True,
                action=action,
                role=role,
                target_id=target_id,
            )
            return result, {
                "action": action,
                "role": role,
                "target_id": target_id,
            }

        result, duplicate = self._mutate(
            bot_id, update_id, "role.confirm", operation
        )
        if duplicate is not None:
            outcome = duplicate.outcome or {}
            return RoleMutationResult(
                applied=True,
                action=str(outcome.get("action", "")),
                role=str(outcome.get("role", "")),
                target_id=(
                    int(outcome["target_id"])
                    if isinstance(outcome.get("target_id"), int)
                    else None
                ),
                duplicate=True,
            )
        assert result is not None
        return result

    def cancel_role_change(
        self,
        actor_id: int,
        *,
        private_chat: bool,
        bot_id: int,
        update_id: int,
    ) -> bool:
        def operation(conn: sqlite3.Connection) -> tuple[bool, dict[str, Any]]:
            self._require_private(private_chat)
            conn.execute("DELETE FROM role_pending WHERE actor_id = ?", (actor_id,))
            return True, {"kind": "role.cancel"}

        result, duplicate = self._mutate(
            bot_id, update_id, "role.cancel", operation
        )
        return True if duplicate is not None else bool(result)

    def get_pending_role(self, actor_id: int) -> PendingRole | None:
        with self.db.read() as conn:
            row = self._load_pending(conn, actor_id)
            return pending_from_row(row) if row is not None else None

    def roles_overview(self, actor_id: int) -> tuple[RoleEntry, ...]:
        with self.db.read() as conn:
            self._require_admin(conn, actor_id)
            rows = conn.execute(
                """
                SELECT r.telegram_id, r.role, u.username, r.granted_by, r.granted_at
                FROM role_bindings AS r
                LEFT JOIN users AS u ON u.telegram_id = r.telegram_id
                ORDER BY r.role ASC, r.telegram_id ASC
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

    def set_notifications(
        self,
        actor_id: int,
        enabled: bool,
        *,
        bot_id: int,
        update_id: int,
    ) -> SettingsMutationResult:
        def operation(conn: sqlite3.Connection) -> tuple[SettingsMutationResult, dict[str, Any]]:
            self._ensure_user(conn, actor_id)
            previous = self._notifications_enabled(conn, actor_id)
            if previous == enabled:
                return SettingsMutationResult(enabled, 0), {"value": enabled}
            value = 1 if enabled else 0
            now = utc_now()
            conn.execute(
                """
                INSERT INTO user_settings (telegram_id, notifications, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    notifications = excluded.notifications,
                    updated_at = excluded.updated_at
                """,
                (actor_id, value, now),
            )
            self._audit(
                conn,
                actor_id,
                "settings.notifications",
                "user",
                actor_id,
                "updated",
                {"from": previous, "to": enabled},
            )
            return SettingsMutationResult(enabled, 0), {"value": enabled}

        result, duplicate = self._mutate(
            bot_id, update_id, "settings.notifications", operation
        )
        if duplicate is not None:
            outcome = duplicate.outcome or {}
            return SettingsMutationResult(
                bool(outcome.get("value", False)), 0, duplicate=True
            )
        assert result is not None
        return result

    def set_accept_new_requests(
        self,
        actor_id: int,
        enabled: bool,
        expected_version: int,
        *,
        private_chat: bool,
        bot_id: int,
        update_id: int,
    ) -> SettingsMutationResult:
        def operation(conn: sqlite3.Connection) -> tuple[SettingsMutationResult, dict[str, Any]]:
            self._require_private(private_chat)
            self._require_admin(conn, actor_id)
            row = conn.execute(
                "SELECT value, version FROM settings WHERE key = 'accept_new_requests'"
            ).fetchone()
            if row is None:
                raise DomainError(
                    "settings_missing",
                    "Настройка не найдена. Проверьте миграции базы.",
                )
            if int(row["version"]) != expected_version:
                raise DomainError(
                    "stale",
                    "Настройка уже изменилась. Откройте экран заново.",
                )
            value = "1" if enabled else "0"
            changed = conn.execute(
                """
                UPDATE settings
                SET value = ?, version = version + 1, updated_at = ?, updated_by = ?
                WHERE key = 'accept_new_requests' AND version = ?
                """,
                (value, utc_now(), actor_id, expected_version),
            ).rowcount
            if changed != 1:
                raise DomainError(
                    "stale",
                    "Настройка уже изменилась. Откройте экран заново.",
                )
            self._audit(
                conn,
                actor_id,
                "settings.accept_new_requests",
                "setting",
                "accept_new_requests",
                "updated",
                {"from": str(row["value"]), "to": value},
            )
            return (
                SettingsMutationResult(enabled, expected_version + 1),
                {"value": enabled, "version": expected_version + 1},
            )

        result, duplicate = self._mutate(
            bot_id, update_id, "settings.accept", operation
        )
        if duplicate is not None:
            outcome = duplicate.outcome or {}
            return SettingsMutationResult(
                bool(outcome.get("value", False)),
                int(outcome.get("version", 0))
                if isinstance(outcome.get("version"), int)
                else 0,
                duplicate=True,
            )
        assert result is not None
        return result

    def get_admin_settings(self, actor_id: int) -> tuple[bool, int]:
        with self.db.read() as conn:
            self._require_admin(conn, actor_id)
            row = conn.execute(
                "SELECT value, version FROM settings WHERE key = 'accept_new_requests'"
            ).fetchone()
            if row is None:
                raise DomainError(
                    "settings_missing",
                    "Настройка не найдена. Проверьте миграции базы.",
                )
            return str(row["value"]) == "1", int(row["version"])

    def audit_page(self, actor_id: int, page: int) -> Page[AuditEntry]:
        with self.db.read() as conn:
            self._require_admin(conn, actor_id)
            total = int(
                conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            )
            page, pages, offset = page_bounds(total, page, AUDIT_PAGE_SIZE)
            rows = conn.execute(
                """
                SELECT id, actor_id, action, object_type, object_id, outcome,
                       details, created_at
                FROM audit_events
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (AUDIT_PAGE_SIZE, offset),
            ).fetchall()
            return Page(
                items=tuple(
                    AuditEntry(
                        id=int(row["id"]),
                        actor_id=int(row["actor_id"]),
                        action=str(row["action"]),
                        object_type=str(row["object_type"]),
                        object_id=(
                            str(row["object_id"]) if row["object_id"] else None
                        ),
                        outcome=str(row["outcome"]),
                        details=str(row["details"]),
                        created_at=str(row["created_at"]),
                    )
                    for row in rows
                ),
                page=page,
                pages=pages,
                total=total,
            )
