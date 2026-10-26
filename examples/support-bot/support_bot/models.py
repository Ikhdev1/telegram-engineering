"""Plain domain records exchanged between storage, service and renderers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

STATUS_NEW = "new"
STATUS_ASSIGNED = "assigned"
STATUS_RESOLVED = "resolved"
STATUS_CANCELLED = "cancelled"

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"

ROLE_ACTIONS = ("grant", "revoke")
ROLE_NAMES = (ROLE_ADMIN, ROLE_OPERATOR)


@dataclass(frozen=True)
class Draft:
    telegram_id: int
    step: str
    subject: str
    details: str
    token: str | None
    updated_at: str


@dataclass(frozen=True)
class SupportRequest:
    id: int
    requester_id: int
    subject: str
    details: str
    status: str
    assignee_id: int | None
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RoleEntry:
    telegram_id: int
    role: str
    username: str | None
    granted_by: int
    granted_at: str


@dataclass(frozen=True)
class PendingRole:
    actor_id: int
    action: str
    role: str
    stage: str
    target_id: int | None
    target_username: str | None
    token: str | None
    expires_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AuditEntry:
    id: int
    actor_id: int
    action: str
    object_type: str
    object_id: str | None
    outcome: str
    details: str
    created_at: str


@dataclass(frozen=True)
class ActorState:
    telegram_id: int
    is_owner: bool
    roles: frozenset[str]
    notifications_enabled: bool
    accept_new_requests: bool
    accept_version: int

    @property
    def is_admin(self) -> bool:
        return self.is_owner or ROLE_ADMIN in self.roles

    @property
    def is_operator(self) -> bool:
        return ROLE_OPERATOR in self.roles


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    page: int
    pages: int
    total: int


@dataclass(frozen=True)
class DraftMutation:
    draft: Draft | None
    duplicate: bool = False


@dataclass(frozen=True)
class ConfirmDraftResult:
    request: SupportRequest | None
    duplicate: bool = False


@dataclass(frozen=True)
class RequestMutationResult:
    request: SupportRequest | None
    duplicate: bool = False
    notify_requester: bool = False


@dataclass(frozen=True)
class PendingRoleResult:
    pending: PendingRole | None
    duplicate: bool = False


@dataclass(frozen=True)
class RoleMutationResult:
    applied: bool
    action: str
    role: str
    target_id: int | None
    duplicate: bool = False


@dataclass(frozen=True)
class SettingsMutationResult:
    value: bool
    version: int
    duplicate: bool = False
