"""Small shared release guard, not a general secret scanner."""
from __future__ import annotations

import re

BOT_TOKEN = re.compile(rb'(?<![A-Za-z0-9_])(?:bot)?\d{5,15}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])', re.I)
PRIVATE_KEY = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----')


def contains_secret(content: bytes) -> bool:
    """Recognize likely Telegram credentials/private keys without returning them."""
    return BOT_TOKEN.search(content) is not None or PRIVATE_KEY.search(content) is not None
