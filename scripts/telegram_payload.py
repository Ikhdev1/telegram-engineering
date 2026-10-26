#!/usr/bin/env python3
"""Offline structural validator for a bounded set of Telegram Bot API payloads.

This module does not call Telegram and does not reproduce the server parser.
It checks the Bot API 10.3 request fields, obvious type/shape errors, ordinary
HTML/Markdown limits, rich HTML tokens and the documented keyboard/ephemeral
rules that are visible in a JSON payload.  Findings are advisory: a payload
without errors here can still be rejected by Telegram.

Public interface:
    validate_request(method: str, payload: dict) -> list[dict]

CLI:
    python scripts/telegram_payload.py --method METHOD --file PATH [--json]
Exit codes: 0 no errors, 1 validation errors, 2 invocation/I/O/JSON failure.
"""
from __future__ import annotations

import argparse
import heapq
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

BOT_API_SNAPSHOT = "10.3"
MAX_FINDINGS = 200
MAX_ORDINARY_TEXT = 4096
MAX_RICH_TEXT = 32768
MAX_CALLBACK_BYTES = 64
MAX_RICH_MEDIA = 50
MAX_TABLE_COLUMNS = 20
MAX_RICH_NESTING = 16
MAX_RICH_BLOCKS = 500
MAX_QUOTE_CHARS = 1024

SUPPORTED_METHODS = (
    "sendMessage",
    "sendRichMessage",
    "editMessageText",
    "editEphemeralMessageText",
    "answerCallbackQuery",
)
CANONICAL_METHODS = {name.lower(): name for name in SUPPORTED_METHODS}


def _canonical_method(method: object) -> str | None:
    if not isinstance(method, str):
        return None
    return CANONICAL_METHODS.get(method.lower())

METHOD_FIELDS: dict[str, frozenset[str]] = {
    "sendMessage": frozenset(
        {
            "business_connection_id",
            "chat_id",
            "message_thread_id",
            "direct_messages_topic_id",
            "ephemeral_message_parameters",
            "text",
            "parse_mode",
            "entities",
            "link_preview_options",
            "disable_notification",
            "protect_content",
            "allow_paid_broadcast",
            "message_effect_id",
            "suggested_post_parameters",
            "reply_parameters",
            "reply_markup",
        }
    ),
    "sendRichMessage": frozenset(
        {
            "business_connection_id",
            "chat_id",
            "message_thread_id",
            "direct_messages_topic_id",
            "ephemeral_message_parameters",
            "rich_message",
            "disable_notification",
            "protect_content",
            "allow_paid_broadcast",
            "message_effect_id",
            "suggested_post_parameters",
            "reply_parameters",
            "reply_markup",
        }
    ),
    "editMessageText": frozenset(
        {
            "business_connection_id",
            "chat_id",
            "message_id",
            "inline_message_id",
            "text",
            "parse_mode",
            "entities",
            "link_preview_options",
            "rich_message",
            "reply_markup",
        }
    ),
    "editEphemeralMessageText": frozenset(
        {
            "chat_id",
            "receiver_user_id",
            "ephemeral_message_id",
            "text",
            "parse_mode",
            "entities",
            "rich_message",
            "link_preview_options",
            "reply_markup",
        }
    ),
    "answerCallbackQuery": frozenset(
        {"callback_query_id", "text", "show_alert", "url", "cache_time"}
    ),
}

LEGACY_SEND_EPHEMERAL_FIELDS = frozenset(
    {"receiver_user_id", "callback_query_id", "replace_callback_query_message"}
)
SEND_METHODS = frozenset({"sendMessage", "sendRichMessage"})
SEND_RICH_METHODS = frozenset({"sendRichMessage"})
EDIT_METHODS = frozenset({"editMessageText", "editEphemeralMessageText"})

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DATE_TIME_FORMAT_RE = re.compile(r"^(?:r|w?[dD]?[tT]?)$")
MEDIA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")
NUMERIC_CHAT_ID_RE = re.compile(r"^-?[0-9]+$")
RICH_MEDIA_REF_RE = re.compile(
    r"tg://(?:photo|video|document|audio)\?id=([A-Za-z0-9_-]+)"
)

ENTITY_TYPES = frozenset(
    {
        "mention",
        "hashtag",
        "cashtag",
        "bot_command",
        "url",
        "email",
        "phone_number",
        "bold",
        "italic",
        "underline",
        "strikethrough",
        "spoiler",
        "blockquote",
        "expandable_blockquote",
        "code",
        "pre",
        "text_link",
        "text_mention",
        "custom_emoji",
        "date_time",
    }
)
ENTITY_COMMON_FIELDS = frozenset({"type", "offset", "length"})
ENTITY_FIELDS = frozenset(
    {
        "type",
        "offset",
        "length",
        "url",
        "user",
        "language",
        "custom_emoji_id",
        "unix_time",
        "date_time_format",
    }
)
ENTITY_FORMATTING = frozenset(
    {"bold", "italic", "underline", "strikethrough", "spoiler"}
)
ENTITY_CODE = frozenset({"code", "pre"})
ENTITY_QUOTE = frozenset({"blockquote", "expandable_blockquote"})

ORDINARY_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "span",
        "tg-spoiler",
        "a",
        "code",
        "pre",
        "blockquote",
        "tg-emoji",
        "tg-time",
    }
)
ORDINARY_FORMATTING = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler"}
)
ORDINARY_NO_ATTR_TAGS = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "tg-spoiler", "pre"}
)
ORDINARY_NAMED_ENTITIES = {
    "lt": "<",
    "gt": ">",
    "amp": "&",
    "quot": '"',
}
RICH_NAMED_ENTITIES = {
    **ORDINARY_NAMED_ENTITIES,
    "apos": "'",
    "nbsp": "\u00a0",
    "hellip": "\u2026",
    "mdash": "\u2014",
    "ndash": "\u2013",
    "lsquo": "\u2018",
    "rsquo": "\u2019",
    "ldquo": "\u201c",
    "rdquo": "\u201d",
}

RICH_TAGS = frozenset(
    {
        "a",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "mark",
        "sub",
        "sup",
        "tg-spoiler",
        "tg-reference",
        "tg-emoji",
        "tg-time",
        "tg-math",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "pre",
        "footer",
        "hr",
        "ul",
        "ol",
        "li",
        "blockquote",
        "aside",
        "details",
        "summary",
        "table",
        "caption",
        "tr",
        "th",
        "td",
        "figure",
        "figcaption",
        "img",
        "video",
        "audio",
        "tg-document",
        "tg-map",
        "tg-collage",
        "tg-slideshow",
        "tg-math-block",
        "tg-button",
        "tg-button-row",
        "input",
        "cite",
    }
)
RICH_FORMATTING = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "mark",
        "sub",
        "sup",
        "tg-spoiler",
    }
)
RICH_TEXT_ONLY_TAGS = frozenset(
    {
        "a",
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "footer",
        "aside",
        "summary",
        "figcaption",
        "caption",
        "cite",
        "tg-reference",
        "tg-math",
        "tg-math-block",
    }
)
RICH_INLINE_TAGS = frozenset(
    {
        "a",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "mark",
        "sub",
        "sup",
        "tg-spoiler",
        "tg-reference",
        "tg-emoji",
        "tg-time",
        "tg-math",
        "br",
        "cite",
        "tg-button",
        "input",
    }
)
RICH_BLOCK_COUNT_TAGS = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
        "footer",
        "hr",
        "tg-math-block",
        "ul",
        "ol",
        "li",
        "blockquote",
        "aside",
        "details",
        "table",
        "tr",
        "figure",
        "tg-collage",
        "tg-slideshow",
        "tg-map",
        "tg-button-row",
        "img",
        "video",
        "audio",
        "tg-document",
    }
)
RICH_BLOCK_TAGS = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
        "footer",
        "hr",
        "ul",
        "ol",
        "li",
        "blockquote",
        "aside",
        "details",
        "table",
        "tr",
        "th",
        "td",
        "figure",
        "figcaption",
        "tg-collage",
        "tg-slideshow",
        "tg-map",
        "tg-math-block",
        "tg-button-row",
    }
)
RICH_MEDIA_TAGS = frozenset({"img", "video", "audio", "tg-document"})
RICH_VOID_TAGS = frozenset({"br", "hr", "img", "input", "tg-map"})
RICH_MEDIA_HOSTS = frozenset({"photo", "video", "document", "audio"})

RICH_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "name"}),
    "ol": frozenset({"start", "type", "reversed"}),
    "li": frozenset({"value", "type"}),
    "table": frozenset({"bordered", "striped", "compact"}),
    "th": frozenset({"colspan", "rowspan", "align", "valign"}),
    "td": frozenset({"colspan", "rowspan", "align", "valign"}),
    "details": frozenset({"open"}),
    "blockquote": frozenset({"expandable"}),
    "img": frozenset({"src", "alt", "tg-spoiler"}),
    "video": frozenset({"src", "tg-spoiler"}),
    "audio": frozenset({"src"}),
    "tg-document": frozenset({"src"}),
    "tg-map": frozenset({"lat", "long", "zoom"}),
    "tg-button-row": frozenset({"align"}),
    "tg-emoji": frozenset({"emoji-id"}),
    "tg-time": frozenset({"unix", "format"}),
    "tg-reference": frozenset({"name"}),
    "input": frozenset({"type", "checked"}),
    "tg-button": frozenset(
        {
            "type",
            "style",
            "url",
            "data",
            "query",
            "text",
            "forward-text",
            "request-write-access",
            "allow-user-chats",
            "allow-bot-chats",
            "allow-group-chats",
            "allow-channel-chats",
        }
    ),
}

INLINE_BUTTON_ACTIONS = frozenset(
    {
        "url",
        "callback_data",
        "web_app",
        "login_url",
        "switch_inline_query",
        "switch_inline_query_current_chat",
        "switch_inline_query_chosen_chat",
        "copy_text",
        "callback_game",
        "pay",
        "disabled",
    }
)
INLINE_BUTTON_FIELDS = frozenset(
    {"text", "icon_custom_emoji_id", "style"} | INLINE_BUTTON_ACTIONS
)
REPLY_BUTTON_FIELDS = frozenset(
    {
        "text",
        "icon_custom_emoji_id",
        "style",
        "request_users",
        "request_chat",
        "request_managed_bot",
        "request_contact",
        "request_location",
        "request_poll",
        "web_app",
    }
)
REPLY_BUTTON_ACTIONS = frozenset(
    {
        "request_users",
        "request_chat",
        "request_managed_bot",
        "request_contact",
        "request_location",
        "request_poll",
        "web_app",
    }
)
BUTTON_STYLES = frozenset({"danger", "success", "primary"})
RICH_BUTTON_TYPES = frozenset(
    {
        "url",
        "callback_data",
        "web_app",
        "login_url",
        "switch_inline_query",
        "switch_inline_query_current_chat",
        "switch_inline_query_chosen_chat",
        "copy_text",
        "disabled",
    }
)
RICH_BUTTON_ACTION_ATTRS = frozenset(
    {
        "url",
        "data",
        "query",
        "text",
        "forward-text",
        "request-write-access",
        "allow-user-chats",
        "allow-bot-chats",
        "allow-group-chats",
        "allow-channel-chats",
    }
)


def _finding(severity: str, code: str, path: str, message: str) -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "path": path,
        "message": message,
    }


class _Findings:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []
        self.errors_omitted = False
        self._truncation_index: int | None = None

    def _set_truncation(self, severity: str) -> None:
        message = f"Additional findings omitted after {MAX_FINDINGS} entries."
        if severity == "error" or self.errors_omitted:
            message += " At least one omitted finding is an error."
        if self._truncation_index is None:
            self.items.append(
                _finding(severity, "findings_truncated", "$", message)
            )
            self._truncation_index = len(self.items) - 1
            return
        if severity == "error":
            self.items[self._truncation_index] = _finding(
                "error", "findings_truncated", "$", message
            )

    def _add(self, severity: str, code: str, path: str, message: str) -> None:
        if len(self.items) >= MAX_FINDINGS:
            if severity == "error":
                self.errors_omitted = True
            self._set_truncation(severity)
            return
        self.items.append(_finding(severity, code, path, message))

    def error(self, code: str, path: str, message: str) -> None:
        self._add("error", code, path, message)

    def warning(self, code: str, path: str, message: str) -> None:
        self._add("warning", code, path, message)

    def force_error(self, code: str, path: str, message: str) -> None:
        finding = _finding("error", code, path, message)
        if self._truncation_index is not None:
            self.items[self._truncation_index] = finding
        else:
            self.items.append(finding)


def _join(path: str, key: str) -> str:
    if isinstance(key, str) and IDENTIFIER_RE.match(key):
        return f"{path}.{key}"
    return f"{path}[{key!r}]"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_number_text(value: str) -> bool:
    return bool(re.fullmatch(r"-?\d+", value))


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _utf16_boundaries(value: str) -> set[int]:
    positions = {0}
    position = 0
    for char in value:
        position += 1 if ord(char) <= 0xFFFF else 2
        positions.add(position)
    return positions


def _utf8_length(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _url_ok(
    value: Any,
    schemes: tuple[str, ...],
    *,
    https_only: bool = False,
    allow_fragment: bool = False,
) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if any(char.isspace() or ord(char) < 0x20 for char in value):
        return False
    if allow_fragment and value.startswith("#"):
        return len(value) > 1
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        return False
    if https_only:
        return scheme == "https" and bool(parts.netloc)
    if scheme in {"http", "https", "tg"}:
        return bool(parts.netloc)
    if scheme in {"mailto", "tel"}:
        return bool(parts.path or parts.netloc)
    return False


def _tg_media_ref(value: Any) -> str | None:
    if not isinstance(value, str) or not value.lower().startswith("tg://"):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() != "tg" or parts.netloc.lower() not in RICH_MEDIA_HOSTS:
        return None
    try:
        ids = parse_qs(parts.query, keep_blank_values=True).get("id")
    except ValueError:
        return None
    if not ids:
        return None
    return ids[0]


def _markdown_code_visible_length(value: str) -> int:
    visible = 0
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            if value[index + 1] in "`\\":
                visible += 1
                index += 2
                continue
            visible += 2
            index += 2
            continue
        visible += 1
        index += 1
    return visible


def _markdown_visible_length(value: str, mode: str) -> int:
    """Rough visible length for MarkdownV2 or legacy Markdown.

    Code spans and fenced blocks are counted as literal visible content; link
    targets and obvious delimiters are removed.  The server remains the source
    of truth for Markdown parsing and callers warn about that.  The estimate is
    never used to raise a hard empty-text error.
    """
    visible = 0
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char == "\\" and index + 1 < length:
            visible += 1
            index += 2
            continue
        if char == "`":
            run_end = index
            while run_end < length and value[run_end] == "`":
                run_end += 1
            run = run_end - index
            closing = value.find("`" * run, run_end)
            if closing == -1:
                content = value[run_end:]
                index = length
            else:
                content = value[run_end:closing]
                index = closing + run
            if run >= 3:
                newline = content.find("\n")
                if newline != -1:
                    first_line = content[:newline]
                    if not first_line or re.fullmatch(
                        r"[A-Za-z0-9_+.-]*", first_line
                    ):
                        content = content[newline + 1 :]
            visible += _markdown_code_visible_length(content)
            continue
        if char in "![":
            match = re.match(r"!?\[([^\]]*)\]\(([^)]*)\)", value[index:])
            if match:
                visible += _markdown_visible_length(match.group(1), mode)
                index += match.end()
                continue
        if mode == "MarkdownV2":
            if char in "*_~|`":
                index += 1
                continue
            if char == ">" and (index == 0 or value[index - 1] == "\n"):
                index += 1
                continue
        else:
            if char in "*_":
                index += 1
                continue
        visible += 1
        index += 1
    return visible


def _markdown_media_count(value: str) -> int:
    count = 0
    for match in re.finditer(r"!\[[^\]]*\]\(([^)]*)\)", value):
        target = match.group(1).strip()
        if target.lower().startswith("tg://emoji"):
            continue
        count += 1
    return count


class _HTMLParserBase(HTMLParser):
    mode = ""
    named_entities: dict[str, str] = {}
    void_tags: frozenset[str] = frozenset()

    def __init__(self, path: str, findings: _Findings) -> None:
        super().__init__(convert_charrefs=False)
        self.path = path
        self.findings = findings
        self.stack: list[dict[str, Any]] = []
        self.visible_len = 0
        self.tag_count = 0
        self._gt_warned = False

    def error(self, code: str, message: str) -> None:
        self.findings.error(code, self.path, message)

    def warning(self, code: str, message: str) -> None:
        self.findings.warning(code, self.path, message)

    def _add_text(self, text: str) -> None:
        if not text:
            return
        self.visible_len += len(text)
        if self.stack:
            self.stack[-1]["text_parts"].append(text)

    def handle_data(self, data: str) -> None:
        if "<" in data:
            self.error(
                "html_unescaped_lt",
                "Raw '<' in text is not allowed; escape it as &lt;.",
            )
        if "&" in data:
            self.error(
                "html_unescaped_ampersand",
                "Raw '&' in text is not allowed; escape it as &amp;.",
            )
        if ">" in data and not self._gt_warned:
            self._gt_warned = True
            self.warning(
                "html_unescaped_gt",
                "Raw '>' in text should be escaped as &gt; for the documented HTML contract.",
            )
        self._add_text(data)

    def handle_entityref(self, name: str) -> None:
        value = self.named_entities.get(name)
        if value is None:
            self.error(
                "html_entity_unsupported",
                "Named HTML entity is not in the documented allowlist.",
            )
            return
        self._add_text(value)

    def handle_charref(self, name: str) -> None:
        try:
            if name[:1].lower() == "x":
                codepoint = int(name[1:], 16)
            else:
                codepoint = int(name, 10)
        except (TypeError, ValueError):
            self.error(
                "html_entity_invalid",
                "Numeric HTML entity has an invalid code point.",
            )
            return
        if (
            codepoint <= 0
            or codepoint > 0x10FFFF
            or 0xD800 <= codepoint <= 0xDFFF
        ):
            self.error(
                "html_entity_invalid",
                "Numeric HTML entity is outside valid Unicode scalar values.",
            )
            return
        self._add_text(chr(codepoint))

    def handle_comment(self, data: str) -> None:
        self.error(
            "html_markup_unsupported",
            "HTML comments are not part of the documented Telegram markup.",
        )

    def handle_decl(self, decl: str) -> None:
        self.error(
            "html_markup_unsupported",
            "DOCTYPE/declaration markup is not part of Telegram message HTML.",
        )

    def handle_pi(self, data: str) -> None:
        self.error(
            "html_markup_unsupported",
            "Processing instructions are not part of Telegram message HTML.",
        )

    def unknown_decl(self, data: str) -> None:
        self.error(
            "html_markup_unsupported",
            "Unknown declaration markup is not part of Telegram message HTML.",
        )

    def _normalize_attrs(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for name, value in attrs:
            if name in result:
                self.error(
                    "html_attribute_duplicate",
                    f"Duplicate attribute on <{tag}>.",
                )
                continue
            result[name] = value
        return result

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag_count += 1
        self._start(tag, self._normalize_attrs(tag, attrs))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.tag_count += 1
        clean = self._normalize_attrs(tag, attrs)
        self._start(tag, clean)
        if tag not in self.void_tags:
            self._end(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.void_tags:
            self.error(
                "html_unexpected_end_tag",
                f"Unexpected closing tag </{tag}> for a void element.",
            )
            return
        self._end(tag)

    def _push(self, tag: str, attrs: dict[str, str | None]) -> None:
        self.stack.append(
            {
                "tag": tag,
                "attrs": attrs,
                "text_parts": [],
                "child_tags": [],
                "media_count": 0,
                "visual_count": 0,
                "figcaption_count": 0,
                "caption_count": 0,
                "summary_count": 0,
                "button_count": 0,
                "row_columns": 0,
                "max_columns": 0,
                "cells": 0,
                "rows": 0,
            }
        )

    def _parent_tag(self) -> str | None:
        return self.stack[-1]["tag"] if self.stack else None

    def _close_tag(self, tag: str) -> dict[str, Any] | None:
        if not self.stack:
            self.error(
                "html_unexpected_end_tag",
                f"Closing tag </{tag}> has no matching opening tag.",
            )
            return None
        if self.stack[-1]["tag"] == tag:
            frame = self.stack.pop()
        else:
            matching_index = None
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index]["tag"] == tag:
                    matching_index = index
                    break
            if matching_index is None:
                self.error(
                    "html_unexpected_end_tag",
                    f"Closing tag </{tag}> has no matching opening tag.",
                )
                return None
            self.error(
                "html_mismatched_tag",
                f"Closing tag </{tag}> does not match the innermost open tag.",
            )
            while len(self.stack) > matching_index + 1:
                frame = self.stack.pop()
                if self.stack:
                    self.stack[-1]["text_parts"].extend(frame["text_parts"])
            frame = self.stack.pop()
        if self.stack:
            self.stack[-1]["text_parts"].extend(frame["text_parts"])
        return frame

    def finish(self) -> None:
        try:
            self.close()
        except Exception:
            self.error(
                "html_parse_failure",
                "The HTML input could not be parsed locally.",
            )
        for frame in self.stack:
            self.error(
                "html_unclosed_tag",
                f"Tag <{frame['tag']}> is not closed.",
            )


class _OrdinaryHTMLParser(_HTMLParserBase):
    mode = "ordinary"
    named_entities = ORDINARY_NAMED_ENTITIES

    def _start(self, tag: str, attrs: dict[str, str | None]) -> None:
        parent = self._parent_tag()
        if tag not in ORDINARY_TAGS:
            self.error(
                "html_tag_unsupported",
                f"Tag <{tag}> is not supported in an ordinary parse_mode=HTML message.",
            )
        else:
            self._check_attributes(tag, attrs, parent)
            self._check_context(tag, parent)
        self._push(tag, attrs)

    def _check_attributes(
        self, tag: str, attrs: dict[str, str | None], parent: str | None
    ) -> None:
        names = set(attrs)
        if tag in ORDINARY_NO_ATTR_TAGS:
            if names:
                self.error(
                    "html_attribute_unknown",
                    f"Tag <{tag}> does not accept attributes here.",
                )
            return
        if tag == "a":
            self._only_attrs(tag, names, {"href"})
            href = attrs.get("href")
            if not isinstance(href, str) or not href:
                self.error(
                    "html_attribute_required",
                    "Tag <a> requires a non-empty href attribute.",
                )
            elif not _url_ok(href, ("http", "https", "tg", "mailto", "tel")):
                self.error(
                    "invalid_url",
                    "Tag <a> href must use http(s), tg://, mailto: or tel:.",
                )
            return
        if tag == "span":
            self._only_attrs(tag, names, {"class"})
            if attrs.get("class") != "tg-spoiler":
                self.error(
                    "html_attribute_value",
                    'Tag <span> is supported only with class="tg-spoiler".',
                )
            return
        if tag == "blockquote":
            self._only_attrs(tag, names, {"expandable"})
            return
        if tag == "tg-emoji":
            self._only_attrs(tag, names, {"emoji-id"})
            emoji_id = attrs.get("emoji-id")
            if not isinstance(emoji_id, str) or not emoji_id.isdigit():
                self.error(
                    "html_attribute_required",
                    "Tag <tg-emoji> requires a numeric emoji-id attribute.",
                )
            return
        if tag == "tg-time":
            self._only_attrs(tag, names, {"unix", "format"})
            unix = attrs.get("unix")
            if not isinstance(unix, str) or not _is_number_text(unix):
                self.error(
                    "html_attribute_required",
                    "Tag <tg-time> requires a numeric unix attribute.",
                )
            self._check_time_format(attrs.get("format"), "tg-time")
            return
        if tag == "code":
            if parent == "pre":
                self._only_attrs(tag, names, {"class"})
                class_value = attrs.get("class")
                if class_value is not None and not re.fullmatch(
                    r"language-[A-Za-z0-9_+#.-]+", class_value
                ):
                    self.error(
                        "html_attribute_value",
                        'A <code> inside <pre> accepts only class="language-...".',
                    )
            elif names:
                self.error(
                    "html_attribute_unknown",
                    "A standalone <code> tag does not accept attributes.",
                )
            return
        if names:
            self.error(
                "html_attribute_unknown",
                f"Tag <{tag}> does not accept attributes here.",
            )

    def _check_context(self, tag: str, parent: str | None) -> None:
        if parent in {"pre", "code"}:
            if tag == "code" and parent == "pre":
                return
            self.error(
                "html_nesting",
                f"Tag <{tag}> cannot be nested inside <{parent}> in ordinary HTML.",
            )
        if parent in {"tg-emoji", "tg-time"}:
            self.error(
                "html_nesting",
                f"Tag <{tag}> cannot be nested inside <{parent}>; its content is plain text.",
            )
        if parent == "a" and tag in {
            "a",
            "code",
            "pre",
            "blockquote",
            "tg-emoji",
            "tg-time",
        }:
            self.error(
                "html_nesting",
                f"Tag <{tag}> cannot be nested inside <a> in ordinary HTML.",
            )
        if tag in {"pre", "code"} and parent in ORDINARY_FORMATTING:
            self.error(
                "html_nesting",
                f"Tag <{tag}> cannot be nested inside formatting tags.",
            )
        if tag == "blockquote":
            if any(frame["tag"] == "blockquote" for frame in self.stack):
                self.error(
                    "html_nesting",
                    "Blockquote entities cannot be nested.",
                )
        if tag == "a":
            if any(frame["tag"] == "a" for frame in self.stack):
                self.error(
                    "html_nesting",
                    "Inline link entities cannot be nested.",
                )

    def _only_attrs(
        self, tag: str, names: set[str], allowed: set[str]
    ) -> None:
        unknown = names - allowed
        if unknown:
            self.error(
                "html_attribute_unknown",
                f"Tag <{tag}> has an unsupported attribute.",
            )

    def _check_time_format(self, value: Any, tag: str) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not DATE_TIME_FORMAT_RE.fullmatch(value):
            self.error(
                "html_attribute_value",
                f"Tag <{tag}> has an invalid format attribute; expected r or w?[dD]?[tT]?.",
            )

    def _end(self, tag: str) -> None:
        frame = self._close_tag(tag)
        if frame is None:
            return
        text = "".join(frame["text_parts"]).strip()
        if tag == "tg-emoji" and not text:
            self.error(
                "html_tag_content",
                "Tag <tg-emoji> requires an alternative emoji as text content.",
            )
        if tag == "tg-time" and not text:
            self.warning(
                "html_tag_content",
                "Tag <tg-time> has no fallback text; clients may show nothing.",
            )


class _RichHTMLParser(_HTMLParserBase):
    mode = "rich"
    named_entities = RICH_NAMED_ENTITIES
    void_tags = RICH_VOID_TAGS

    def __init__(
        self,
        path: str,
        findings: _Findings,
        *,
        ephemeral: bool,
        business: bool,
    ) -> None:
        super().__init__(path, findings)
        self.ephemeral = ephemeral
        self.business = business
        self.block_count = 0
        self.max_depth = 0
        self.media_count = 0
        self.max_columns = 0
        self.media_refs: set[str] = set()
        self.anchor_names: set[str] = set()
        self.anchor_links: set[str] = set()
        self._nesting_warned = False

    def _start(self, tag: str, attrs: dict[str, str | None]) -> None:
        parent = self._parent_tag()
        if tag not in RICH_TAGS:
            self.error(
                "html_tag_unsupported",
                f"Tag <{tag}> is not in the Bot API 10.3 rich HTML allowlist.",
            )
        else:
            self._check_context(tag, parent)
            self._check_attributes(tag, attrs, parent)
            self._track_structures(tag, attrs, parent)
        if tag in self.void_tags:
            return
        self._push(tag, attrs)
        depth = len(self.stack)
        if depth > self.max_depth:
            self.max_depth = depth
        if depth > MAX_RICH_NESTING and not self._nesting_warned:
            self._nesting_warned = True
            self.warning(
                "rich_nesting_unverified",
                f"Source HTML nesting exceeds {MAX_RICH_NESTING} levels; the server maps HTML to blocks and may count differently.",
            )

    def _check_context(self, tag: str, parent: str | None) -> None:
        if parent in {"pre", "code"}:
            if tag == "code" and parent == "pre":
                return
            self.error(
                "html_nesting",
                f"Tag <{tag}> cannot be nested inside <{parent}> in a preformatted block.",
            )
        if parent in {"tg-emoji", "tg-time"}:
            self.error(
                "html_nesting",
                f"Tag <{tag}> cannot be nested inside <{parent}>; its content is plain text.",
            )
        if tag in {"pre", "code"} and parent in RICH_FORMATTING:
            self.error(
                "html_nesting",
                f"Tag <{tag}> cannot be nested inside rich formatting tags.",
            )
        if parent == "a" and tag == "a":
            self.error(
                "html_nesting",
                f"Tag <{tag}> cannot be nested inside <a>.",
            )
        if tag == "a" and any(frame["tag"] == "a" for frame in self.stack):
            self.error("html_nesting", "Inline links cannot be nested.")
        if parent in RICH_TEXT_ONLY_TAGS and (
            tag in RICH_BLOCK_TAGS or tag in RICH_MEDIA_TAGS
        ):
            self.error(
                "html_nesting",
                f"RichText-only container <{parent}> cannot contain block/media <{tag}>.",
            )
        if parent in {"td", "th"} and tag not in RICH_INLINE_TAGS:
            self.error(
                "html_nesting",
                f"Table cells accept only inline formatting, not <{tag}>.",
            )
        if parent == "tg-button" and tag not in {"tg-emoji", "tg-time"}:
            self.error(
                "html_nesting",
                "A rich tg-button label accepts only plain text, tg-emoji and tg-time.",
            )
        if parent == "tg-button-row" and tag != "tg-button":
            self.error(
                "html_nesting",
                "A tg-button-row accepts only tg-button children.",
            )
        if parent in {"tg-collage", "tg-slideshow"} and tag not in {
            "img",
            "video",
            "audio",
            "tg-document",
            "figcaption",
        }:
            self.error(
                "html_nesting",
                f"<{parent}> accepts only media elements and figcaption children.",
            )
        if parent == "figure" and tag not in {
            "img",
            "video",
            "audio",
            "tg-document",
            "tg-map",
            "figcaption",
        }:
            self.error(
                "html_nesting",
                f"<figure> does not accept <{tag}> as a child.",
            )
        if tag in RICH_MEDIA_TAGS and parent not in {
            None,
            "details",
            "li",
            "blockquote",
            "figure",
            "tg-collage",
            "tg-slideshow",
        }:
            self.error(
                "html_nesting",
                "Images, video, audio and documents must be separate media blocks.",
            )
        if tag == "figcaption" and parent not in {
            "figure",
            "tg-collage",
            "tg-slideshow",
        }:
            self.error(
                "html_nesting",
                "<figcaption> is allowed only inside figure, tg-collage or tg-slideshow.",
            )
        if tag == "summary" and parent != "details":
            self.error("html_nesting", "<summary> is allowed only inside <details>.")
        if tag == "caption" and parent != "table":
            self.error("html_nesting", "<caption> is allowed only inside <table>.")
        if tag in {"td", "th"} and parent != "tr":
            self.error("html_nesting", f"<{tag}> is allowed only inside <tr>.")
        if tag == "tr" and parent != "table":
            self.error("html_nesting", "<tr> is allowed only inside <table>.")
        if tag == "li" and parent not in {"ul", "ol"}:
            self.error("html_nesting", "<li> is allowed only inside <ul> or <ol>.")
        if parent in {"ul", "ol"} and tag != "li":
            self.error(
                "html_nesting",
                f"<{parent}> accepts only <li> children.",
            )
        if tag == "input" and parent != "li":
            self.error("html_nesting", "<input> is allowed only inside <li>.")
        if tag == "cite" and parent not in {"blockquote", "aside", "figcaption"}:
            self.warning(
                "rich_cite_placement",
                "<cite> is normally used inside blockquote, aside or figcaption.",
            )
        if tag == "tg-button" and parent not in {"p", "tg-button-row"}:
            self.warning(
                "rich_button_placement",
                "A tg-button is normally placed inside <p> or <tg-button-row>.",
            )

    def _check_attributes(
        self, tag: str, attrs: dict[str, str | None], parent: str | None
    ) -> None:
        names = set(attrs)
        allowed = RICH_ATTRS.get(tag, frozenset())
        unknown = names - allowed
        if unknown:
            self.error(
                "html_attribute_unknown",
                f"Tag <{tag}> has an attribute outside the Bot API 10.3 allowlist.",
            )
        if tag == "a":
            href = attrs.get("href")
            name = attrs.get("name")
            if (href is None) == (name is None):
                self.error(
                    "html_attribute_required",
                    'Tag <a> requires exactly one of href or name.',
                )
            if href is not None:
                if not isinstance(href, str) or not href:
                    self.error(
                        "html_attribute_required",
                        "Tag <a> has an empty href.",
                    )
                elif href.startswith("#"):
                    if len(href) == 1:
                        self.error(
                            "html_attribute_value",
                            "Tag <a> has an empty in-document reference.",
                        )
                    else:
                        self.anchor_links.add(href[1:])
                elif not _url_ok(
                    href, ("http", "https", "tg", "mailto", "tel")
                ):
                    self.error(
                        "invalid_url",
                        "Tag <a> href must use http(s), tg://, mailto:, tel: or #anchor.",
                    )
            if name is not None:
                if not isinstance(name, str) or not name or name.startswith("#"):
                    self.error(
                        "html_attribute_value",
                        "Tag <a> name must be a non-empty anchor name.",
                    )
                else:
                    if name in self.anchor_names:
                        self.error(
                            "html_attribute_duplicate",
                            "Duplicate rich anchor name.",
                        )
                    self.anchor_names.add(name)
            return
        if tag == "tg-reference":
            name = attrs.get("name")
            if not isinstance(name, str) or not name:
                self.error(
                    "html_attribute_required",
                    "Tag <tg-reference> requires a non-empty name attribute.",
                )
            else:
                if name in self.anchor_names:
                    self.error(
                        "html_attribute_duplicate",
                        "Duplicate rich anchor name.",
                    )
                self.anchor_names.add(name)
            return
        if tag == "img":
            self._check_media_src(attrs.get("src"), tag, allow_emoji=True)
            return
        if tag in {"video", "audio", "tg-document"}:
            self._check_media_src(attrs.get("src"), tag, allow_emoji=False)
            return
        if tag == "tg-map":
            for key in ("lat", "long", "zoom"):
                value = attrs.get(key)
                if value is None:
                    self.error(
                        "html_attribute_required",
                        f"Tag <tg-map> requires the {key} attribute.",
                    )
            self._check_numeric_attr(attrs.get("lat"), "tg-map", "lat", float)
            self._check_numeric_attr(attrs.get("long"), "tg-map", "long", float)
            self._check_numeric_attr(attrs.get("zoom"), "tg-map", "zoom", int)
            lat = self._parse_number(attrs.get("lat"), float)
            long = self._parse_number(attrs.get("long"), float)
            if lat is not None and not -90 <= lat <= 90:
                self.error(
                    "html_attribute_value",
                    "Tag <tg-map> latitude must be between -90 and 90.",
                )
            if long is not None and not -180 <= long <= 180:
                self.error(
                    "html_attribute_value",
                    "Tag <tg-map> longitude must be between -180 and 180.",
                )
            return
        if tag in {"th", "td"}:
            for key in ("colspan", "rowspan"):
                value = attrs.get(key)
                if value is not None:
                    self._check_positive_int_attr(value, tag, key)
            self._check_enum_attr(
                attrs.get("align"), "left", "center", "right", tag=tag
            )
            self._check_enum_attr(
                attrs.get("valign"), "top", "middle", "bottom", tag=tag
            )
            return
        if tag == "ol":
            if attrs.get("start") is not None:
                self._check_numeric_attr(
                    attrs.get("start"), "ol", "start", int
                )
            self._check_enum_attr(attrs.get("type"), "1", "a", "A", "i", "I", tag=tag)
            return
        if tag == "li":
            if attrs.get("value") is not None:
                self._check_numeric_attr(
                    attrs.get("value"), "li", "value", int
                )
            self._check_enum_attr(attrs.get("type"), "1", "a", "A", "i", "I", tag=tag)
            return
        if tag == "tg-button-row":
            self._check_enum_attr(
                attrs.get("align"), "left", "center", "right", tag=tag
            )
            return
        if tag == "tg-button":
            self._check_rich_button(attrs)
            return
        if tag == "input":
            if attrs.get("type") != "checkbox":
                self.error(
                    "html_attribute_value",
                    'Rich <input> is supported only with type="checkbox".',
                )
            return
        if tag == "tg-emoji":
            emoji_id = attrs.get("emoji-id")
            if not isinstance(emoji_id, str) or not emoji_id.isdigit():
                self.error(
                    "html_attribute_required",
                    "Tag <tg-emoji> requires a numeric emoji-id attribute.",
                )
            return
        if tag == "tg-time":
            unix = attrs.get("unix")
            if not isinstance(unix, str) or not _is_number_text(unix):
                self.error(
                    "html_attribute_required",
                    "Tag <tg-time> requires a numeric unix attribute.",
                )
            self._check_time_format(attrs.get("format"))
            return
        if tag == "code":
            if parent != "pre" and names:
                self.error(
                    "html_attribute_unknown",
                    "A standalone <code> tag does not accept attributes.",
                )
            if parent == "pre" and attrs.get("class") is not None:
                if not re.fullmatch(
                    r"language-[A-Za-z0-9_+#.-]+", str(attrs.get("class"))
                ):
                    self.error(
                        "html_attribute_value",
                        'A <code> inside <pre> accepts only class="language-...".',
                    )
            return

    def _check_rich_button(self, attrs: dict[str, str | None]) -> None:
        button_type = attrs.get("type")
        if not isinstance(button_type, str) or not button_type:
            self.error(
                "rich_button_type",
                "Tag <tg-button> requires a type attribute.",
            )
            return
        if button_type not in RICH_BUTTON_TYPES:
            self.error(
                "rich_button_type",
                "Tag <tg-button> has an unsupported type.",
            )
            return
        style = attrs.get("style")
        if style is not None and style not in {"danger", "success", "primary", "link"}:
            self.error(
                "rich_button_style",
                "Rich tg-button style must be danger, success, primary or link.",
            )
        if style == "link" and button_type != "callback_data":
            self.error(
                "rich_button_style",
                'Rich style "link" is allowed only for callback_data buttons.',
            )
        present_actions = RICH_BUTTON_ACTION_ATTRS & set(attrs)
        allowed_actions: set[str] = set()
        required_actions: set[str] = set()
        if button_type in {"url", "web_app"}:
            allowed_actions = {"url"}
            required_actions = {"url"}
        elif button_type == "callback_data":
            allowed_actions = {"data"}
            required_actions = {"data"}
        elif button_type == "login_url":
            allowed_actions = {
                "url",
                "forward-text",
                "request-write-access",
            }
            required_actions = {"url"}
        elif button_type in {
            "switch_inline_query",
            "switch_inline_query_current_chat",
        }:
            allowed_actions = {"query"}
            required_actions = {"query"}
        elif button_type == "switch_inline_query_chosen_chat":
            allowed_actions = {
                "query",
                "allow-user-chats",
                "allow-bot-chats",
                "allow-group-chats",
                "allow-channel-chats",
            }
        elif button_type == "copy_text":
            allowed_actions = {"text"}
            required_actions = {"text"}
        elif button_type == "disabled":
            allowed_actions = set()
        extra = present_actions - allowed_actions
        if extra:
            self.error(
                "rich_button_action",
                f"Rich button type '{button_type}' has an attribute for another action.",
            )
        missing = required_actions - present_actions
        if missing:
            self.error(
                "rich_button_action",
                f"Rich button type '{button_type}' is missing a required attribute.",
            )
            return
        if button_type == "url":
            value = attrs.get("url")
            if not isinstance(value, str) or not _url_ok(value, ("http", "https", "tg")):
                self.error(
                    "invalid_url",
                    "Rich URL button requires an http(s) or tg:// URL.",
                )
        elif button_type == "callback_data":
            self._check_callback_bytes(attrs.get("data"))
        elif button_type == "web_app":
            value = attrs.get("url")
            if not isinstance(value, str) or not _url_ok(
                value, ("https",), https_only=True
            ):
                self.error(
                    "invalid_url",
                    "Rich web_app button requires an HTTPS URL.",
                )
            if self.ephemeral or self.business:
                self.error(
                    "rich_button_action",
                    "Rich web_app buttons are not supported for ephemeral or business messages.",
                )
            else:
                self.warning(
                    "chat_context_unverified",
                    "web_app buttons require a private chat; the payload does not prove the chat type.",
                )
        elif button_type == "login_url":
            value = attrs.get("url")
            if not isinstance(value, str) or not _url_ok(
                value, ("https",), https_only=True
            ):
                self.error(
                    "invalid_url",
                    "Rich login_url button requires an HTTPS URL.",
                )
            if self.ephemeral:
                self.error(
                    "rich_button_action",
                    "login_url buttons are not supported for ephemeral messages.",
                )
            if "forward-text" in attrs and not isinstance(
                attrs.get("forward-text"), str
            ):
                self.error(
                    "rich_button_action",
                    "forward-text must be a string.",
                )
        elif button_type in {
            "switch_inline_query",
            "switch_inline_query_current_chat",
        }:
            if "query" in attrs and not isinstance(attrs.get("query"), str):
                self.error(
                    "rich_button_action",
                    "Switch-inline query must be a string.",
                )
            if self.business:
                self.error(
                    "rich_button_action",
                    "Switch-inline buttons are not supported on behalf of a business account.",
                )
        elif button_type == "switch_inline_query_chosen_chat":
            if "query" in attrs and not isinstance(attrs.get("query"), str):
                self.error(
                    "rich_button_action",
                    "Switch-inline query must be a string.",
                )
            if self.business:
                self.error(
                    "rich_button_action",
                    "Switch-inline buttons are not supported on behalf of a business account.",
                )
            allows = {
                "allow-user-chats",
                "allow-bot-chats",
                "allow-group-chats",
                "allow-channel-chats",
            }
            if not (allows & set(attrs)):
                self.warning(
                    "rich_button_action",
                    "switch_inline_query_chosen_chat has no allow-* attribute, so no chat type is selectable.",
                )
        elif button_type == "copy_text":
            value = attrs.get("text")
            if not isinstance(value, str) or not 1 <= len(value) <= 256:
                self.error(
                    "rich_button_action",
                    "Rich copy_text button requires text of 1-256 characters.",
                )

    def _check_callback_bytes(self, value: Any) -> None:
        if not isinstance(value, str):
            self.error(
                "rich_button_action",
                "Rich callback_data button requires a string data attribute.",
            )
            return
        length = _utf8_length(value)
        if length is None:
            return
        if not 1 <= length <= MAX_CALLBACK_BYTES:
            self.error(
                "callback_data_length",
                f"callback data must be 1-{MAX_CALLBACK_BYTES} UTF-8 bytes; got {length}.",
            )

    def _check_media_src(
        self, value: Any, tag: str, *, allow_emoji: bool
    ) -> None:
        if not isinstance(value, str) or not value:
            self.error(
                "html_attribute_required",
                f"Tag <{tag}> requires a non-empty src attribute.",
            )
            return
        if value.lower().startswith("tg://"):
            try:
                parts = urlsplit(value)
            except ValueError:
                parts = None
            if parts is not None and parts.netloc.lower() == "emoji":
                if not allow_emoji:
                    self.error(
                        "invalid_url",
                        f"Tag <{tag}> does not accept tg://emoji as media.",
                    )
                return
            ref = _tg_media_ref(value)
            if ref is None:
                self.error(
                    "invalid_url",
                    f"Tag <{tag}> media link must use a documented tg:// media form.",
                )
                return
            self.media_refs.add(ref)
            return
        if not _url_ok(value, ("http", "https")):
            self.error(
                "invalid_url",
                f"Tag <{tag}> src must be an HTTP(S) URL or a documented tg:// media link.",
            )

    def _check_positive_int_attr(self, value: Any, tag: str, key: str) -> None:
        if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d*", value):
            self.error(
                "html_attribute_value",
                f"Tag <{tag}> {key} must be a positive integer.",
            )

    def _check_time_format(self, value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not DATE_TIME_FORMAT_RE.fullmatch(value):
            self.error(
                "html_attribute_value",
                "Tag <tg-time> has an invalid format attribute; expected r or w?[dD]?[tT]?.",
            )

    def _check_numeric_attr(
        self, value: Any, tag: str, key: str, kind: type
    ) -> None:
        if value is None:
            return
        if not isinstance(value, str) or self._parse_number(value, kind) is None:
            self.error(
                "html_attribute_value",
                f"Tag <{tag}> {key} must be numeric.",
            )

    @staticmethod
    def _parse_number(value: Any, kind: type) -> int | float | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return kind(value)
        except (TypeError, ValueError):
            return None

    def _check_enum_attr(
        self, value: Any, *allowed: str, tag: str
    ) -> None:
        if value is None:
            return
        if not isinstance(value, str) or value not in allowed:
            self.error(
                "html_attribute_value",
                f"Tag <{tag}> has an unsupported attribute value.",
            )

    def _track_structures(
        self, tag: str, attrs: dict[str, str | None], parent: str | None
    ) -> None:
        if tag in RICH_BLOCK_COUNT_TAGS:
            self.block_count += 1
        if tag in RICH_MEDIA_TAGS:
            source = attrs.get("src")
            emoji_image = (
                tag == "img"
                and isinstance(source, str)
                and source.lower().startswith("tg://emoji")
            )
            if not emoji_image:
                self.media_count += 1
                if self.stack and self.stack[-1]["tag"] in {
                    "figure",
                    "tg-collage",
                    "tg-slideshow",
                }:
                    self.stack[-1]["media_count"] += 1
                    self.stack[-1]["visual_count"] += 1
        if tag == "tg-map" and self.stack and self.stack[-1]["tag"] == "figure":
            self.stack[-1]["visual_count"] += 1
        if tag == "figcaption" and self.stack:
            frame = self.stack[-1]
            frame["figcaption_count"] += 1
            if frame["figcaption_count"] > 1:
                self.error(
                    "html_nesting",
                    "A media block can contain at most one figcaption.",
                )
        if tag == "caption" and self.stack:
            frame = self.stack[-1]
            if frame["tag"] == "table":
                frame["caption_count"] += 1
                if frame["caption_count"] > 1:
                    self.error(
                        "html_nesting",
                        "A table can contain at most one caption.",
                    )
        if tag == "summary" and self.stack:
            frame = self.stack[-1]
            if frame["tag"] == "details":
                frame["summary_count"] += 1
                if frame["summary_count"] > 1:
                    self.error(
                        "html_nesting",
                        "A details block can contain at most one summary.",
                    )
        if tag == "tg-button" and self.stack:
            frame = self.stack[-1]
            if frame["tag"] == "tg-button-row":
                frame["button_count"] += 1
        if tag == "tr" and self.stack:
            table = self._nearest("table")
            if table is not None:
                table["rows"] += 1
                table["row_columns"] = 0
        if tag in {"td", "th"}:
            table = self._nearest("table")
            if table is not None:
                colspan = 1
                raw = attrs.get("colspan")
                if isinstance(raw, str) and re.fullmatch(r"[1-9]\d*", raw):
                    colspan = int(raw)
                table["row_columns"] += colspan
                parent_frame = self.stack[-1] if self.stack else None
                if parent_frame is not None and parent_frame["tag"] == "tr":
                    parent_frame["cells"] += 1

    def _nearest(self, tag: str) -> dict[str, Any] | None:
        for frame in reversed(self.stack):
            if frame["tag"] == tag:
                return frame
        return None

    def _end(self, tag: str) -> None:
        frame = self._close_tag(tag)
        if frame is None:
            return
        text = "".join(frame["text_parts"]).strip()
        if tag == "tg-button":
            if not text:
                self.error(
                    "rich_button_label",
                    "A rich tg-button requires a non-empty label.",
                )
        elif tag == "tg-button-row":
            if frame["button_count"] == 0:
                self.error(
                    "rich_button_row_empty",
                    "A tg-button-row must contain at least one tg-button.",
                )
        elif tag in {"figure", "tg-collage", "tg-slideshow"}:
            if frame["visual_count"] == 0:
                self.error(
                    "rich_media_missing",
                    f"<{tag}> must contain at least one media element.",
                )
        elif tag == "table":
            table = frame
            if table["rows"] == 0:
                self.error("rich_table_empty", "A table must contain at least one row.")
            self.max_columns = max(self.max_columns, table["max_columns"])
        elif tag == "tr":
            table = self._nearest("table")
            if table is not None:
                table["max_columns"] = max(
                    table["max_columns"], table["row_columns"]
                )
                if table["max_columns"] > MAX_TABLE_COLUMNS:
                    table["max_columns"] = MAX_TABLE_COLUMNS + 1
            if frame["cells"] == 0:
                self.error(
                    "rich_table_empty",
                    "A table row must contain at least one cell.",
                )
        elif tag == "details":
            if frame["summary_count"] == 0:
                self.warning(
                    "rich_details_summary_missing",
                    "A details block normally starts with a summary element.",
                )
        elif tag == "tg-emoji":
            if not text:
                self.error(
                    "html_tag_content",
                    "Tag <tg-emoji> requires an alternative emoji as text content.",
                )
        elif tag == "tg-time":
            if not text:
                self.warning(
                    "html_tag_content",
                    "Tag <tg-time> has no fallback text; clients may show nothing.",
                )
        elif tag == "tg-reference":
            if not text:
                self.warning(
                    "html_tag_content",
                    "Tag <tg-reference> has no referenced text.",
                )

    def unresolved_anchors(self) -> list[str]:
        return sorted(self.anchor_links - self.anchor_names)


class _Validator:
    def __init__(
        self, method: str, payload: dict[str, Any], findings: _Findings
    ) -> None:
        self.method = method
        self.payload = payload
        self.findings = findings
        self.ephemeral = method == "editEphemeralMessageText" or (
            method in SEND_METHODS
            and isinstance(payload.get("ephemeral_message_parameters"), dict)
        )
        self.business = "business_connection_id" in payload

    def run(self) -> None:
        self._check_payload_unicode()
        self._check_unknown_top_level()
        if self.method == "sendMessage":
            self._validate_send(rich=False)
        elif self.method == "sendRichMessage":
            self._validate_send(rich=True)
        elif self.method == "editMessageText":
            self._validate_edit(ephemeral=False)
        elif self.method == "editEphemeralMessageText":
            self._validate_edit(ephemeral=True)
        else:
            self._validate_callback_answer()

    def _check_payload_unicode(self) -> None:
        seen: set[int] = set()
        stack: list[tuple[Any, str]] = [(self.payload, "$")]
        while stack:
            value, path = stack.pop()
            if isinstance(value, dict):
                marker = id(value)
                if marker in seen:
                    continue
                seen.add(marker)
                for key, child in value.items():
                    key_is_valid = True
                    if isinstance(key, str):
                        key_is_valid = _utf8_length(key) is not None
                        if not key_is_valid:
                            self.error(
                                "invalid_unicode",
                                path,
                                "A string key cannot be encoded as UTF-8.",
                            )
                    child_path = _join(path, key) if key_is_valid else path
                    stack.append((child, child_path))
            elif isinstance(value, list):
                marker = id(value)
                if marker in seen:
                    continue
                seen.add(marker)
                for index, child in enumerate(value):
                    stack.append((child, f"{path}[{index}]"))
            elif isinstance(value, str):
                if _utf8_length(value) is None:
                    self.error(
                        "invalid_unicode",
                        path,
                        "A string value cannot be encoded as UTF-8.",
                    )

    def error(self, code: str, path: str, message: str) -> None:
        self.findings.error(code, path, message)

    def warning(self, code: str, path: str, message: str) -> None:
        self.findings.warning(code, path, message)

    def _check_unknown_top_level(self) -> None:
        allowed = METHOD_FIELDS[self.method]
        for key in self.payload:
            if isinstance(key, str) and key in allowed:
                continue
            path = _join("$", key)
            if (
                isinstance(key, str)
                and self.method in SEND_METHODS
                and key in LEGACY_SEND_EPHEMERAL_FIELDS
            ):
                self.error(
                    "legacy_ephemeral_field",
                    path,
                    "Bot API 10.3 sends ephemeral settings in ephemeral_message_parameters, not as top-level send fields.",
                )
                continue
            if (
                isinstance(key, str)
                and self.method == "editEphemeralMessageText"
                and key == "ephemeral_message_parameters"
            ):
                self.error(
                    "ephemeral_shape_mismatch",
                    path,
                    "editEphemeralMessageText uses top-level receiver_user_id and ephemeral_message_id, not ephemeral_message_parameters.",
                )
                continue
            self.error(
                "unknown_field",
                path,
                f"Field is not part of the Bot API {BOT_API_SNAPSHOT} snapshot for {self.method}.",
            )

    # ------------------------------------------------------------------
    # Shared scalar/object checks.
    # ------------------------------------------------------------------

    def _required(self, obj: dict[str, Any], key: str, path: str) -> bool:
        if key not in obj:
            self.error(
                "missing_field",
                _join(path, key),
                f"Required field '{key}' is missing.",
            )
            return False
        return True

    def _check_chat_id(self, value: Any, path: str) -> None:
        if _is_int(value):
            if value == 0:
                self.error(
                    "invalid_value",
                    path,
                    "chat_id integer must not be zero.",
                )
            return
        if isinstance(value, str):
            if not value:
                self.error(
                    "invalid_value",
                    path,
                    "chat_id string must not be empty.",
                )
            elif value.startswith("@"):
                if not USERNAME_RE.fullmatch(value):
                    self.error(
                        "invalid_chat_username",
                        path,
                        "chat_id username must be 5-32 letters, digits or underscores after @.",
                    )
            elif NUMERIC_CHAT_ID_RE.fullmatch(value):
                # Explicit local compatibility exception: some SDKs serialize
                # numeric chat ids as strings.  Bot API documents string targets
                # as @username; this exception is not a server guarantee.
                if value.lstrip("-").strip("0") == "":
                    self.error(
                        "invalid_value",
                        path,
                        "chat_id numeric string must not be zero.",
                    )
            else:
                self.error(
                    "invalid_chat_username",
                    path,
                    "chat_id string must be an @username (5-32 characters) or a decimal integer string accepted as a compatibility exception.",
                )
            return
        self.error(
            "wrong_type",
            path,
            "chat_id must be an integer or a non-empty username string.",
        )

    def _check_positive_int(
        self, value: Any, path: str, field: str, *, allow_zero: bool = False
    ) -> None:
        if not _is_int(value):
            self.error(
                "wrong_type",
                path,
                f"{field} must be an integer (booleans are not accepted).",
            )
            return
        if value < 0 or (value == 0 and not allow_zero):
            self.error(
                "invalid_value",
                path,
                f"{field} must be {'non-negative' if allow_zero else 'positive'}.",
            )

    def _check_bool_field(
        self, obj: dict[str, Any], key: str, path: str
    ) -> None:
        if key in obj and not _is_bool(obj[key]):
            self.error(
                "wrong_type",
                _join(path, key),
                f"{key} must be a boolean.",
            )

    def _check_int_field(
        self,
        obj: dict[str, Any],
        key: str,
        path: str,
        *,
        allow_zero: bool = False,
    ) -> None:
        if key in obj:
            self._check_positive_int(
                obj[key],
                _join(path, key),
                key,
                allow_zero=allow_zero,
            )

    def _check_str_field(
        self,
        obj: dict[str, Any],
        key: str,
        path: str,
        *,
        nonempty: bool = True,
    ) -> None:
        if key not in obj:
            return
        value = obj[key]
        if not isinstance(value, str):
            self.error(
                "wrong_type",
                _join(path, key),
                f"{key} must be a string.",
            )
        elif nonempty and not value:
            self.error(
                "invalid_value",
                _join(path, key),
                f"{key} must not be empty.",
            )

    def _check_common_send_options(self, path: str) -> None:
        p = self.payload
        self._check_str_field(p, "business_connection_id", path)
        self._check_int_field(p, "message_thread_id", path)
        self._check_int_field(p, "direct_messages_topic_id", path)
        for key in ("disable_notification", "protect_content", "allow_paid_broadcast"):
            self._check_bool_field(p, key, path)
        self._check_str_field(p, "message_effect_id", path, nonempty=False)
        if "suggested_post_parameters" in p and not isinstance(
            p["suggested_post_parameters"], dict
        ):
            self.error(
                "wrong_type",
                _join(path, "suggested_post_parameters"),
                "suggested_post_parameters must be an object.",
            )
        self._check_reply_parameters(p, path)
        if "ephemeral_message_parameters" in p:
            self._check_ephemeral_parameters(p, path)

    def _check_reply_parameters(self, obj: dict[str, Any], path: str) -> None:
        if "reply_parameters" not in obj:
            return
        value = obj["reply_parameters"]
        rp_path = _join(path, "reply_parameters")
        if not isinstance(value, dict):
            self.error(
                "wrong_type",
                rp_path,
                "reply_parameters must be an object.",
            )
            return
        allowed = {
            "message_id",
            "chat_id",
            "ephemeral_message_id",
            "allow_sending_without_reply",
            "quote",
            "quote_parse_mode",
            "quote_entities",
            "quote_position",
            "checklist_task_id",
            "poll_option_id",
        }
        for key in value:
            if key not in allowed:
                self.error(
                    "unknown_field",
                    _join(rp_path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} ReplyParameters snapshot.",
                )

        has_message = "message_id" in value
        has_ephemeral = "ephemeral_message_id" in value
        if not has_message and not has_ephemeral:
            self.error(
                "reply_target_required",
                rp_path,
                "ReplyParameters requires exactly one of message_id or ephemeral_message_id.",
            )
        elif has_message and has_ephemeral:
            self.error(
                "reply_target_conflict",
                rp_path,
                "message_id and ephemeral_message_id are mutually exclusive.",
            )
        if has_message:
            self._check_positive_int(
                value["message_id"],
                _join(rp_path, "message_id"),
                "reply_parameters.message_id",
            )
        if has_ephemeral:
            self._check_positive_int(
                value["ephemeral_message_id"],
                _join(rp_path, "ephemeral_message_id"),
                "reply_parameters.ephemeral_message_id",
            )

        allow_value = value.get("allow_sending_without_reply")
        if "allow_sending_without_reply" in value and not _is_bool(allow_value):
            self.error(
                "wrong_type",
                _join(rp_path, "allow_sending_without_reply"),
                "reply_parameters.allow_sending_without_reply must be a boolean.",
            )

        has_reply_chat = "chat_id" in value
        if has_reply_chat:
            self._check_chat_id(
                value["chat_id"],
                _join(rp_path, "chat_id"),
            )
            if self.business:
                self.error(
                    "reply_chat_not_supported",
                    _join(rp_path, "chat_id"),
                    "reply_parameters.chat_id is not supported for business-account messages.",
                )
            if "direct_messages_topic_id" in obj:
                self.error(
                    "reply_chat_not_supported",
                    _join(rp_path, "chat_id"),
                    "reply_parameters.chat_id is not supported in direct messages topics.",
                )
            if has_ephemeral:
                self.error(
                    "reply_chat_not_supported",
                    _join(rp_path, "chat_id"),
                    "reply_parameters.chat_id is not supported for ephemeral replies.",
                )
            elif allow_value is True:
                self.error(
                    "reply_allow_sending_conflict",
                    _join(rp_path, "allow_sending_without_reply"),
                    "allow_sending_without_reply is always false for a reply in another chat.",
                )

        if has_ephemeral:
            ephemeral_params = obj.get("ephemeral_message_parameters")
            if not isinstance(ephemeral_params, dict) or "receiver_user_id" not in ephemeral_params:
                self.error(
                    "ephemeral_reply_requires_ephemeral",
                    rp_path,
                    "A reply to an ephemeral message must itself be ephemeral with receiver_user_id.",
                )
            if allow_value is True:
                self.error(
                    "reply_allow_sending_conflict",
                    _join(rp_path, "allow_sending_without_reply"),
                    "allow_sending_without_reply is always false for ephemeral replies.",
                )

        self._check_quote_parameters(
            value, rp_path, ephemeral_reply=has_ephemeral
        )

        if "quote_position" in value:
            position = value["quote_position"]
            if not _is_int(position):
                self.error(
                    "wrong_type",
                    _join(rp_path, "quote_position"),
                    "reply_parameters.quote_position must be an integer (booleans are not accepted).",
                )
            elif position < 0:
                self.error(
                    "invalid_value",
                    _join(rp_path, "quote_position"),
                    "reply_parameters.quote_position must be non-negative.",
                )
        if "checklist_task_id" in value:
            self._check_positive_int(
                value["checklist_task_id"],
                _join(rp_path, "checklist_task_id"),
                "reply_parameters.checklist_task_id",
            )
        if "poll_option_id" in value:
            poll_option = value["poll_option_id"]
            if not isinstance(poll_option, str) or not poll_option:
                self.error(
                    "invalid_value",
                    _join(rp_path, "poll_option_id"),
                    "reply_parameters.poll_option_id must be a non-empty string.",
                )

    def _check_quote_parameters(
        self,
        value: dict[str, Any],
        rp_path: str,
        *,
        ephemeral_reply: bool = False,
    ) -> None:
        quote_value = value.get("quote")
        quote_text: str | None = None
        if "quote" in value:
            if not isinstance(quote_value, str):
                self.error(
                    "wrong_type",
                    _join(rp_path, "quote"),
                    "reply_parameters.quote must be a string.",
                )
            else:
                quote_text = quote_value

        parse_mode: str | None = None
        quote_parse_mode_present = "quote_parse_mode" in value
        if quote_parse_mode_present:
            mode = value["quote_parse_mode"]
            mode_path = _join(rp_path, "quote_parse_mode")
            if not isinstance(mode, str):
                self.error(
                    "wrong_type",
                    mode_path,
                    "reply_parameters.quote_parse_mode must be a string.",
                )
            elif mode not in {"HTML", "MarkdownV2", "Markdown"}:
                self.error(
                    "invalid_parse_mode",
                    mode_path,
                    'quote_parse_mode must be "HTML", "MarkdownV2" or "Markdown".',
                )
            else:
                parse_mode = mode

        quote_entities_present = "quote_entities" in value
        if quote_entities_present:
            if quote_parse_mode_present:
                self.error(
                    "parse_mode_entities_conflict",
                    _join(rp_path, "quote_entities"),
                    "quote_entities and quote_parse_mode are mutually exclusive.",
                )
            quote_entities = value["quote_entities"]
            if not isinstance(quote_entities, list):
                self.error(
                    "wrong_type",
                    _join(rp_path, "quote_entities"),
                    "reply_parameters.quote_entities must be an array.",
                )
            else:
                self._check_entities(
                    quote_text,
                    quote_entities,
                    _join(rp_path, "quote_entities"),
                )

        if (
            "quote_parse_mode" in value or quote_entities_present
        ) and quote_text is None:
            self.error(
                "field_requires_quote",
                rp_path,
                "quote_parse_mode and quote_entities require a quote.",
            )

        if quote_text is None:
            return

        if parse_mode == "HTML":
            parser = _OrdinaryHTMLParser(
                _join(rp_path, "quote"), self.findings
            )
            try:
                parser.feed(quote_text)
                parser.finish()
            except Exception:
                self.error(
                    "html_parse_failure",
                    _join(rp_path, "quote"),
                    "The quote HTML could not be parsed locally.",
                )
            length = parser.visible_len
        elif parse_mode in {"MarkdownV2", "Markdown"}:
            length = _markdown_visible_length(quote_text, parse_mode)
            self.warning(
                "markdown_syntax_unverified",
                _join(rp_path, "quote_parse_mode"),
                "Quote Markdown syntax is only roughly checked; the server parser remains authoritative.",
            )
            if length == 0 and quote_text:
                self.warning(
                    "empty_text_unverified",
                    _join(rp_path, "quote"),
                    "Visible quote text could not be confirmed locally.",
                )
        else:
            length = len(quote_text)
        if length > MAX_QUOTE_CHARS and not ephemeral_reply:
            self.error(
                "quote_too_long",
                _join(rp_path, "quote"),
                f"reply_parameters.quote exceeds {MAX_QUOTE_CHARS} characters after parsing.",
            )

    def _check_ephemeral_parameters(
        self, obj: dict[str, Any], path: str
    ) -> None:
        value = obj["ephemeral_message_parameters"]
        eph_path = _join(path, "ephemeral_message_parameters")
        if not isinstance(value, dict):
            self.error(
                "wrong_type",
                eph_path,
                "ephemeral_message_parameters must be an object.",
            )
            return
        allowed = {
            "receiver_user_id",
            "callback_query_id",
            "replace_callback_query_message",
        }
        for key in value:
            if key not in allowed:
                self.error(
                    "unknown_field",
                    _join(eph_path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} EphemeralMessageParameters snapshot.",
                )
        if "receiver_user_id" not in value:
            self.error(
                "missing_field",
                _join(eph_path, "receiver_user_id"),
                "receiver_user_id is required for an ephemeral message.",
            )
        else:
            self._check_positive_int(
                value["receiver_user_id"],
                _join(eph_path, "receiver_user_id"),
                "receiver_user_id",
            )
        if "callback_query_id" in value:
            self._check_str_field(value, "callback_query_id", eph_path)
        if "replace_callback_query_message" in value and not _is_bool(
            value["replace_callback_query_message"]
        ):
            self.error(
                "wrong_type",
                _join(eph_path, "replace_callback_query_message"),
                "replace_callback_query_message must be a boolean.",
            )
        has_callback = "callback_query_id" in value
        reply_parameters = obj.get("reply_parameters")
        has_reply_target = isinstance(reply_parameters, dict) and "ephemeral_message_id" in reply_parameters
        if not has_callback and not has_reply_target:
            self.warning(
                "ephemeral_target_unverified",
                eph_path,
                "Without callback_query_id or reply_parameters.ephemeral_message_id, sending requires live bot-admin rights; the payload cannot prove them.",
            )

    def _check_link_preview(self, obj: dict[str, Any], path: str) -> None:
        if "link_preview_options" not in obj:
            return
        value = obj["link_preview_options"]
        lp_path = _join(path, "link_preview_options")
        if not isinstance(value, dict):
            self.error(
                "wrong_type",
                lp_path,
                "link_preview_options must be an object.",
            )
            return
        allowed = {
            "is_disabled",
            "url",
            "prefer_small_media",
            "prefer_large_media",
            "show_above_text",
        }
        for key in value:
            if key not in allowed:
                self.error(
                    "unknown_field",
                    _join(lp_path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} LinkPreviewOptions snapshot.",
                )
        for key in (
            "is_disabled",
            "prefer_small_media",
            "prefer_large_media",
            "show_above_text",
        ):
            if key in value and not _is_bool(value[key]):
                self.error(
                    "wrong_type",
                    _join(lp_path, key),
                    f"link_preview_options.{key} must be a boolean.",
                )
        if "url" in value:
            url = value["url"]
            url_path = _join(lp_path, "url")
            if not isinstance(url, str):
                self.error(
                    "wrong_type",
                    url_path,
                    "link_preview_options.url must be a string.",
                )
            elif url and not _url_ok(url, ("http", "https")):
                self.error(
                    "invalid_url",
                    url_path,
                    "link_preview_options.url must be empty or an HTTP(S) URL.",
                )
        if value.get("prefer_small_media") is True and value.get("prefer_large_media") is True:
            self.warning(
                "link_preview_conflict",
                lp_path,
                "prefer_small_media and prefer_large_media are both true.",
            )

    # ------------------------------------------------------------------
    # sendMessage / sendRichMessage.
    # ------------------------------------------------------------------

    def _validate_send(self, *, rich: bool) -> None:
        p = self.payload
        path = "$"
        self._required(p, "chat_id", path)
        if "chat_id" in p:
            self._check_chat_id(p["chat_id"], _join(path, "chat_id"))
        self._check_common_send_options(path)
        if rich:
            if self._required(p, "rich_message", path):
                self._check_rich_message(p["rich_message"], _join(path, "rich_message"))
        else:
            if self._required(p, "text", path):
                self._check_ordinary_text(p, path)
            else:
                self._check_orphan_text_options(p, path)
            self._check_link_preview(p, path)
        if "reply_markup" in p:
            self._check_reply_markup(
                p["reply_markup"],
                _join(path, "reply_markup"),
                allowed_kinds={"inline", "reply", "remove", "force"},
            )

    def _check_orphan_text_options(self, obj: dict[str, Any], path: str) -> None:
        for key in ("parse_mode", "entities"):
            if key in obj:
                self.error(
                    "field_requires_text",
                    _join(path, key),
                    f"{key} is valid only together with text.",
                )
                break

    # ------------------------------------------------------------------
    # editMessageText / editEphemeralMessageText.
    # ------------------------------------------------------------------

    def _validate_edit(self, *, ephemeral: bool) -> None:
        p = self.payload
        path = "$"
        if ephemeral:
            for key in ("chat_id", "receiver_user_id", "ephemeral_message_id"):
                self._required(p, key, path)
            if "chat_id" in p:
                self._check_chat_id(p["chat_id"], _join(path, "chat_id"))
            if "receiver_user_id" in p:
                self._check_positive_int(
                    p["receiver_user_id"],
                    _join(path, "receiver_user_id"),
                    "receiver_user_id",
                )
            if "ephemeral_message_id" in p:
                self._check_positive_int(
                    p["ephemeral_message_id"],
                    _join(path, "ephemeral_message_id"),
                    "ephemeral_message_id",
                )
        else:
            self._check_str_field(p, "business_connection_id", path)
            inline = "inline_message_id" in p
            has_chat = "chat_id" in p
            has_message = "message_id" in p
            if has_chat:
                self._check_chat_id(p["chat_id"], _join(path, "chat_id"))
            if has_message:
                self._check_positive_int(
                    p["message_id"],
                    _join(path, "message_id"),
                    "message_id",
                )
            if inline:
                if has_chat or has_message:
                    self.error(
                        "address_conflict",
                        path,
                        "inline_message_id cannot be combined with chat_id/message_id.",
                    )
                self._check_str_field(p, "inline_message_id", path)
            else:
                if not has_chat:
                    self._required(p, "chat_id", path)
                if not has_message:
                    self._required(p, "message_id", path)

        has_text = "text" in p
        has_rich = "rich_message" in p
        if not has_text and not has_rich:
            self.error(
                "missing_content",
                path,
                "Either text or rich_message must be provided for an edit.",
            )
        if has_text and has_rich:
            self.error(
                "content_conflict",
                path,
                "text and rich_message are mutually exclusive for an edit.",
            )
        if has_text:
            self._check_ordinary_text(p, path)
        else:
            self._check_orphan_text_options(p, path)
        if has_rich:
            self._check_rich_message(p["rich_message"], _join(path, "rich_message"))
            if "parse_mode" in p or "entities" in p:
                self.error(
                    "rich_parse_mode_conflict",
                    path,
                    "parse_mode/entities are not used with rich_message.",
                )
            if "link_preview_options" in p:
                self.warning(
                    "link_preview_with_rich",
                    path,
                    "link_preview_options is an ordinary-message option and is not part of the rich content contract.",
                )
        self._check_link_preview(p, path)
        if "reply_markup" in p:
            self._check_reply_markup(
                p["reply_markup"],
                _join(path, "reply_markup"),
                allowed_kinds={"inline"},
            )

    # ------------------------------------------------------------------
    # answerCallbackQuery.
    # ------------------------------------------------------------------

    def _validate_callback_answer(self) -> None:
        p = self.payload
        path = "$"
        self._required(p, "callback_query_id", path)
        if "callback_query_id" in p:
            self._check_str_field(p, "callback_query_id", path)
        if "text" in p:
            value = p["text"]
            if not isinstance(value, str):
                self.error(
                    "wrong_type",
                    _join(path, "text"),
                    "answerCallbackQuery text must be a string.",
                )
            elif len(value) > 200:
                self.error(
                    "callback_text_too_long",
                    _join(path, "text"),
                    "answerCallbackQuery text must be 0-200 characters.",
                )
        if "show_alert" in p and not _is_bool(p["show_alert"]):
            self.error(
                "wrong_type",
                _join(path, "show_alert"),
                "show_alert must be a boolean.",
            )
        if "url" in p:
            value = p["url"]
            url_path = _join(path, "url")
            if not isinstance(value, str) or not _url_ok(
                value, ("http", "https", "tg")
            ):
                self.error(
                    "invalid_url",
                    url_path,
                    "answerCallbackQuery url must use http(s) or tg://.",
                )
            else:
                try:
                    host = (urlsplit(value).hostname or "").lower()
                except ValueError:
                    host = ""
                if host not in {"t.me", "telegram.me"}:
                    self.warning(
                        "callback_url_context",
                        url_path,
                        "A non-t.me URL works only for a game callback; the payload does not prove the callback origin.",
                    )
        if "cache_time" in p:
            self._check_positive_int(
                p["cache_time"],
                _join(path, "cache_time"),
                "cache_time",
                allow_zero=True,
            )

    # ------------------------------------------------------------------
    # Ordinary text / entities.
    # ------------------------------------------------------------------

    def _check_ordinary_text(
        self, obj: dict[str, Any], path: str
    ) -> None:
        text = obj.get("text")
        text_path = _join(path, "text")
        text_value: str | None = None
        if not isinstance(text, str):
            self.error(
                "wrong_type",
                text_path,
                "text must be a string.",
            )
        else:
            text_value = text
            if not text:
                self.error(
                    "empty_text",
                    text_path,
                    "text must contain 1-4096 characters after entity parsing.",
                )

        parse_mode: str | None = None
        if "parse_mode" in obj:
            value = obj["parse_mode"]
            pm_path = _join(path, "parse_mode")
            if not isinstance(value, str):
                self.error(
                    "wrong_type",
                    pm_path,
                    "parse_mode must be a string.",
                )
            elif value not in {"HTML", "MarkdownV2", "Markdown"}:
                self.error(
                    "invalid_parse_mode",
                    pm_path,
                    'parse_mode must be "HTML", "MarkdownV2" or "Markdown".',
                )
            else:
                parse_mode = value

        if "entities" in obj:
            if parse_mode is not None:
                self.error(
                    "parse_mode_entities_conflict",
                    _join(path, "entities"),
                    "entities and parse_mode are mutually exclusive.",
                )
            self._check_entities(
                text_value,
                obj["entities"],
                _join(path, "entities"),
            )

        if text_value is None:
            return

        if parse_mode == "HTML":
            parser = _OrdinaryHTMLParser(text_path, self.findings)
            try:
                parser.feed(text_value)
                parser.finish()
            except Exception:
                self.error(
                    "html_parse_failure",
                    text_path,
                    "The HTML input could not be parsed locally.",
                )
            length = parser.visible_len
            if length == 0 and text_value:
                self.error(
                    "empty_text",
                    text_path,
                    "text has no visible characters after HTML parsing.",
                )
        elif parse_mode in {"MarkdownV2", "Markdown"}:
            length = _markdown_visible_length(text_value, parse_mode)
            self.warning(
                "markdown_syntax_unverified",
                _join(path, "parse_mode"),
                "Markdown syntax is only roughly checked; the server parser remains authoritative.",
            )
            if len(text_value) > MAX_ORDINARY_TEXT and length <= MAX_ORDINARY_TEXT:
                self.warning(
                    "text_length_unverified",
                    text_path,
                    "Markdown source exceeds 4096 characters; the estimated visible text is within the limit.",
                )
            if length == 0 and text_value:
                self.warning(
                    "empty_text_unverified",
                    text_path,
                    "Visible Markdown text could not be confirmed locally; empty markup may still be rejected by the server.",
                )
        else:
            length = len(text_value)

        if length > MAX_ORDINARY_TEXT:
            self.error(
                "text_too_long",
                text_path,
                f"text exceeds the ordinary-message limit of {MAX_ORDINARY_TEXT} characters after parsing.",
            )

    def _check_entities(
        self, text: str | None, entities: Any, path: str
    ) -> None:
        if not isinstance(entities, list):
            self.error(
                "wrong_type",
                path,
                "entities must be an array of MessageEntity objects.",
            )
            return
        boundaries = _utf16_boundaries(text) if isinstance(text, str) else set()
        text_units = _utf16_units(text) if isinstance(text, str) else None
        intervals: list[tuple[int, int, str]] = []
        for index, entity in enumerate(entities):
            entity_path = f"{path}[{index}]"
            if not isinstance(entity, dict):
                self.error(
                    "wrong_type",
                    entity_path,
                    "Each entity must be an object.",
                )
                continue
            for key in entity:
                if key not in ENTITY_FIELDS:
                    self.error(
                        "unknown_field",
                        _join(entity_path, key),
                        f"Field is not part of the Bot API {BOT_API_SNAPSHOT} MessageEntity snapshot.",
                    )
            entity_type = entity.get("type")
            if not isinstance(entity_type, str):
                self.error(
                    "wrong_type",
                    _join(entity_path, "type"),
                    "entity type must be a string.",
                )
                entity_type = None
            elif entity_type not in ENTITY_TYPES:
                self.error(
                    "entity_type_unsupported",
                    _join(entity_path, "type"),
                    f"Entity type is not in the Bot API {BOT_API_SNAPSHOT} snapshot.",
                )
            offset = entity.get("offset")
            length = entity.get("length")
            valid_offset = _is_int(offset) and offset >= 0
            valid_length = _is_int(length) and length > 0
            if not _is_int(offset):
                self.error(
                    "wrong_type",
                    _join(entity_path, "offset"),
                    "entity offset must be an integer (booleans are not accepted).",
                )
            elif offset < 0:
                self.error(
                    "invalid_value",
                    _join(entity_path, "offset"),
                    "entity offset must be non-negative.",
                )
            if not _is_int(length):
                self.error(
                    "wrong_type",
                    _join(entity_path, "length"),
                    "entity length must be an integer (booleans are not accepted).",
                )
            elif length <= 0:
                self.error(
                    "invalid_value",
                    _join(entity_path, "length"),
                    "entity length must be positive.",
                )
            if valid_offset and valid_length and text_units is not None:
                end = offset + length
                if end > text_units:
                    self.error(
                        "entity_bounds",
                        entity_path,
                        "entity offset/length exceed the text length in UTF-16 code units.",
                    )
                else:
                    if offset not in boundaries or end not in boundaries:
                        self.error(
                            "entity_utf16_split",
                            entity_path,
                            "entity boundary splits a UTF-16 surrogate pair.",
                        )
                    elif entity_type is not None and entity_type in ENTITY_TYPES:
                        intervals.append((offset, end, entity_type))
            if entity_type is not None:
                self._check_entity_fields(entity, entity_type, entity_path)
        self._check_entity_nesting(intervals, path)

    def _check_entity_fields(
        self, entity: dict[str, Any], entity_type: str, path: str
    ) -> None:
        allowed = set(ENTITY_COMMON_FIELDS)
        if entity_type == "text_link":
            allowed.add("url")
            if "url" not in entity:
                self.error(
                    "entity_field_required",
                    path,
                    "text_link entity requires a url field.",
                )
            elif not isinstance(entity["url"], str) or not _url_ok(
                entity["url"], ("http", "https", "tg", "mailto", "tel")
            ):
                self.error(
                    "invalid_url",
                    _join(path, "url"),
                    "text_link url must use http(s), tg://, mailto: or tel:.",
                )
        elif entity_type == "text_mention":
            allowed.add("user")
            if "user" not in entity:
                self.error(
                    "entity_field_required",
                    path,
                    "text_mention entity requires a user object.",
                )
            else:
                user = entity["user"]
                user_path = _join(path, "user")
                if not isinstance(user, dict):
                    self.error(
                        "wrong_type",
                        user_path,
                        "text_mention user must be an object.",
                    )
                else:
                    if "id" not in user:
                        self.error(
                            "missing_field",
                            _join(user_path, "id"),
                            "User id is required.",
                        )
                    else:
                        self._check_positive_int(
                            user["id"], _join(user_path, "id"), "user id"
                        )
                    if "is_bot" in user and not _is_bool(user["is_bot"]):
                        self.error(
                            "wrong_type",
                            _join(user_path, "is_bot"),
                            "User is_bot must be a boolean.",
                        )
                    if "first_name" in user and not isinstance(
                        user["first_name"], str
                    ):
                        self.error(
                            "wrong_type",
                            _join(user_path, "first_name"),
                            "User first_name must be a string.",
                        )
        elif entity_type == "pre":
            allowed.add("language")
            if "language" in entity and not isinstance(entity["language"], str):
                self.error(
                    "wrong_type",
                    _join(path, "language"),
                    "pre language must be a string.",
                )
        elif entity_type == "custom_emoji":
            allowed.add("custom_emoji_id")
            if "custom_emoji_id" not in entity:
                self.error(
                    "entity_field_required",
                    path,
                    "custom_emoji entity requires custom_emoji_id.",
                )
            elif not isinstance(entity["custom_emoji_id"], str) or not entity[
                "custom_emoji_id"
            ].isdigit():
                self.error(
                    "invalid_value",
                    _join(path, "custom_emoji_id"),
                    "custom_emoji_id must be a numeric string.",
                )
        elif entity_type == "date_time":
            allowed.update({"unix_time", "date_time_format"})
            if "unix_time" not in entity:
                self.error(
                    "entity_field_required",
                    path,
                    "date_time entity requires unix_time.",
                )
            elif not _is_int(entity["unix_time"]):
                self.error(
                    "wrong_type",
                    _join(path, "unix_time"),
                    "unix_time must be an integer (booleans are not accepted).",
                )
            if "date_time_format" in entity:
                value = entity["date_time_format"]
                if not isinstance(value, str) or not DATE_TIME_FORMAT_RE.fullmatch(value):
                    self.error(
                        "invalid_value",
                        _join(path, "date_time_format"),
                        "date_time_format must match r or w?[dD]?[tT]?.",
                    )
        for key in entity:
            if key in ENTITY_FIELDS and key not in allowed:
                self.error(
                    "entity_field_not_applicable",
                    _join(path, key),
                    "Field is not applicable to this entity type.",
                )

    def _check_entity_nesting(
        self, intervals: list[tuple[int, int, str]], path: str
    ) -> None:
        ordered = sorted(intervals, key=lambda item: (item[0], -item[1]))
        active: dict[int, str] = {}
        active_ends: list[tuple[int, int]] = []
        open_code = 0
        open_quote = 0
        open_nonformat = 0
        seen_ranges: set[tuple[int, int]] = set()
        reported_duplicates: set[tuple[int, int]] = set()

        index = 0
        while index < len(ordered):
            start = ordered[index][0]
            while active_ends and active_ends[0][0] <= start:
                _, expired_index = heapq.heappop(active_ends)
                expired_type = active.pop(expired_index, None)
                if expired_type is None:
                    continue
                if expired_type in ENTITY_CODE:
                    open_code -= 1
                if expired_type in ENTITY_QUOTE:
                    open_quote -= 1
                if expired_type not in ENTITY_FORMATTING:
                    open_nonformat -= 1

            group_end = index
            while group_end < len(ordered) and ordered[group_end][0] == start:
                group_end += 1
            group = ordered[index:group_end]
            min_end_earlier = active_ends[0][0] if active_ends else None
            if min_end_earlier is not None and min_end_earlier < group[0][1]:
                self.error(
                    "entity_overlap",
                    path,
                    "Entities overlap without one being fully contained.",
                )

            new_entries: list[tuple[int, int]] = []
            for interval_index in range(index, group_end):
                _, end, entity_type = ordered[interval_index]
                interval_range = (start, end)
                if interval_range in seen_ranges:
                    if interval_range not in reported_duplicates:
                        reported_duplicates.add(interval_range)
                        self.warning(
                            "entity_duplicate_range",
                            path,
                            "Two entities have exactly the same range.",
                        )
                else:
                    seen_ranges.add(interval_range)

                if entity_type in ENTITY_CODE:
                    if active:
                        self.error(
                            "entity_code_nesting",
                            path,
                            "Code/pre entities cannot contain or be contained by other entities.",
                        )
                elif open_code:
                    self.error(
                        "entity_code_nesting",
                        path,
                        "Code/pre entities cannot contain or be contained by other entities.",
                    )
                elif entity_type in ENTITY_QUOTE and open_quote:
                    self.error(
                        "entity_quote_nesting",
                        path,
                        "Blockquote entities cannot be nested.",
                    )
                elif (
                    entity_type not in ENTITY_FORMATTING
                    and open_nonformat
                ):
                    self.error(
                        "entity_nesting",
                        path,
                        "Non-formatting entities cannot contain each other.",
                    )

                active[interval_index] = entity_type
                if entity_type in ENTITY_CODE:
                    open_code += 1
                if entity_type in ENTITY_QUOTE:
                    open_quote += 1
                if entity_type not in ENTITY_FORMATTING:
                    open_nonformat += 1
                new_entries.append((end, interval_index))

            for end, interval_index in new_entries:
                heapq.heappush(active_ends, (end, interval_index))
            index = group_end

    # ------------------------------------------------------------------
    # Rich messages.
    # ------------------------------------------------------------------

    def _check_rich_message(self, value: Any, path: str) -> None:
        if not isinstance(value, dict):
            self.error(
                "wrong_type",
                path,
                "rich_message must be an object.",
            )
            return
        allowed = {
            "blocks",
            "html",
            "markdown",
            "media",
            "is_rtl",
            "skip_entity_detection",
        }
        for key in value:
            if key not in allowed:
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} InputRichMessage snapshot.",
                )
        for key in ("is_rtl", "skip_entity_detection"):
            if key in value and not _is_bool(value[key]):
                self.error(
                    "wrong_type",
                    _join(path, key),
                    f"{key} must be a boolean.",
                )

        modes = [key for key in ("html", "markdown", "blocks") if key in value]
        if not modes:
            self.error(
                "rich_mode",
                path,
                "rich_message requires exactly one of html, markdown or blocks.",
            )
        elif len(modes) > 1:
            self.error(
                "rich_mode",
                path,
                "rich_message accepts exactly one of html, markdown or blocks.",
            )

        media_tags = 0
        markdown_media = 0
        html_media_refs: set[str] = set()
        markdown_media_refs: set[str] = set()
        if "html" in value:
            html_value = value["html"]
            html_path = _join(path, "html")
            if not isinstance(html_value, str):
                self.error(
                    "wrong_type",
                    html_path,
                    "rich_message.html must be a string.",
                )
            elif not html_value:
                self.error(
                    "empty_text",
                    html_path,
                    "rich_message.html must not be empty.",
                )
            else:
                parser = _RichHTMLParser(
                    html_path,
                    self.findings,
                    ephemeral=self.ephemeral,
                    business=self.business,
                )
                try:
                    parser.feed(html_value)
                    parser.finish()
                except Exception:
                    self.error(
                        "html_parse_failure",
                        html_path,
                        "The rich HTML input could not be parsed locally.",
                    )
                self.warning(
                    "rich_html_unverified",
                    html_path,
                    "The rich HTML token and attribute allowlist is a Bot API 10.3 approximation; the server parser remains authoritative.",
                )
                if parser.visible_len > MAX_RICH_TEXT:
                    self.error(
                        "rich_text_too_long",
                        html_path,
                        f"Rich HTML visible text exceeds the {MAX_RICH_TEXT}-character limit.",
                    )
                if parser.tag_count == 0 and parser.visible_len == 0:
                    self.error(
                        "empty_text",
                        html_path,
                        "rich_message.html has no visible content.",
                    )
                if parser.block_count > MAX_RICH_BLOCKS:
                    self.warning(
                        "rich_block_limit_unverified",
                        html_path,
                        f"Source HTML contains more than {MAX_RICH_BLOCKS} block-like nodes; the server block count is authoritative.",
                    )
                if parser.max_columns > MAX_TABLE_COLUMNS:
                    self.error(
                        "rich_table_columns",
                        html_path,
                        f"A table row has more than {MAX_TABLE_COLUMNS} columns.",
                    )
                if parser.unresolved_anchors():
                    self.warning(
                        "rich_anchor_unresolved",
                        html_path,
                        "An in-document link points to an anchor not defined in this rich message.",
                    )
                media_tags = parser.media_count
                html_media_refs.update(parser.media_refs)

        if "markdown" in value:
            markdown_value = value["markdown"]
            markdown_path = _join(path, "markdown")
            if not isinstance(markdown_value, str):
                self.error(
                    "wrong_type",
                    markdown_path,
                    "rich_message.markdown must be a string.",
                )
            elif not markdown_value:
                self.error(
                    "empty_text",
                    markdown_path,
                    "rich_message.markdown must not be empty.",
                )
            else:
                markdown_media = _markdown_media_count(markdown_value)
                markdown_media_refs.update(
                    RICH_MEDIA_REF_RE.findall(markdown_value)
                )
                if len(markdown_value) > MAX_RICH_TEXT:
                    self.warning(
                        "rich_text_length_unverified",
                        markdown_path,
                        f"Markdown source exceeds {MAX_RICH_TEXT} characters; only the server can count the parsed rich text.",
                    )
                if markdown_value.count("```") % 2:
                    self.warning(
                        "rich_markdown_unverified",
                        markdown_path,
                        "Markdown has an odd number of code fences; syntax is not fully checked locally.",
                    )
                else:
                    self.warning(
                        "rich_markdown_unverified",
                        markdown_path,
                        "Rich Markdown syntax is not fully checked locally; the server parser remains authoritative.",
                    )

        if "blocks" in value:
            blocks = value["blocks"]
            blocks_path = _join(path, "blocks")
            if not isinstance(blocks, list):
                self.error(
                    "wrong_type",
                    blocks_path,
                    "rich_message.blocks must be an array.",
                )
            elif not blocks:
                self.error(
                    "empty_text",
                    blocks_path,
                    "rich_message.blocks must not be empty.",
                )
            else:
                if len(blocks) > MAX_RICH_BLOCKS:
                    self.warning(
                        "rich_block_limit_unverified",
                        blocks_path,
                        f"Block array length exceeds {MAX_RICH_BLOCKS}; nested block counting is server-side.",
                    )
                for index, block in enumerate(blocks):
                    block_path = f"{blocks_path}[{index}]"
                    if not isinstance(block, dict):
                        self.error(
                            "wrong_type",
                            block_path,
                            "Each rich block must be an object.",
                        )
                    elif not isinstance(block.get("type"), str) or not block.get(
                        "type"
                    ):
                        self.error(
                            "wrong_type",
                            _join(block_path, "type"),
                            "Each rich block requires a non-empty type string.",
                        )
                self.warning(
                    "rich_blocks_unverified",
                    blocks_path,
                    "Block field schemas are not fully validated locally; only basic block shape is checked.",
                )

        media_ids: list[str] = []
        if "media" in value:
            media_path = _join(path, "media")
            media_ids = self._check_rich_media(value["media"], media_path)
            if "blocks" in value:
                self.warning(
                    "rich_media_with_blocks_unverified",
                    media_path,
                    "The media array is documented for html/markdown content; block media semantics are not fully checked locally.",
                )

        missing_html_refs = sorted(html_media_refs - set(media_ids))
        if missing_html_refs:
            self.error(
                "rich_media_id_missing",
                _join(path, "media"),
                "One or more tg:// media references have no matching media entry id.",
            )
        missing_markdown_refs = sorted(
            markdown_media_refs - set(media_ids) - html_media_refs
        )
        if missing_markdown_refs:
            self.warning(
                "rich_media_id_missing_unverified",
                _join(path, "markdown"),
                "Markdown contains tg:// media-like links without matching media ids; only the server can confirm whether they are media references.",
            )

        total_media = max(media_tags, len(media_ids))
        if total_media > MAX_RICH_MEDIA:
            self.error(
                "rich_media_limit",
                _join(path, "media") if "media" in value else path,
                f"Rich message exceeds the {MAX_RICH_MEDIA}-media limit.",
            )
        elif markdown_media > MAX_RICH_MEDIA:
            self.warning(
                "rich_media_limit_unverified",
                _join(path, "markdown"),
                f"Markdown contains more than {MAX_RICH_MEDIA} image-like spans; only the server can confirm the parsed media count.",
            )

    def _check_rich_media(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, list):
            self.error(
                "wrong_type",
                path,
                "rich_message.media must be an array.",
            )
            return []
        ids: list[str] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            if not isinstance(item, dict):
                self.error(
                    "wrong_type",
                    item_path,
                    "Each rich media entry must be an object.",
                )
                continue
            for key in item:
                if key not in {"id", "media"}:
                    self.error(
                        "unknown_field",
                        _join(item_path, key),
                        f"Field is not part of the Bot API {BOT_API_SNAPSHOT} InputRichMessageMedia snapshot.",
                    )
            media_id = item.get("id")
            if not isinstance(media_id, str) or not MEDIA_ID_RE.fullmatch(media_id):
                self.error(
                    "invalid_value",
                    _join(item_path, "id"),
                    "Media id must be 1-64 characters using A-Z, a-z, 0-9, _ or -.",
                )
            else:
                if media_id in seen_ids:
                    self.error(
                        "invalid_value",
                        _join(item_path, "id"),
                        "Media id is duplicated.",
                    )
                else:
                    seen_ids.add(media_id)
                    ids.append(media_id)
            media = item.get("media")
            if not isinstance(media, dict):
                self.error(
                    "wrong_type",
                    _join(item_path, "media"),
                    "Media entry requires an InputMedia object.",
                )
            elif not isinstance(media.get("type"), str) or not media.get("type"):
                self.error(
                    "wrong_type",
                    _join(item_path, "media"),
                    "Media object requires a non-empty type.",
                )
        return ids

    # ------------------------------------------------------------------
    # reply_markup and buttons.
    # ------------------------------------------------------------------

    def _check_reply_markup(
        self,
        value: Any,
        path: str,
        *,
        allowed_kinds: set[str],
    ) -> None:
        if not isinstance(value, dict):
            self.error(
                "wrong_type",
                path,
                "reply_markup must be an object.",
            )
            return
        keys = set(value)
        has_inline = "inline_keyboard" in keys
        has_keyboard = "keyboard" in keys
        has_remove = "remove_keyboard" in keys
        has_force = "force_reply" in keys
        marker_count = sum((has_inline, has_keyboard, has_remove))
        kind: str | None = None
        if marker_count > 1:
            self.error(
                "reply_markup_kind",
                path,
                "reply_markup mixes several markup kinds.",
            )
            return
        if has_inline:
            kind = "inline"
        elif has_keyboard:
            kind = "reply"
        elif has_remove:
            kind = "remove"
        elif has_force:
            kind = "force"
        else:
            self.error(
                "reply_markup_kind",
                path,
                "reply_markup does not match inline, reply, remove or force-reply shape.",
            )
            return
        if kind not in allowed_kinds:
            self.error(
                "reply_markup_not_allowed",
                path,
                f"{self.method} allows only {', '.join(sorted(allowed_kinds))} reply_markup.",
            )
        if kind == "inline":
            self._check_inline_markup(value, path)
        elif kind == "reply":
            self._check_reply_keyboard(value, path)
        elif kind == "remove":
            self._check_remove_keyboard(value, path)
        else:
            self._check_force_reply(value, path)

    def _check_inline_markup(self, obj: dict[str, Any], path: str) -> None:
        for key in obj:
            if key not in {"inline_keyboard", "force_reply"}:
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} InlineKeyboardMarkup snapshot.",
                )
        if "force_reply" in obj and not _is_bool(obj["force_reply"]):
            self.error(
                "wrong_type",
                _join(path, "force_reply"),
                "InlineKeyboardMarkup.force_reply must be a boolean.",
            )
        rows = obj.get("inline_keyboard")
        rows_path = _join(path, "inline_keyboard")
        if not isinstance(rows, list):
            self.error(
                "wrong_type",
                rows_path,
                "inline_keyboard must be an array of rows.",
            )
            return
        if not rows:
            self.error(
                "keyboard_shape",
                rows_path,
                "inline_keyboard must contain at least one row.",
            )
            return
        for row_index, row in enumerate(rows):
            row_path = f"{rows_path}[{row_index}]"
            if not isinstance(row, list):
                self.error(
                    "keyboard_shape",
                    row_path,
                    "Each inline keyboard row must be an array of buttons.",
                )
                continue
            if not row:
                self.error(
                    "keyboard_shape",
                    row_path,
                    "Inline keyboard rows must not be empty.",
                )
                continue
            for button_index, button in enumerate(row):
                button_path = f"{row_path}[{button_index}]"
                if not isinstance(button, dict):
                    self.error(
                        "keyboard_shape",
                        button_path,
                        "Each inline keyboard button must be an object.",
                    )
                    continue
                self._check_inline_button(
                    button,
                    button_path,
                    row_index,
                    button_index,
                )

    def _check_inline_button(
        self,
        button: dict[str, Any],
        path: str,
        row_index: int,
        button_index: int,
    ) -> None:
        for key in button:
            if key not in INLINE_BUTTON_FIELDS:
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} InlineKeyboardButton snapshot.",
                )
        if "text" not in button:
            self.error(
                "button_text_required",
                _join(path, "text"),
                "Inline keyboard button requires text.",
            )
        elif not isinstance(button["text"], str) or not button["text"]:
            self.error(
                "button_text_required",
                _join(path, "text"),
                "Inline keyboard button text must be a non-empty string.",
            )
        if "icon_custom_emoji_id" in button and (
            not isinstance(button["icon_custom_emoji_id"], str)
            or not button["icon_custom_emoji_id"]
        ):
            self.error(
                "wrong_type",
                _join(path, "icon_custom_emoji_id"),
                "icon_custom_emoji_id must be a non-empty string.",
            )
        style = button.get("style")
        if style is not None:
            if not isinstance(style, str) or style not in BUTTON_STYLES:
                self.error(
                    "button_style",
                    _join(path, "style"),
                    'Inline keyboard style must be "danger", "success" or "primary"; "link" is rich-only.',
                )
        actions = [key for key in button if key in INLINE_BUTTON_ACTIONS]
        if len(actions) != 1:
            self.error(
                "button_action_count",
                path,
                "An inline keyboard button must have exactly one action field (except text, style and icon).",
            )
        for action in actions:
            action_path = _join(path, action)
            value = button[action]
            if action == "url":
                if not isinstance(value, str) or not _url_ok(
                    value, ("http", "https", "tg")
                ):
                    self.error(
                        "invalid_url",
                        action_path,
                        "Button url must use http(s) or tg://.",
                    )
            elif action == "callback_data":
                self._check_callback_data(value, action_path)
            elif action == "web_app":
                self._check_web_app_info(value, action_path)
            elif action == "login_url":
                self._check_login_url(value, action_path, rich=False)
            elif action in {
                "switch_inline_query",
                "switch_inline_query_current_chat",
            }:
                if not isinstance(value, str):
                    self.error(
                        "wrong_type",
                        action_path,
                        f"{action} must be a string (it may be empty).",
                    )
                if self.business:
                    self.error(
                        "button_action_not_supported",
                        action_path,
                        "Switch-inline buttons are not supported for business-account messages.",
                    )
            elif action == "switch_inline_query_chosen_chat":
                self._check_switch_chosen(value, action_path)
                if self.business:
                    self.error(
                        "button_action_not_supported",
                        action_path,
                        "Switch-inline buttons are not supported for business-account messages.",
                    )
            elif action == "copy_text":
                self._check_copy_text(value, action_path)
            elif action == "callback_game":
                if not isinstance(value, dict):
                    self.error(
                        "wrong_type",
                        action_path,
                        "callback_game must be an object.",
                    )
                if not (row_index == 0 and button_index == 0):
                    self.error(
                        "button_first_row",
                        action_path,
                        "A callback_game button must be the first button of the first row.",
                    )
            elif action == "pay":
                if value is not True:
                    self.error(
                        "wrong_type",
                        action_path,
                        "pay must be true when used.",
                    )
                if not (row_index == 0 and button_index == 0):
                    self.error(
                        "button_first_row",
                        action_path,
                        "A pay button must be the first button of the first row.",
                    )
                self.error(
                    "button_action_not_supported",
                    action_path,
                    "pay is valid only in invoice messages, which this validator does not cover.",
                )
            elif action == "disabled":
                if not isinstance(value, dict):
                    self.error(
                        "wrong_type",
                        action_path,
                        "disabled must be an object.",
                    )

    def _check_callback_data(self, value: Any, path: str) -> None:
        if not isinstance(value, str):
            self.error(
                "wrong_type",
                path,
                "callback_data must be a string.",
            )
            return
        length = _utf8_length(value)
        if length is None:
            return
        if not 1 <= length <= MAX_CALLBACK_BYTES:
            self.error(
                "callback_data_length",
                path,
                f"callback_data must be 1-{MAX_CALLBACK_BYTES} UTF-8 bytes; got {length}.",
            )

    def _check_web_app_info(self, value: Any, path: str) -> None:
        if not isinstance(value, dict):
            self.error(
                "wrong_type",
                path,
                "web_app must be a WebAppInfo object.",
            )
            return
        for key in value:
            if key != "url":
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} WebAppInfo snapshot.",
                )
        url = value.get("url")
        if not isinstance(url, str) or not _url_ok(
            url, ("https",), https_only=True
        ):
            self.error(
                "invalid_url",
                _join(path, "url"),
                "WebAppInfo.url must be an HTTPS URL.",
            )
        if self.ephemeral:
            self.error(
                "button_action_not_supported",
                path,
                "web_app buttons are not available in ephemeral messages.",
            )
        if self.business:
            self.error(
                "button_action_not_supported",
                path,
                "web_app buttons are not supported for business-account messages.",
            )
        if not self.ephemeral and not self.business:
            self.warning(
                "chat_context_unverified",
                path,
                "web_app buttons require a private chat; the payload does not prove the chat type.",
            )

    def _check_login_url(
        self, value: Any, path: str, *, rich: bool
    ) -> None:
        if not isinstance(value, dict):
            self.error(
                "wrong_type",
                path,
                "login_url must be a LoginUrl object.",
            )
            return
        allowed = {"url", "forward_text", "bot_username", "request_write_access"}
        for key in value:
            if key not in allowed:
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} LoginUrl snapshot.",
                )
        if rich and "bot_username" in value:
            self.error(
                "login_url_field_not_supported",
                _join(path, "bot_username"),
                "bot_username is not supported in a rich message button.",
            )
        url = value.get("url")
        if not isinstance(url, str) or not _url_ok(
            url, ("https",), https_only=True
        ):
            self.error(
                "invalid_url",
                _join(path, "url"),
                "LoginUrl.url must be an HTTPS URL.",
            )
        for key in ("forward_text", "bot_username"):
            if key in value and not isinstance(value[key], str):
                self.error(
                    "wrong_type",
                    _join(path, key),
                    f"LoginUrl.{key} must be a string.",
                )
        if "request_write_access" in value and not _is_bool(
            value["request_write_access"]
        ):
            self.error(
                "wrong_type",
                _join(path, "request_write_access"),
                "LoginUrl.request_write_access must be a boolean.",
            )
        if self.ephemeral:
            self.error(
                "button_action_not_supported",
                path,
                "login_url buttons are not supported for ephemeral messages.",
            )

    def _check_switch_chosen(self, value: Any, path: str) -> None:
        if not isinstance(value, dict):
            self.error(
                "wrong_type",
                path,
                "switch_inline_query_chosen_chat must be an object.",
            )
            return
        allowed = {
            "query",
            "allow_user_chats",
            "allow_bot_chats",
            "allow_group_chats",
            "allow_channel_chats",
        }
        for key in value:
            if key not in allowed:
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} SwitchInlineQueryChosenChat snapshot.",
                )
        if "query" in value and not isinstance(value["query"], str):
            self.error(
                "wrong_type",
                _join(path, "query"),
                "switch_inline_query_chosen_chat.query must be a string.",
            )
        for key in allowed - {"query"}:
            if key in value and not _is_bool(value[key]):
                self.error(
                    "wrong_type",
                    _join(path, key),
                    f"{key} must be a boolean.",
                )
        if not any(value.get(key) is True for key in allowed - {"query"}):
            self.warning(
                "button_action_context",
                path,
                "No allow_* chat type is true; no target chat type is selectable.",
            )

    def _check_copy_text(self, value: Any, path: str) -> None:
        if not isinstance(value, dict):
            self.error(
                "wrong_type",
                path,
                "copy_text must be a CopyTextButton object.",
            )
            return
        for key in value:
            if key != "text":
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} CopyTextButton snapshot.",
                )
        text = value.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= 256:
            self.error(
                "invalid_value",
                _join(path, "text"),
                "copy_text.text must contain 1-256 characters.",
            )

    def _check_reply_keyboard(self, obj: dict[str, Any], path: str) -> None:
        allowed = {
            "keyboard",
            "is_persistent",
            "resize_keyboard",
            "one_time_keyboard",
            "input_field_placeholder",
            "selective",
            "force_reply",
        }
        for key in obj:
            if key not in allowed:
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} ReplyKeyboardMarkup snapshot.",
                )
        for key in (
            "is_persistent",
            "resize_keyboard",
            "one_time_keyboard",
            "selective",
            "force_reply",
        ):
            if key in obj and not _is_bool(obj[key]):
                self.error(
                    "wrong_type",
                    _join(path, key),
                    f"{key} must be a boolean.",
                )
        if "input_field_placeholder" in obj:
            placeholder = obj["input_field_placeholder"]
            if not isinstance(placeholder, str) or not 1 <= len(placeholder) <= 64:
                self.error(
                    "invalid_value",
                    _join(path, "input_field_placeholder"),
                    "input_field_placeholder must contain 1-64 characters.",
                )
        if self.business:
            self.error(
                "reply_markup_not_allowed",
                path,
                "Reply keyboards are not supported for business-account messages.",
            )
        rows = obj.get("keyboard")
        rows_path = _join(path, "keyboard")
        if not isinstance(rows, list):
            self.error(
                "wrong_type",
                rows_path,
                "keyboard must be an array of rows.",
            )
            return
        if not rows:
            self.error(
                "keyboard_shape",
                rows_path,
                "keyboard must contain at least one row.",
            )
            return
        for row_index, row in enumerate(rows):
            row_path = f"{rows_path}[{row_index}]"
            if not isinstance(row, list):
                self.error(
                    "keyboard_shape",
                    row_path,
                    "Each reply keyboard row must be an array.",
                )
                continue
            if not row:
                self.error(
                    "keyboard_shape",
                    row_path,
                    "Reply keyboard rows must not be empty.",
                )
                continue
            for button_index, button in enumerate(row):
                button_path = f"{row_path}[{button_index}]"
                if isinstance(button, str):
                    if not button:
                        self.error(
                            "button_text_required",
                            button_path,
                            "A string reply-keyboard button must not be empty.",
                        )
                elif isinstance(button, dict):
                    self._check_reply_button(button, button_path)
                else:
                    self.error(
                        "keyboard_shape",
                        button_path,
                        "A reply-keyboard button must be a string or an object.",
                    )

    def _check_reply_button(self, button: dict[str, Any], path: str) -> None:
        for key in button:
            if key not in REPLY_BUTTON_FIELDS:
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} KeyboardButton snapshot.",
                )
        if "text" not in button:
            self.error(
                "button_text_required",
                _join(path, "text"),
                "Reply keyboard button requires text.",
            )
        elif not isinstance(button["text"], str) or not button["text"]:
            self.error(
                "button_text_required",
                _join(path, "text"),
                "Reply keyboard button text must be a non-empty string.",
            )
        if "icon_custom_emoji_id" in button and (
            not isinstance(button["icon_custom_emoji_id"], str)
            or not button["icon_custom_emoji_id"]
        ):
            self.error(
                "wrong_type",
                _join(path, "icon_custom_emoji_id"),
                "icon_custom_emoji_id must be a non-empty string.",
            )
        style = button.get("style")
        if style is not None and (
            not isinstance(style, str) or style not in BUTTON_STYLES
        ):
            self.error(
                "button_style",
                _join(path, "style"),
                'Reply keyboard style must be "danger", "success" or "primary".',
            )
        actions = [key for key in button if key in REPLY_BUTTON_ACTIONS]
        if len(actions) > 1:
            self.error(
                "button_action_count",
                path,
                "A reply keyboard button may have at most one action field.",
            )
        for action in actions:
            action_path = _join(path, action)
            value = button[action]
            if action == "request_contact" or action == "request_location":
                if not _is_bool(value):
                    self.error(
                        "wrong_type",
                        action_path,
                        f"{action} must be a boolean.",
                    )
            elif action in {
                "request_users",
                "request_chat",
                "request_managed_bot",
                "request_poll",
            }:
                if not isinstance(value, dict):
                    self.error(
                        "wrong_type",
                        action_path,
                        f"{action} must be an object.",
                    )
            elif action == "web_app":
                self._check_web_app_info(value, action_path)

    def _check_remove_keyboard(self, obj: dict[str, Any], path: str) -> None:
        for key in obj:
            if key not in {"remove_keyboard", "selective"}:
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} ReplyKeyboardRemove snapshot.",
                )
        if obj.get("remove_keyboard") is not True:
            self.error(
                "invalid_value",
                _join(path, "remove_keyboard"),
                "remove_keyboard must be true.",
            )
        if "selective" in obj and not _is_bool(obj["selective"]):
            self.error(
                "wrong_type",
                _join(path, "selective"),
                "selective must be a boolean.",
            )
        if self.business:
            self.error(
                "reply_markup_not_allowed",
                path,
                "ReplyKeyboardRemove is not supported for business-account messages.",
            )

    def _check_force_reply(self, obj: dict[str, Any], path: str) -> None:
        for key in obj:
            if key not in {
                "force_reply",
                "input_field_placeholder",
                "selective",
            }:
                self.error(
                    "unknown_field",
                    _join(path, key),
                    f"Field is not part of the Bot API {BOT_API_SNAPSHOT} ForceReply snapshot.",
                )
        if obj.get("force_reply") is not True:
            self.error(
                "invalid_value",
                _join(path, "force_reply"),
                "force_reply must be true.",
            )
        if "input_field_placeholder" in obj:
            placeholder = obj["input_field_placeholder"]
            if not isinstance(placeholder, str) or not 1 <= len(placeholder) <= 64:
                self.error(
                    "invalid_value",
                    _join(path, "input_field_placeholder"),
                    "input_field_placeholder must contain 1-64 characters.",
                )
        if "selective" in obj and not _is_bool(obj["selective"]):
            self.error(
                "wrong_type",
                _join(path, "selective"),
                "selective must be a boolean.",
            )
        if self.business:
            self.error(
                "reply_markup_not_allowed",
                path,
                "ForceReply is not supported for business-account messages.",
            )


def validate_request(method: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    """Return findings for one Telegram Bot API request payload.

    The function is offline and total for arbitrary inputs: invalid method or
    payload shape is reported as a finding instead of raising.  The five
    supported method names are matched case-insensitively.  It is not a
    replacement for Telegram's server parser.
    """
    findings = _Findings()
    canonical = _canonical_method(method)
    if canonical is None:
        findings.error(
            "unsupported_method",
            "$",
            "Method is not covered by this local validator.",
        )
        return findings.items
    if not isinstance(payload, dict):
        findings.error(
            "payload_not_object",
            "$",
            "Request payload must be a JSON object.",
        )
        return findings.items
    try:
        _Validator(canonical, payload, findings).run()
    except Exception:
        findings.force_error(
            "validator_failure",
            "$",
            "The local validator could not complete on this payload shape.",
        )
    return findings.items


def _emit_human(method: str, findings: list[dict[str, str]]) -> None:
    if not findings:
        print(f"OK: {method} has no local Bot API {BOT_API_SNAPSHOT} findings.")
        return
    for finding in findings:
        print(
            f"{finding['severity'].upper()} {finding['code']} "
            f"{finding['path']}: {finding['message']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline structural validator for selected Telegram Bot API payloads.",
        epilog="Exit codes: 0 no errors, 1 validation errors, 2 invocation/I/O/JSON failure.",
        allow_abbrev=False,
    )
    parser.add_argument("--method", required=True, help="Telegram Bot API method name")
    parser.add_argument("--file", required=True, help="Path to a JSON request body")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print findings as a JSON array instead of human-readable lines",
    )
    args = parser.parse_args(argv)

    canonical = _canonical_method(args.method)
    if canonical is None:
        print(
            "error: method is not supported by this validator; "
            f"supported: {', '.join(SUPPORTED_METHODS)}",
            file=sys.stderr,
        )
        return 2
    try:
        raw = Path(args.file).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print(f"error: cannot read JSON file: {args.file}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"error: invalid JSON at line {exc.lineno}, column {exc.colno}",
            file=sys.stderr,
        )
        return 2

    findings = validate_request(canonical, payload)
    if args.json:
        print(json.dumps(findings, ensure_ascii=False, indent=2))
    else:
        _emit_human(canonical, findings)
    return 1 if any(item["severity"] == "error" for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
