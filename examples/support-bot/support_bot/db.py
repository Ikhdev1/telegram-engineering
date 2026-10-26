"""Durable SQLite storage with explicit, versioned migrations.

One process is expected.  Every operation opens its own connection, so no
connection is shared between asyncio tasks or worker threads.  Writes use
``BEGIN IMMEDIATE``; this both acquires the write lock early and gives the
caller a predictable place to compare-and-swap object versions.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import ConfigError

SCHEMA_VERSION = 1

# Each entry is applied once, in order, inside BEGIN IMMEDIATE.  Statements
# are executed individually because sqlite3.executescript() commits the
# pending transaction implicitly.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE role_bindings (
            telegram_id INTEGER NOT NULL
                REFERENCES users(telegram_id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('admin', 'operator')),
            granted_by INTEGER NOT NULL,
            granted_at TEXT NOT NULL,
            PRIMARY KEY (telegram_id, role)
        )
        """,
        """
        CREATE TABLE drafts (
            telegram_id INTEGER PRIMARY KEY
                REFERENCES users(telegram_id) ON DELETE CASCADE,
            step TEXT NOT NULL CHECK (step IN ('subject', 'details', 'preview')),
            subject TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            token TEXT UNIQUE,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_id INTEGER NOT NULL REFERENCES users(telegram_id),
            subject TEXT NOT NULL,
            details TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('new', 'assigned', 'resolved', 'cancelled')),
            assignee_id INTEGER REFERENCES users(telegram_id),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (status = 'new' AND assignee_id IS NULL)
                OR (status IN ('assigned', 'resolved') AND assignee_id IS NOT NULL)
                OR status = 'cancelled'
            )
        )
        """,
        "CREATE INDEX requests_owner_page ON requests (requester_id, created_at DESC, id DESC)",
        "CREATE INDEX requests_queue_page ON requests (status, created_at, id)",
        "CREATE INDEX requests_assignee_page ON requests (assignee_id, status, created_at, id)",
        """
        CREATE TABLE user_settings (
            telegram_id INTEGER PRIMARY KEY
                REFERENCES users(telegram_id) ON DELETE CASCADE,
            notifications INTEGER NOT NULL DEFAULT 1 CHECK (notifications IN (0, 1)),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 1),
            updated_at TEXT NOT NULL,
            updated_by INTEGER
        )
        """,
        """
        INSERT INTO settings (key, value, version, updated_at, updated_by)
        VALUES ('accept_new_requests', '1', 1, '1970-01-01T00:00:00+00:00', NULL)
        """,
        """
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT,
            outcome TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX audit_events_page ON audit_events (created_at DESC, id DESC)",
        """
        CREATE TABLE processed_updates (
            bot_id INTEGER NOT NULL,
            update_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            outcome TEXT NOT NULL DEFAULT '',
            processed_at TEXT NOT NULL,
            PRIMARY KEY (bot_id, update_id)
        )
        """,
        """
        CREATE TABLE role_pending (
            actor_id INTEGER PRIMARY KEY
                REFERENCES users(telegram_id) ON DELETE CASCADE,
            action TEXT NOT NULL CHECK (action IN ('grant', 'revoke')),
            role TEXT NOT NULL CHECK (role IN ('admin', 'operator')),
            stage TEXT NOT NULL CHECK (stage IN ('target', 'confirm')),
            target_id INTEGER,
            token TEXT UNIQUE,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (stage = 'target' AND target_id IS NULL AND token IS NULL)
                OR (stage = 'confirm' AND target_id IS NOT NULL AND token IS NOT NULL)
            )
        )
        """,
    ),
}


class Database:
    """File-backed SQLite database with one connection per operation."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if str(path) == ":memory:":
            raise ValueError(
                "In-memory SQLite is unsafe here: every operation uses its own "
                "connection. Use a real file path."
            )
        self.path = Path(path)
        self.busy_timeout_ms = max(0, int(busy_timeout_ms))

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def initialize(self) -> None:
        """Create or upgrade the schema atomically.

        The current version is read while holding the write lock, so an
        overlapping start cannot apply migration 1 twice.  The WAL pragma runs
        before that lock, so a different writer can still make this call fail;
        a persistent lock is mapped to a clear ConfigError instead of exposing
        a raw SQLite OperationalError.  There is deliberately no retry loop:
        this example supports one process.
        """

        try:
            self._initialize()
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                raise ConfigError(
                    "SQLite database is locked by another process or active "
                    "writer. This example supports one process only; stop the "
                    "other instance or wait for the writer, then restart. "
                    "Startup was not retried and no data was changed."
                ) from exc
            raise

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if current > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Database schema version {current} is newer than this "
                        f"code supports ({SCHEMA_VERSION}). Refusing to start."
                    )
                for version in range(current + 1, SCHEMA_VERSION + 1):
                    for statement in MIGRATIONS.get(version, ()):
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {version}")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
