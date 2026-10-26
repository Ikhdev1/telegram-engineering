"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Sequence

from .app import run_polling
from .config import ConfigError, Settings
from .util import redact_secrets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m support_bot",
        description=(
            "Reference support bot on aiogram 3 + SQLite. "
            "Normal mode polls Telegram using BOT_TOKEN, OWNER_ID and "
            "optional SUPPORT_DB_PATH. --offline-smoke uses a temporary "
            "database and a no-network fake transport."
        ),
        epilog=(
            "Configuration: BOT_TOKEN (required), OWNER_ID (required numeric "
            "Telegram ID), SUPPORT_DB_PATH (default support_bot.sqlite3). "
            "Only one polling process may use a token and database."
        ),
    )
    parser.add_argument(
        "--offline-smoke",
        action="store_true",
        help=(
            "run the full dispatcher/DB smoke without network, token or "
            "Telegram account; prints a JSON report"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.offline_smoke:
        # Imported lazily so ordinary runs do not load the fake transport.
        from .offline import run_offline_smoke

        try:
            report = asyncio.run(run_offline_smoke())
        except Exception as exc:  # noqa: BLE001 - CLI must return a clear failure
            print(
                json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
                file=sys.stdout,
            )
            return 1
        print(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
            file=sys.stdout,
        )
        return 0 if report.ok else 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        asyncio.run(run_polling(settings))
    except ConfigError as exc:
        print(f"Startup refused: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level supervisor boundary
        logging.getLogger(__name__).error(
            "Bot stopped after an unexpected error: %s: %s",
            type(exc).__name__,
            redact_secrets(exc),
        )
        return 1
    return 0
