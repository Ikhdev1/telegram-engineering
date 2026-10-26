from __future__ import annotations

import unittest
from pathlib import Path

from support_bot.config import ConfigError, Settings
from support_bot.db import Database
from support_bot.util import MAX_TELEGRAM_ID, parse_numeric_id, redact_secrets


class SettingsTest(unittest.TestCase):
    def test_minimal_environment_and_default_db(self) -> None:
        settings = Settings.from_env(
            {"BOT_TOKEN": "42:TEST", "OWNER_ID": "123456789"}
        )
        self.assertEqual(settings.owner_id, 123456789)
        self.assertEqual(settings.db_path, Path("support_bot.sqlite3"))
        self.assertNotIn("42:TEST", repr(settings))

    def test_missing_values_fail_closed(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({"OWNER_ID": "123"})
        with self.assertRaises(ConfigError):
            Settings.from_env({"BOT_TOKEN": "42:TEST"})
        with self.assertRaises(ConfigError):
            Settings.from_env({"BOT_TOKEN": "42:TEST", "OWNER_ID": ""})

    def test_owner_is_strictly_numeric(self) -> None:
        for value in ("@someone", "0", "-5", "+123", "1 23", "9007199254740992"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    Settings.from_env(
                        {"BOT_TOKEN": "42:TEST", "OWNER_ID": value}
                    )

    def test_token_format_is_not_echoed(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({"BOT_TOKEN": "bad token", "OWNER_ID": "123"})
        with self.assertRaises(ConfigError):
            Settings.from_env({"BOT_TOKEN": "no-colon", "OWNER_ID": "123"})

    def test_memory_database_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env(
                {
                    "BOT_TOKEN": "42:TEST",
                    "OWNER_ID": "123",
                    "SUPPORT_DB_PATH": ":memory:",
                }
            )


class NumericIdTest(unittest.TestCase):
    def test_parse_valid(self) -> None:
        self.assertEqual(parse_numeric_id("123"), 123)
        self.assertEqual(parse_numeric_id(f"  {MAX_TELEGRAM_ID}  "), MAX_TELEGRAM_ID)

    def test_parse_invalid(self) -> None:
        for value in ("", "0", "-1", "+1", "1.0", "1e3", "abc", str(MAX_TELEGRAM_ID + 1)):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_numeric_id(value)


class SecretRedactionTest(unittest.TestCase):
    def test_token_shaped_value_is_not_logged(self) -> None:
        token_like = "123456789:" + "A" * 25
        self.assertNotIn(token_like, redact_secrets(f"failed for {token_like}"))
        self.assertIn("<bot-token>", redact_secrets(f"failed for {token_like}"))
        self.assertEqual(redact_secrets("42:TEST"), "42:TEST")


class DatabasePathTest(unittest.TestCase):
    def test_memory_database_object_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Database(":memory:")


if __name__ == "__main__":
    unittest.main()
