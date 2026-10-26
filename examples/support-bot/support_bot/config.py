"""Environment configuration and its startup validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .util import parse_numeric_id


class ConfigError(RuntimeError):
    """Invalid or missing deployment configuration."""


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Переменная окружения {name} не задана. "
            "Заполните её локально или на хостинге; в репозиторий секреты не попадают."
        )
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    owner_id: int
    db_path: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if environ is None else environ
        token = _required(values, "BOT_TOKEN")
        left, separator, right = token.partition(":")
        if (
            not separator
            or not left.isdigit()
            or not right
            or any(character.isspace() for character in token)
        ):
            raise ConfigError(
                "BOT_TOKEN имеет неверный формат. Токен бота не выводится в лог."
            )
        raw_owner = _required(values, "OWNER_ID")
        try:
            owner_id = parse_numeric_id(raw_owner)
        except ValueError as exc:
            raise ConfigError(
                "OWNER_ID должен быть положительным numeric Telegram ID "
                "(не username и не @упоминание)."
            ) from exc
        raw_db = values.get("SUPPORT_DB_PATH", "support_bot.sqlite3").strip()
        if not raw_db or raw_db == ":memory:":
            raise ConfigError(
                "SUPPORT_DB_PATH должен указывать на файл SQLite на постоянном томе."
            )
        return cls(bot_token=token, owner_id=owner_id, db_path=Path(raw_db))
