from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PAYLOADS = ROOT / "assets" / "payloads"
SCRIPT = SCRIPTS / "telegram_payload.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from telegram_payload import SUPPORTED_METHODS, validate_request  # noqa: E402


def _findings(method: str, payload: object) -> list[dict[str, str]]:
    return validate_request(method, payload)  # type: ignore[arg-type]


def _errors(method: str, payload: object) -> list[dict[str, str]]:
    return [
        finding
        for finding in _findings(method, payload)
        if finding["severity"] == "error"
    ]


def _codes(method: str, payload: object) -> set[str]:
    return {finding["code"] for finding in _findings(method, payload)}


def _assert_valid(method: str, payload: object) -> None:
    errors = _errors(method, payload)
    if errors:
        raise AssertionError(f"unexpected errors: {errors}")


class InterfaceTests(unittest.TestCase):
    def test_findings_have_documented_shape_and_values(self) -> None:
        findings = _findings("sendMessage", {"text": "x", "foo": True})
        self.assertTrue(findings)
        for finding in findings:
            self.assertEqual(set(finding), {"severity", "code", "path", "message"})
            self.assertIn(finding["severity"], {"error", "warning"})
            self.assertIsInstance(finding["code"], str)
            self.assertTrue(finding["path"].startswith("$"))
            self.assertIsInstance(finding["message"], str)
        self.assertIn("missing_field", {item["code"] for item in findings})
        self.assertIn("unknown_field", {item["code"] for item in findings})

    def test_unknown_method_is_reported_not_raised(self) -> None:
        findings = _findings("sendDocument", {"chat_id": 1})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "unsupported_method")
        self.assertEqual(findings[0]["severity"], "error")

    def test_non_object_payload_is_reported(self) -> None:
        findings = _findings("sendMessage", ["not", "an", "object"])
        self.assertEqual(findings[0]["code"], "payload_not_object")

    def test_bool_is_not_an_id(self) -> None:
        payload = {"chat_id": True, "text": "hello"}
        self.assertIn("wrong_type", _codes("sendMessage", payload))
        edit_payload = {
            "chat_id": 1,
            "message_id": False,
            "text": "hello",
        }
        self.assertIn("wrong_type", _codes("editMessageText", edit_payload))

    def test_method_names_are_case_insensitive(self) -> None:
        for method in ("sendMessage", "SendMessage", "SENDMESSAGE", "sendmessage"):
            with self.subTest(method=method):
                _assert_valid(method, {"chat_id": 1, "text": "x"})
        self.assertIn(
            "unsupported_method",
            _codes("SendDocument", {"chat_id": 1, "text": "x"}),
        )

    def test_chat_id_requires_username_or_numeric_exception(self) -> None:
        for value in ("@valid_name", "123", "-1001234567890"):
            with self.subTest(value=value):
                _assert_valid("sendMessage", {"chat_id": value, "text": "x"})
        for value in ("abc", "@abc", "@bad name", "123abc", "0"):
            with self.subTest(value=value):
                self.assertIn(
                    "invalid_chat_username" if value != "0" else "invalid_value",
                    _codes("sendMessage", {"chat_id": value, "text": "x"}),
                )

    def test_lone_surrogates_are_rejected_across_nested_strings(self) -> None:
        cases = {
            "rich_html": (
                "sendRichMessage",
                {"chat_id": 1, "rich_message": {"html": "<p>\ud800</p>"}},
            ),
            "rich_markdown": (
                "sendRichMessage",
                {"chat_id": 1, "rich_message": {"markdown": "\ud800"}},
            ),
            "rich_blocks": (
                "sendRichMessage",
                {
                    "chat_id": 1,
                    "rich_message": {
                        "blocks": [{"type": "paragraph", "text": "\ud800"}]
                    },
                },
            ),
            "button_text": (
                "sendMessage",
                {
                    "chat_id": 1,
                    "text": "x",
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"text": "\ud800", "callback_data": "x"}]
                        ]
                    },
                },
            ),
            "copy_text": (
                "sendMessage",
                {
                    "chat_id": 1,
                    "text": "x",
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "copy",
                                    "copy_text": {"text": "\ud800"},
                                }
                            ]
                        ]
                    },
                },
            ),
            "answer_text": (
                "answerCallbackQuery",
                {"callback_query_id": "cbq", "text": "\ud800"},
            ),
            "dict_key": (
                "sendMessage",
                {"chat_id": 1, "text": "x", "bad\ud800": 1},
            ),
        }
        for name, (method, payload) in cases.items():
            with self.subTest(name=name):
                self.assertIn("invalid_unicode", _codes(method, payload))

    def test_findings_cap_fails_closed_on_late_errors(self) -> None:
        buttons = [
            {
                "text": "warning",
                "web_app": {"url": "https://example.com/app"},
            }
            for _ in range(200)
        ]
        buttons.append({"text": "error", "callback_data": "x" * 65})
        payload = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {"inline_keyboard": [buttons]},
        }
        findings = _findings("sendMessage", payload)
        errors = [f for f in findings if f["severity"] == "error"]
        self.assertTrue(errors)
        self.assertIn("findings_truncated", {f["code"] for f in errors})
        self.assertLessEqual(len(findings), 201)

    def test_envelope_fixtures_are_valid_and_complete(self) -> None:
        files = sorted(PAYLOADS.glob("*.json"))
        self.assertGreaterEqual(len(files), 6)
        for path in files:
            with self.subTest(path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(set(data), {"method", "payload"})
                self.assertIn(data["method"], SUPPORTED_METHODS)
                errors = _errors(data["method"], data["payload"])
                self.assertEqual(errors, [], f"{path.name}: {errors}")


class OrdinaryTextTests(unittest.TestCase):
    def test_plain_text_boundary(self) -> None:
        _assert_valid("sendMessage", {"chat_id": 1, "text": "x" * 4096})
        self.assertIn(
            "text_too_long",
            _codes("sendMessage", {"chat_id": 1, "text": "x" * 4097}),
        )
        self.assertIn(
            "text_too_long",
            _codes("sendMessage", {"chat_id": 1, "text": "x" * 12000}),
        )

    def test_emoji_limit_is_counted_in_characters_not_utf8_bytes(self) -> None:
        # 3000 emoji are 12000 UTF-8 bytes but only 3000 visible characters.
        payload = {"chat_id": 1, "text": "😀" * 3000}
        self.assertEqual(len(payload["text"].encode("utf-8")), 12000)
        _assert_valid("sendMessage", payload)
        self.assertIn(
            "text_too_long",
            _codes("sendMessage", {"chat_id": 1, "text": "😀" * 5000}),
        )

    def test_html_visible_length_and_allowlist(self) -> None:
        _assert_valid(
            "sendMessage",
            {
                "chat_id": 1,
                "text": "<b>" + "x" * 4096 + "</b>",
                "parse_mode": "HTML",
            },
        )
        self.assertIn(
            "text_too_long",
            _codes(
                "sendMessage",
                {
                    "chat_id": 1,
                    "text": "<b>" + "x" * 4097 + "</b>",
                    "parse_mode": "HTML",
                },
            ),
        )
        h1 = {
            "chat_id": 1,
            "text": "<h1>Heading</h1>",
            "parse_mode": "HTML",
        }
        self.assertIn("html_tag_unsupported", _codes("sendMessage", h1))

    def test_escaping_and_entity_rules(self) -> None:
        _assert_valid(
            "sendMessage",
            {
                "chat_id": 1,
                "text": "Анна &amp; Иван &lt;ok&gt;",
                "parse_mode": "HTML",
            },
        )
        raw_amp = {
            "chat_id": 1,
            "text": "Анна & Иван",
            "parse_mode": "HTML",
        }
        self.assertIn(
            "html_unescaped_ampersand",
            _codes("sendMessage", raw_amp),
        )
        ordinary_nbsp = {
            "chat_id": 1,
            "text": "a&nbsp;b",
            "parse_mode": "HTML",
        }
        self.assertIn(
            "html_entity_unsupported",
            _codes("sendMessage", ordinary_nbsp),
        )
        self.assertIn(
            "empty_text",
            _codes("sendMessage", {"chat_id": 1, "text": "", "parse_mode": "HTML"}),
        )

    def test_safe_nesting_and_broken_nesting(self) -> None:
        _assert_valid(
            "sendMessage",
            {
                "chat_id": 1,
                "text": "<b><i><span class=\"tg-spoiler\">x</span></i></b>",
                "parse_mode": "HTML",
            },
        )
        mismatched = {
            "chat_id": 1,
            "text": "<b><i>x</b></i>",
            "parse_mode": "HTML",
        }
        codes = _codes("sendMessage", mismatched)
        self.assertIn("html_mismatched_tag", codes)
        self.assertIn("html_unexpected_end_tag", codes)
        pre_bold = {
            "chat_id": 1,
            "text": "<pre><b>x</b></pre>",
            "parse_mode": "HTML",
        }
        self.assertIn("html_nesting", _codes("sendMessage", pre_bold))
        nested_quote = {
            "chat_id": 1,
            "text": "<blockquote><blockquote>x</blockquote></blockquote>",
            "parse_mode": "HTML",
        }
        self.assertIn("html_nesting", _codes("sendMessage", nested_quote))

    def test_tag_specific_code_rules(self) -> None:
        _assert_valid(
            "sendMessage",
            {
                "chat_id": 1,
                "text": "<pre><code class=\"language-python\">print(1)</code></pre>",
                "parse_mode": "HTML",
            },
        )
        standalone_class = {
            "chat_id": 1,
            "text": "<code class=\"language-python\">x</code>",
            "parse_mode": "HTML",
        }
        self.assertIn(
            "html_attribute_unknown",
            _codes("sendMessage", standalone_class),
        )
        _assert_valid(
            "sendMessage",
            {
                "chat_id": 1,
                "text": "<tg-time unix=\"1647531900\" format=\"wDT\">22:45</tg-time>",
                "parse_mode": "HTML",
            },
        )

    def test_script_doctype_and_external_entities_are_rejected(self) -> None:
        script = {
            "chat_id": 1,
            "text": "<script>alert(1)</script>",
            "parse_mode": "HTML",
        }
        self.assertIn("html_tag_unsupported", _codes("sendMessage", script))
        doctype = {
            "chat_id": 1,
            "text": "<!DOCTYPE html><b>x</b>",
            "parse_mode": "HTML",
        }
        self.assertIn("html_markup_unsupported", _codes("sendMessage", doctype))
        external_entity = {
            "chat_id": 1,
            "text": "<!ENTITY xxe SYSTEM \"file:///etc/passwd\"><b>x</b>",
            "parse_mode": "HTML",
        }
        self.assertIn(
            "html_markup_unsupported",
            _codes("sendMessage", external_entity),
        )
        named_external = {
            "chat_id": 1,
            "text": "a&xxe;b",
            "parse_mode": "HTML",
        }
        self.assertIn(
            "html_entity_unsupported",
            _codes("sendMessage", named_external),
        )

    def test_parse_mode_and_entities_are_exclusive(self) -> None:
        payload = {
            "chat_id": 1,
            "text": "x",
            "parse_mode": "HTML",
            "entities": [{"type": "bold", "offset": 0, "length": 1}],
        }
        self.assertIn(
            "parse_mode_entities_conflict",
            _codes("sendMessage", payload),
        )
        invalid_mode = {"chat_id": 1, "text": "x", "parse_mode": "html"}
        self.assertIn("invalid_parse_mode", _codes("sendMessage", invalid_mode))

    def test_markdown_code_spans_and_legacy_literals(self) -> None:
        for mode in ("MarkdownV2", "Markdown"):
            with self.subTest(mode=mode):
                _assert_valid(
                    "sendMessage",
                    {"chat_id": 1, "text": "`*`", "parse_mode": mode},
                )
        for literal in (">", "|||", "~~~"):
            with self.subTest(literal=literal):
                _assert_valid(
                    "sendMessage",
                    {"chat_id": 1, "text": literal, "parse_mode": "Markdown"},
                )
        empty_markup = {
            "chat_id": 1,
            "text": "**",
            "parse_mode": "MarkdownV2",
        }
        findings = _findings("sendMessage", empty_markup)
        self.assertEqual([f for f in findings if f["severity"] == "error"], [])
        self.assertIn("empty_text_unverified", {f["code"] for f in findings})

    def test_markdown_code_span_length_boundary(self) -> None:
        valid = {
            "chat_id": 1,
            "text": "`" + "x" * 4096 + "`",
            "parse_mode": "MarkdownV2",
        }
        _assert_valid("sendMessage", valid)
        invalid = {
            "chat_id": 1,
            "text": "`" + "x" * 4097 + "`",
            "parse_mode": "MarkdownV2",
        }
        self.assertIn("text_too_long", _codes("sendMessage", invalid))

    def test_nested_blockquote_is_rejected_through_ancestors(self) -> None:
        payload = {
            "chat_id": 1,
            "text": "<blockquote>a<b><blockquote>b</blockquote></b></blockquote>",
            "parse_mode": "HTML",
        }
        self.assertIn("html_nesting", _codes("sendMessage", payload))
        _assert_valid(
            "sendMessage",
            {
                "chat_id": 1,
                "text": "<blockquote>a<b>bold</b></blockquote>",
                "parse_mode": "HTML",
            },
        )


class EntityTests(unittest.TestCase):
    def test_utf16_offsets_and_surrogate_boundaries(self) -> None:
        payload = {
            "chat_id": 1,
            "text": "😀 hi",
            "entities": [{"type": "bold", "offset": 0, "length": 2}],
        }
        _assert_valid("sendMessage", payload)
        split = {
            "chat_id": 1,
            "text": "😀 hi",
            "entities": [{"type": "bold", "offset": 0, "length": 1}],
        }
        self.assertIn("entity_utf16_split", _codes("sendMessage", split))
        out_of_bounds = {
            "chat_id": 1,
            "text": "😀 hi",
            "entities": [{"type": "bold", "offset": 4, "length": 2}],
        }
        self.assertIn("entity_bounds", _codes("sendMessage", out_of_bounds))

    def test_entity_types_and_required_fields(self) -> None:
        unknown_type = {
            "chat_id": 1,
            "text": "hello",
            "entities": [{"type": "rainbow", "offset": 0, "length": 2}],
        }
        self.assertIn(
            "entity_type_unsupported",
            _codes("sendMessage", unknown_type),
        )
        missing_url = {
            "chat_id": 1,
            "text": "hello",
            "entities": [{"type": "text_link", "offset": 0, "length": 2}],
        }
        self.assertIn(
            "entity_field_required",
            _codes("sendMessage", missing_url),
        )
        dangerous_url = {
            "chat_id": 1,
            "text": "hello",
            "entities": [
                {
                    "type": "text_link",
                    "offset": 0,
                    "length": 2,
                    "url": "javascript:alert(1)",
                }
            ],
        }
        self.assertIn("invalid_url", _codes("sendMessage", dangerous_url))
        bad_emoji = {
            "chat_id": 1,
            "text": "hello",
            "entities": [{"type": "custom_emoji", "offset": 0, "length": 2}],
        }
        self.assertIn(
            "entity_field_required",
            _codes("sendMessage", bad_emoji),
        )
        bad_date = {
            "chat_id": 1,
            "text": "hello",
            "entities": [
                {
                    "type": "date_time",
                    "offset": 0,
                    "length": 2,
                    "unix_time": 1647531900,
                    "date_time_format": "rd",
                }
            ],
        }
        self.assertIn("invalid_value", _codes("sendMessage", bad_date))

    def test_entity_nesting_and_bool_offsets(self) -> None:
        code_nested = {
            "chat_id": 1,
            "text": "hello",
            "entities": [
                {"type": "code", "offset": 0, "length": 5},
                {"type": "bold", "offset": 1, "length": 2},
            ],
        }
        self.assertIn("entity_code_nesting", _codes("sendMessage", code_nested))
        bool_offset = {
            "chat_id": 1,
            "text": "hello",
            "entities": [{"type": "bold", "offset": True, "length": 2}],
        }
        self.assertIn("wrong_type", _codes("sendMessage", bool_offset))
        link_outside = {
            "chat_id": 1,
            "text": "hello",
            "entities": [
                {
                    "type": "text_link",
                    "offset": 0,
                    "length": 5,
                    "url": "https://example.com",
                },
                {"type": "bold", "offset": 1, "length": 2},
            ],
        }
        _assert_valid("sendMessage", link_outside)

    def test_entity_nesting_large_duplicate_set_is_bounded(self) -> None:
        entities = [
            {"type": "bold", "offset": 0, "length": 1}
            for _ in range(10000)
        ]
        payload = {"chat_id": 1, "text": "x", "entities": entities}
        started = time.perf_counter()
        findings = _findings("sendMessage", payload)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0)
        self.assertFalse(
            [f for f in findings if f["severity"] == "error"],
            "duplicate formatting is a warning, not an invented entity-count error",
        )
        self.assertIn(
            "entity_duplicate_range",
            {f["code"] for f in findings},
        )


class ReplyParametersTests(unittest.TestCase):
    @staticmethod
    def _payload(reply: dict[str, object], **extra: object) -> dict[str, object]:
        return {
            "chat_id": 1,
            "text": "x",
            "reply_parameters": reply,
            **extra,
        }

    def test_reply_target_is_required_and_exclusive(self) -> None:
        self.assertIn(
            "reply_target_required",
            _codes("sendMessage", self._payload({})),
        )
        self.assertIn(
            "reply_target_conflict",
            _codes(
                "sendMessage",
                self._payload({"message_id": 1, "ephemeral_message_id": 2}),
            ),
        )
        _assert_valid("sendMessage", self._payload({"message_id": 1}))

    def test_reply_field_types_and_unknown_keys(self) -> None:
        cases = [
            ({"message_id": 1, "quote_parse_mode": 5}, "wrong_type"),
            ({"message_id": 1, "quote_entities": "not-list"}, "wrong_type"),
            ({"message_id": 1, "quote_position": "x"}, "wrong_type"),
            ({"message_id": 1, "allow_sending_without_reply": 1}, "wrong_type"),
            ({"message_id": 1, "checklist_task_id": True}, "wrong_type"),
            ({"message_id": 1, "poll_option_id": 5}, "invalid_value"),
            ({"message_id": 1, "ticket": "x"}, "unknown_field"),
        ]
        for reply, code in cases:
            with self.subTest(reply=reply):
                self.assertIn(code, _codes("sendMessage", self._payload(reply)))

    def test_reply_chat_id_uses_request_chat_rules(self) -> None:
        for chat_id in ("@valid_name", "123", "-1001234567890"):
            with self.subTest(chat_id=chat_id):
                _assert_valid(
                    "sendMessage",
                    self._payload({"message_id": 1, "chat_id": chat_id}),
                )
        for chat_id in ("abc", "@abc", "@bad name"):
            with self.subTest(chat_id=chat_id):
                self.assertIn(
                    "invalid_chat_username",
                    _codes(
                        "sendMessage",
                        self._payload({"message_id": 1, "chat_id": chat_id}),
                    ),
                )

    def test_quote_length_parsing_and_entities(self) -> None:
        _assert_valid(
            "sendMessage",
            self._payload({"message_id": 1, "quote": "x" * 1024}),
        )
        self.assertIn(
            "quote_too_long",
            _codes(
                "sendMessage",
                self._payload({"message_id": 1, "quote": "x" * 1025}),
            ),
        )
        _assert_valid(
            "sendMessage",
            self._payload(
                {
                    "message_id": 1,
                    "quote": "<b>" + "x" * 1024 + "</b>",
                    "quote_parse_mode": "HTML",
                }
            ),
        )
        self.assertIn(
            "parse_mode_entities_conflict",
            _codes(
                "sendMessage",
                self._payload(
                    {
                        "message_id": 1,
                        "quote": "x",
                        "quote_parse_mode": "HTML",
                        "quote_entities": [],
                    }
                ),
            ),
        )
        _assert_valid(
            "sendMessage",
            self._payload(
                {
                    "message_id": 1,
                    "quote": "x",
                    "quote_entities": [
                        {"type": "bold", "offset": 0, "length": 1}
                    ],
                }
            ),
        )
        self.assertIn(
            "entity_bounds",
            _codes(
                "sendMessage",
                self._payload(
                    {
                        "message_id": 1,
                        "quote": "x",
                        "quote_entities": [
                            {"type": "bold", "offset": 0, "length": 2}
                        ],
                    }
                ),
            ),
        )

    def test_ephemeral_reply_relations(self) -> None:
        missing = self._payload({"ephemeral_message_id": 5})
        self.assertIn(
            "ephemeral_reply_requires_ephemeral",
            _codes("sendMessage", missing),
        )
        ephemeral = {"receiver_user_id": 123}
        valid = self._payload(
            {"ephemeral_message_id": 5},
            ephemeral_message_parameters=ephemeral,
        )
        _assert_valid("sendMessage", valid)
        with_chat = self._payload(
            {"ephemeral_message_id": 5, "chat_id": "@valid_name"},
            ephemeral_message_parameters=ephemeral,
        )
        self.assertIn(
            "reply_chat_not_supported",
            _codes("sendMessage", with_chat),
        )
        with_allow = self._payload(
            {"ephemeral_message_id": 5, "allow_sending_without_reply": True},
            ephemeral_message_parameters=ephemeral,
        )
        self.assertIn(
            "reply_allow_sending_conflict",
            _codes("sendMessage", with_allow),
        )
        with_ignored_quote = self._payload(
            {"ephemeral_message_id": 5, "quote": "x" * 1025},
            ephemeral_message_parameters=ephemeral,
        )
        _assert_valid("sendMessage", with_ignored_quote)
        another_chat = self._payload(
            {
                "message_id": 1,
                "chat_id": "@valid_name",
                "allow_sending_without_reply": True,
            }
        )
        self.assertIn(
            "reply_allow_sending_conflict",
            _codes("sendMessage", another_chat),
        )

    def test_reply_chat_context_restrictions(self) -> None:
        business = self._payload(
            {"message_id": 1, "chat_id": "@valid_name"}
        )
        business["business_connection_id"] = "bc-1"
        self.assertIn(
            "reply_chat_not_supported",
            _codes("sendMessage", business),
        )
        direct = self._payload({"message_id": 1, "chat_id": "@valid_name"})
        direct["direct_messages_topic_id"] = 1
        self.assertIn(
            "reply_chat_not_supported",
            _codes("sendMessage", direct),
        )

    def test_quote_position_and_option_fields(self) -> None:
        self.assertIn(
            "invalid_value",
            _codes(
                "sendMessage",
                self._payload({"message_id": 1, "quote_position": -1}),
            ),
        )
        self.assertIn(
            "invalid_value",
            _codes(
                "sendMessage",
                self._payload({"message_id": 1, "poll_option_id": ""}),
            ),
        )
        _assert_valid(
            "sendMessage",
            self._payload(
                {
                    "message_id": 1,
                    "quote_position": 0,
                    "checklist_task_id": 10,
                    "poll_option_id": "option-1",
                }
            ),
        )


class RichMessageTests(unittest.TestCase):
    def test_rich_html_12000_characters_is_valid(self) -> None:
        html = "<p>" + "a" * 12000 + "</p>"
        findings = _findings(
            "sendRichMessage",
            {"chat_id": 1, "rich_message": {"html": html}},
        )
        self.assertEqual([f for f in findings if f["severity"] == "error"], [])
        self.assertIn("rich_html_unverified", {f["code"] for f in findings})

    def test_rich_text_32768_boundary(self) -> None:
        good = {"chat_id": 1, "rich_message": {"html": "a" * 32768}}
        self.assertEqual(_errors("sendRichMessage", good), [])
        bad = {"chat_id": 1, "rich_message": {"html": "a" * 32769}}
        self.assertIn("rich_text_too_long", _codes("sendRichMessage", bad))

    def test_rich_modes_are_exactly_one(self) -> None:
        no_mode = {"chat_id": 1, "rich_message": {}}
        self.assertIn("rich_mode", _codes("sendRichMessage", no_mode))
        two_modes = {
            "chat_id": 1,
            "rich_message": {"html": "<p>x</p>", "markdown": "x"},
        }
        self.assertIn("rich_mode", _codes("sendRichMessage", two_modes))

    def test_rich_unknown_fields_are_rejected(self) -> None:
        payload = {
            "chat_id": 1,
            "rich_message": {"html": "<p>x</p>", "format": "html"},
        }
        self.assertIn("unknown_field", _codes("sendRichMessage", payload))

    def test_rich_table_columns_and_media_limits(self) -> None:
        twenty = (
            "<table><tr>"
            + "".join("<td>x</td>" for _ in range(20))
            + "</tr></table>"
        )
        self.assertEqual(
            _errors("sendRichMessage", {"chat_id": 1, "rich_message": {"html": twenty}}),
            [],
        )
        twenty_one = (
            "<table><tr>"
            + "".join("<td>x</td>" for _ in range(21))
            + "</tr></table>"
        )
        self.assertIn(
            "rich_table_columns",
            _codes(
                "sendRichMessage",
                {"chat_id": 1, "rich_message": {"html": twenty_one}},
            ),
        )
        many_images = "".join(
            '<img src="https://example.com/x.png"/>' for _ in range(51)
        )
        self.assertIn(
            "rich_media_limit",
            _codes(
                "sendRichMessage",
                {"chat_id": 1, "rich_message": {"html": many_images}},
            ),
        )

    def test_rich_media_and_anchors(self) -> None:
        resolved = {
            "chat_id": 1,
            "rich_message": {
                "html": '<p><a href="#chapter-1">Go</a></p><h2><a name="chapter-1"></a>Chapter</h2>'
                '<img src="https://example.com/x.png"/>',
            },
        }
        _assert_valid("sendRichMessage", resolved)
        missing_anchor = {
            "chat_id": 1,
            "rich_message": {"html": '<p><a href="#missing">Go</a></p>'},
        }
        self.assertIn(
            "rich_anchor_unresolved",
            _codes("sendRichMessage", missing_anchor),
        )
        unresolved_media = {
            "chat_id": 1,
            "rich_message": {
                "html": '<img src="tg://photo?id=cover"/>',
            },
        }
        self.assertIn(
            "rich_media_id_missing",
            _codes("sendRichMessage", unresolved_media),
        )
        media_entry = {
            "id": "cover",
            "media": {
                "type": "photo",
                "media": "https://example.com/cover.jpg",
            },
        }
        resolved_media = {
            "chat_id": 1,
            "rich_message": {
                "html": '<img src="tg://photo?id=cover"/>',
                "media": [media_entry],
            },
        }
        _assert_valid("sendRichMessage", resolved_media)

    def test_rich_supported_and_unsupported_markup(self) -> None:
        good = {
            "chat_id": 1,
            "rich_message": {
                "html": "<h1>Title</h1><p>Text &nbsp; &hellip;</p>"
                '<details open><summary>More</summary><p>Body</p></details>'
                '<tg-button-row align="center">'
                '<tg-button type="callback_data" data="ok">OK</tg-button>'
                "</tg-button-row>"
            },
        }
        _assert_valid("sendRichMessage", good)
        script = {
            "chat_id": 1,
            "rich_message": {"html": "<script>alert(1)</script>"},
        }
        self.assertIn("html_tag_unsupported", _codes("sendRichMessage", script))
        span = {
            "chat_id": 1,
            "rich_message": {"html": '<span class="tg-spoiler">x</span>'},
        }
        self.assertIn("html_tag_unsupported", _codes("sendRichMessage", span))

    def test_rich_buttons_and_styles(self) -> None:
        callback_link = {
            "chat_id": 1,
            "rich_message": {
                "html": '<p><tg-button type="callback_data" style="link" data="x">x</tg-button></p>'
            },
        }
        _assert_valid("sendRichMessage", callback_link)
        url_link = {
            "chat_id": 1,
            "rich_message": {
                "html": '<p><tg-button type="url" style="link" url="https://t.me">x</tg-button></p>'
            },
        }
        self.assertIn(
            "rich_button_style",
            _codes("sendRichMessage", url_link),
        )
        missing_action = {
            "chat_id": 1,
            "rich_message": {"html": '<p><tg-button type="url">x</tg-button></p>'},
        }
        self.assertIn(
            "rich_button_action",
            _codes("sendRichMessage", missing_action),
        )
        big_callback = {
            "chat_id": 1,
            "rich_message": {
                "html": '<p><tg-button type="callback_data" data="'
                + "😀" * 17
                + '">x</tg-button></p>'
            },
        }
        self.assertIn(
            "callback_data_length",
            _codes("sendRichMessage", big_callback),
        )

    def test_rich_markdown_and_blocks_warn_but_do_not_fake_full_validation(self) -> None:
        markdown = {
            "chat_id": 1,
            "rich_message": {"markdown": "**bold** " + "a" * 12000},
        }
        findings = _findings("sendRichMessage", markdown)
        self.assertEqual([f for f in findings if f["severity"] == "error"], [])
        self.assertIn("rich_markdown_unverified", {f["code"] for f in findings})
        blocks = {
            "chat_id": 1,
            "rich_message": {
                "blocks": [{"type": "paragraph", "text": "hello"}]
            },
        }
        findings = _findings("sendRichMessage", blocks)
        self.assertEqual([f for f in findings if f["severity"] == "error"], [])
        self.assertIn("rich_blocks_unverified", {f["code"] for f in findings})


    def test_rich_content_model_media_and_richtext_only(self) -> None:
        valid_media = [
            "<details><summary>Details</summary><img src=\"https://e/x.png\"/></details>",
            "<ul><li><img src=\"https://e/x.png\"/></li></ul>",
            "<blockquote><img src=\"https://e/x.png\"/></blockquote>",
            "<details><summary>Details</summary><p>Body</p></details>",
            "<blockquote><p>Body</p></blockquote>",
        ]
        for html in valid_media:
            with self.subTest(html=html):
                _assert_valid(
                    "sendRichMessage",
                    {"chat_id": 1, "rich_message": {"html": html}},
                )

        invalid_blocks = [
            "<h1><p>x</p></h1>",
            "<aside><p>x</p></aside>",
            "<figure><img src=\"https://e/x.png\"/><figcaption><p>x</p></figcaption></figure>",
        ]
        for html in invalid_blocks:
            with self.subTest(html=html):
                self.assertIn(
                    "html_nesting",
                    _codes(
                        "sendRichMessage",
                        {"chat_id": 1, "rich_message": {"html": html}},
                    ),
                )

    def test_media_duplicate_ids_are_reported(self) -> None:
        media_entry = {
            "id": "cover",
            "media": {"type": "photo", "media": "https://e/x.png"},
        }
        payload = {
            "chat_id": 1,
            "rich_message": {
                "html": '<img src="tg://photo?id=cover"/>',
                "media": [media_entry, dict(media_entry)],
            },
        }
        self.assertIn(
            "invalid_value",
            _codes("sendRichMessage", payload),
        )

    def test_anchor_and_entity_names_are_not_echoed(self) -> None:
        anchor_payload = {
            "chat_id": 1,
            "rich_message": {
                "html": '<a name="SECRET-ANCHOR-123"></a>'
                '<a name="SECRET-ANCHOR-123"></a>'
            },
        }
        findings = _findings("sendRichMessage", anchor_payload)
        rendered = "\n".join(
            f["path"] + " " + f["message"] for f in findings
        )
        self.assertIn(
            "html_attribute_duplicate",
            {f["code"] for f in findings},
        )
        self.assertNotIn("SECRET-ANCHOR-123", rendered)

        injected_payload = {
            "chat_id": 1,
            "rich_message": {
                "html": '<a name="SECRET\nANCHOR"></a>'
                '<a name="SECRET\nANCHOR"></a>'
            },
        }
        injected_findings = _findings("sendRichMessage", injected_payload)
        injected_rendered = "\n".join(
            f["path"] + " " + f["message"] for f in injected_findings
        )
        self.assertNotIn("SECRET", injected_rendered)
        self.assertTrue(
            all("\n" not in f["message"] for f in injected_findings)
        )

        entity_payload = {
            "chat_id": 1,
            "text": "a&SECRET-ENTITY;b",
            "parse_mode": "HTML",
        }
        findings = _findings("sendMessage", entity_payload)
        codes = {f["code"] for f in findings}
        rendered = "\n".join(
            f["path"] + " " + f["message"] for f in findings
        )
        self.assertTrue(
            {"html_entity_unsupported", "html_unescaped_ampersand"} & codes
        )
        self.assertNotIn("SECRET-ENTITY", rendered)
        self.assertTrue(all("\n" not in f["message"] for f in findings))


class KeyboardTests(unittest.TestCase):
    def test_callback_byte_boundary(self) -> None:
        boundary = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "ok", "callback_data": "a" * 64}]
                ]
            },
        }
        _assert_valid("sendMessage", boundary)
        too_long = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "ok", "callback_data": "a" * 65}]
                ]
            },
        }
        self.assertIn("callback_data_length", _codes("sendMessage", too_long))
        emoji_64 = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "ok", "callback_data": "😀" * 16}]
                ]
            },
        }
        _assert_valid("sendMessage", emoji_64)
        emoji_over = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "ok", "callback_data": "😀" * 17}]
                ]
            },
        }
        self.assertIn("callback_data_length", _codes("sendMessage", emoji_over))

    def test_exactly_one_action_excluding_style_and_icon(self) -> None:
        valid = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "ok",
                            "callback_data": "x",
                            "style": "primary",
                            "icon_custom_emoji_id": "123",
                        }
                    ]
                ]
            },
        }
        _assert_valid("sendMessage", valid)
        two_actions = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "bad",
                            "url": "https://example.com",
                            "callback_data": "x",
                        }
                    ]
                ]
            },
        }
        self.assertIn("button_action_count", _codes("sendMessage", two_actions))
        no_action = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [[{"text": "bad"}]]
            },
        }
        self.assertIn("button_action_count", _codes("sendMessage", no_action))
        style_only = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [[{"text": "bad", "style": "primary"}]]
            },
        }
        self.assertIn("button_action_count", _codes("sendMessage", style_only))

    def test_inline_button_style_and_url_rules(self) -> None:
        link_style = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "x", "callback_data": "x", "style": "link"}]
                ]
            },
        }
        self.assertIn("button_style", _codes("sendMessage", link_style))
        js_url = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "x", "url": "javascript:alert(1)"}]
                ]
            },
        }
        self.assertIn("invalid_url", _codes("sendMessage", js_url))
        web_app_in_ephemeral = {
            "chat_id": -1001234567890,
            "text": "x",
            "ephemeral_message_parameters": {"receiver_user_id": 1},
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "x",
                            "web_app": {"url": "https://example.com/app"},
                        }
                    ]
                ]
            },
        }
        self.assertIn(
            "button_action_not_supported",
            _codes("sendMessage", web_app_in_ephemeral),
        )
        login_in_ephemeral = {
            "chat_id": -1001234567890,
            "text": "x",
            "ephemeral_message_parameters": {"receiver_user_id": 1},
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "x",
                            "login_url": {"url": "https://example.com/login"},
                        }
                    ]
                ]
            },
        }
        self.assertIn(
            "button_action_not_supported",
            _codes("sendMessage", login_in_ephemeral),
        )

    def test_keyboard_shapes(self) -> None:
        empty_rows = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {"inline_keyboard": []},
        }
        self.assertIn("keyboard_shape", _codes("sendMessage", empty_rows))
        row_not_array = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {"inline_keyboard": [{"text": "x"}]},
        }
        self.assertIn("keyboard_shape", _codes("sendMessage", row_not_array))
        empty_row = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {"inline_keyboard": [[]]},
        }
        self.assertIn("keyboard_shape", _codes("sendMessage", empty_row))
        mixed_kind = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [[{"text": "x", "callback_data": "x"}]],
                "keyboard": [["x"]],
            },
        }
        self.assertIn("reply_markup_kind", _codes("sendMessage", mixed_kind))

    def test_reply_keyboard_and_edit_restriction(self) -> None:
        reply_keyboard = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "keyboard": [["One"], [{"text": "Two", "request_contact": True}]],
                "resize_keyboard": True,
            },
        }
        _assert_valid("sendMessage", reply_keyboard)
        edit_with_reply = {
            "chat_id": 1,
            "message_id": 10,
            "text": "x",
            "reply_markup": {"keyboard": [["One"]]},
        }
        self.assertIn(
            "reply_markup_not_allowed",
            _codes("editMessageText", edit_with_reply),
        )
        two_reply_actions = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {
                "keyboard": [
                    [
                        {
                            "text": "x",
                            "request_contact": True,
                            "request_location": True,
                        }
                    ]
                ]
            },
        }
        self.assertIn(
            "button_action_count",
            _codes("sendMessage", two_reply_actions),
        )

    def test_business_context_restrictions(self) -> None:
        business_switch = {
            "chat_id": 1,
            "business_connection_id": "bc-1",
            "text": "x",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "x", "switch_inline_query": "q"}]
                ]
            },
        }
        self.assertIn(
            "button_action_not_supported",
            _codes("sendMessage", business_switch),
        )
        business_reply_keyboard = {
            "chat_id": 1,
            "business_connection_id": "bc-1",
            "text": "x",
            "reply_markup": {"keyboard": [["x"]]},
        }
        self.assertIn(
            "reply_markup_not_allowed",
            _codes("sendMessage", business_reply_keyboard),
        )


class EphemeralTests(unittest.TestCase):
    def test_send_uses_nested_parameters_and_edits_use_top_level_fields(self) -> None:
        nested = {
            "chat_id": -1001234567890,
            "text": "x",
            "ephemeral_message_parameters": {
                "receiver_user_id": 123,
                "callback_query_id": "cbq",
            },
        }
        _assert_valid("sendMessage", nested)
        legacy = {
            "chat_id": -1001234567890,
            "text": "x",
            "receiver_user_id": 123,
        }
        self.assertIn("legacy_ephemeral_field", _codes("sendMessage", legacy))
        edit = {
            "chat_id": -1001234567890,
            "receiver_user_id": 123,
            "ephemeral_message_id": 456,
            "text": "x",
        }
        _assert_valid("editEphemeralMessageText", edit)
        edit_nested = {
            "chat_id": -1001234567890,
            "receiver_user_id": 123,
            "ephemeral_message_id": 456,
            "text": "x",
            "ephemeral_message_parameters": {"receiver_user_id": 123},
        }
        self.assertIn(
            "ephemeral_shape_mismatch",
            _codes("editEphemeralMessageText", edit_nested),
        )

    def test_ephemeral_required_fields_and_ids(self) -> None:
        missing = {"chat_id": -1001234567890, "text": "x"}
        codes = _codes("editEphemeralMessageText", missing)
        self.assertIn("missing_field", codes)
        bad_receiver = {
            "chat_id": -1001234567890,
            "receiver_user_id": True,
            "ephemeral_message_id": 456,
            "text": "x",
        }
        self.assertIn("wrong_type", _codes("editEphemeralMessageText", bad_receiver))
        missing_receiver = {
            "chat_id": 1,
            "text": "x",
            "ephemeral_message_parameters": {"callback_query_id": "cbq"},
        }
        self.assertIn(
            "missing_field",
            _codes("sendMessage", missing_receiver),
        )
        self.assertIn(
            "missing_field",
            _codes("sendMessage", {"chat_id": 1, "text": "x", "ephemeral_message_parameters": {}}),
        )

    def test_ephemeral_target_warning(self) -> None:
        payload = {
            "chat_id": -1001234567890,
            "text": "x",
            "ephemeral_message_parameters": {"receiver_user_id": 123},
        }
        codes = _codes("sendMessage", payload)
        self.assertIn("ephemeral_target_unverified", codes)


class CallbackAnswerTests(unittest.TestCase):
    def test_callback_answer_limits_and_types(self) -> None:
        _assert_valid(
            "answerCallbackQuery",
            {"callback_query_id": "cbq", "text": "x" * 200, "cache_time": 0},
        )
        self.assertIn(
            "callback_text_too_long",
            _codes(
                "answerCallbackQuery",
                {"callback_query_id": "cbq", "text": "x" * 201},
            ),
        )
        self.assertIn(
            "wrong_type",
            _codes(
                "answerCallbackQuery",
                {"callback_query_id": True, "text": "x"},
            ),
        )
        self.assertIn(
            "invalid_url",
            _codes(
                "answerCallbackQuery",
                {"callback_query_id": "cbq", "url": "javascript:alert(1)"},
            ),
        )
        self.assertIn(
            "wrong_type",
            _codes(
                "answerCallbackQuery",
                {"callback_query_id": "cbq", "cache_time": True},
            ),
        )


class CLITests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def _write(self, directory: Path, name: str, content: str) -> Path:
        path = directory / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_valid_payload_exit_zero_and_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp),
                "valid.json",
                json.dumps({"chat_id": 123, "text": "hello"}),
            )
            result = self._run("--method", "sendMessage", "--file", str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK:", result.stdout)
            json_result = self._run(
                "--method", "sendMessage", "--file", str(path), "--json"
            )
            self.assertEqual(json_result.returncode, 0, json_result.stderr)
            self.assertEqual(json.loads(json_result.stdout), [])
            case_result = self._run(
                "--method", "SendMessage", "--file", str(path), "--json"
            )
            self.assertEqual(case_result.returncode, 0, case_result.stderr)
            self.assertEqual(json.loads(case_result.stdout), [])

    def test_findings_cap_forces_exit_one(self) -> None:
        buttons = [
            {
                "text": "warning",
                "web_app": {"url": "https://example.com/app"},
            }
            for _ in range(200)
        ]
        buttons.append({"text": "error", "callback_data": "x" * 65})
        payload = {
            "chat_id": 1,
            "text": "x",
            "reply_markup": {"inline_keyboard": [buttons]},
        }
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp),
                "capped.json",
                json.dumps(payload),
            )
            result = self._run(
                "--method", "sendMessage", "--file", str(path), "--json"
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            findings = json.loads(result.stdout)
            self.assertTrue(
                [f for f in findings if f["severity"] == "error"]
            )

    def test_invalid_payload_exit_one_and_secret_is_not_echoed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            secret = "SECRET-" + "x" * 5000
            path = self._write(
                Path(temp),
                "invalid.json",
                json.dumps({"chat_id": 123, "text": secret}),
            )
            result = self._run(
                "--method", "sendMessage", "--file", str(path), "--json"
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            findings = json.loads(result.stdout)
            self.assertIn("text_too_long", {item["code"] for item in findings})
            self.assertNotIn("SECRET", result.stdout)
            self.assertNotIn("SECRET", result.stderr)

    def test_malformed_json_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(
                Path(temp),
                "broken.json",
                '{"text": "SECRET-not-to-print"',
            )
            result = self._run("--method", "sendMessage", "--file", str(path))
            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid JSON", result.stderr)
            self.assertNotIn("SECRET", result.stdout)
            self.assertNotIn("SECRET", result.stderr)

    def test_unknown_method_and_missing_file_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            secret_file = self._write(
                Path(temp),
                "secret.json",
                json.dumps({"chat_id": 123, "text": "SECRET-unknown-method"}),
            )
            unknown = self._run("--method", "sendDocument", "--file", str(secret_file))
            self.assertEqual(unknown.returncode, 2)
            self.assertIn("not supported", unknown.stderr)
            self.assertNotIn("SECRET", unknown.stdout)
            self.assertNotIn("SECRET", unknown.stderr)
            missing = self._run(
                "--method", "sendMessage", "--file", str(Path(temp) / "absent.json")
            )
            self.assertEqual(missing.returncode, 2)

    def test_missing_argument_exit_two(self) -> None:
        result = self._run("--method", "sendMessage")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
