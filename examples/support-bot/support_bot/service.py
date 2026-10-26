"""Public service facade.

The implementation lives in three focused modules:

``service_core``
    Delivery dedup, transaction boundaries, permissions, row conversion and
    user-facing reads.
``request_service``
    Drafts, client requests, operator queue and admin assignment.
``governance_service``
    Role changes, workspace/user settings and audit reads.

Handlers and tests import only ``SupportService``/``DomainError`` from here.
"""

from __future__ import annotations

from .governance_service import GovernanceServiceMixin
from .request_service import RequestServiceMixin
from .service_core import (
    AUDIT_PAGE_SIZE,
    PAGE_SIZE,
    DomainError,
    DuplicateUpdate,
    ServiceCore,
)

__all__ = [
    "AUDIT_PAGE_SIZE",
    "DomainError",
    "DuplicateUpdate",
    "PAGE_SIZE",
    "SupportService",
]


class SupportService(RequestServiceMixin, GovernanceServiceMixin, ServiceCore):
    """Flat business API used by the aiogram router and tests."""
