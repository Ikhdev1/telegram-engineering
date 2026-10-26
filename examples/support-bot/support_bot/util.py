"""Small, dependency-free helpers shared by the example."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Telegram user identifiers fit into a signed 52-bit value.  Keeping the
# upper bound explicit rejects pasted chat ids, negative ids and accidental
# arbitrary integers before they reach SQLite or callback data.
MAX_TELEGRAM_ID = 9_007_199_254_740_991  # 2**53 - 1

_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,15}\Z")
_BOT_TOKEN = re.compile(r"\b[0-9]{6,}:[A-Za-z0-9_-]{20,}\b")


def redact_secrets(value: object) -> str:
    """Remove token-shaped substrings before a value reaches a log line."""

    return _BOT_TOKEN.sub("<bot-token>", str(value))


def parse_numeric_id(raw: str) -> int:
    """Parse a strictly positive decimal Telegram user id.

    Whitespace around a pasted value is accepted; signs, separators and
    anything else that is not a plain decimal integer are rejected.
    """

    text = raw.strip()
    if not _NUMERIC_ID.fullmatch(text):
        raise ValueError("Ожидается положительный numeric ID без знаков и пробелов.")
    value = int(text)
    if value > MAX_TELEGRAM_ID:
        raise ValueError("Numeric ID выходит за допустимый диапазон Telegram.")
    return value


def validate_numeric_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Numeric ID должен быть целым числом.")
    if value <= 0 or value > MAX_TELEGRAM_ID:
        raise ValueError("Numeric ID выходит за допустимый диапазон Telegram.")
    return value


def utc_now() -> str:
    """UTC timestamp with second precision and an explicit offset."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_after(minutes: int) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=minutes)
    ).isoformat()


def is_expired(timestamp: str | None) -> bool:
    if not timestamp:
        return True
    try:
        value = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= datetime.now(timezone.utc)


def display_utc(timestamp: str) -> str:
    """Render a stored ISO timestamp without relying on the local timezone."""

    try:
        value = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
