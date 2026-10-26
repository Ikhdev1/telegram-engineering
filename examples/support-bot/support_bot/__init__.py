"""Runnable aiogram 3 + SQLite support-bot reference example."""

from __future__ import annotations

__version__ = "1.0.0"

from .config import ConfigError, Settings
from .db import Database
from .models import (
    ActorState,
    AuditEntry,
    Draft,
    Page,
    PendingRole,
    RoleEntry,
    SupportRequest,
)
from .service import DomainError, SupportService

__all__ = [
    "ActorState",
    "AuditEntry",
    "ConfigError",
    "Database",
    "DomainError",
    "Draft",
    "Page",
    "PendingRole",
    "RoleEntry",
    "Settings",
    "SupportRequest",
    "SupportService",
    "__version__",
]
