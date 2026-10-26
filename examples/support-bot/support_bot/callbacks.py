"""Callback-data encoding with the Telegram 1..64 byte limit enforced locally.

Callback data is an untrusted transport string.  It carries only opaque
identifiers, actions and versions; permissions are always re-read from the
database by the service.
"""

from __future__ import annotations

import re

from .util import MAX_TELEGRAM_ID

MAX_CALLBACK_BYTES = 64
_PART = re.compile(r"[a-z0-9_]+\Z")
_ID_PART = re.compile(r"[1-9][0-9]{0,15}\Z")


class CallbackError(ValueError):
    """Malformed, oversized or unsupported callback data."""


def encode(*parts: object) -> str:
    values = [str(part) for part in parts]
    if not values:
        raise CallbackError("Callback data must not be empty.")
    for value in values:
        if not _PART.fullmatch(value):
            raise CallbackError(f"Unsafe callback part: {value!r}")
    data = ":".join(values)
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise CallbackError(
            f"Callback data exceeds {MAX_CALLBACK_BYTES} UTF-8 bytes: {data!r}"
        )
    return data


def parse(data: str) -> tuple[str, ...]:
    if not data:
        raise CallbackError("Empty callback data.")
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise CallbackError("Callback data is longer than 64 UTF-8 bytes.")
    parts = tuple(data.split(":"))
    if any(not part for part in parts):
        raise CallbackError("Callback data contains an empty part.")
    for part in parts:
        if not _PART.fullmatch(part):
            raise CallbackError("Callback data contains unsupported characters.")
    return parts


def parse_id(value: str) -> int:
    if not _ID_PART.fullmatch(value):
        raise CallbackError("Expected a positive numeric Telegram ID.")
    parsed = int(value)
    if parsed > MAX_TELEGRAM_ID:
        raise CallbackError("Numeric ID is outside the safe Telegram range.")
    return parsed


def parse_uint(value: str, *, maximum: int | None = None) -> int:
    if not value.isdigit() or (len(value) > 1 and value.startswith("0")):
        raise CallbackError("Expected a canonical unsigned integer.")
    parsed = int(value)
    if maximum is not None and parsed > maximum:
        raise CallbackError("Callback number is out of range.")
    return parsed


def parse_choice(value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise CallbackError("Unsupported callback choice.")
    return value
