"""The shapes the key store accepts. Every check is a full match over ASCII; none raises with its input attached.

- key id: 1 to 128 of ``A-Z a-z 0-9 . _ : -``, starting with a letter or digit. It is the route's connection
  id (``route.connection.id``), the ``token`` of ``schemas/control/v1/common.v1.json`` narrowed so that it can
  never be read as a helper's option.
- provider: a lower-case vendor name, 1 to 64 of ``a-z 0-9 . _ -``, starting with a letter or digit. Any vendor
  (CONTRACT 275, "API keys are configurable for every vendor"); the store keeps it to show it, nothing else.
- variable name: the environment variable the engine reads the key from, 1 to 128 of ``A-Z 0-9 _``, not
  starting with a digit. The engine adapters check their own allowlists (``ANTHROPIC_API_KEY`` for Claude
  Code, ``CODEX_API_KEY`` or ``OPENAI_API_KEY`` for Codex); the store checks only the shape.
- value: 8 to 4096 visible ASCII characters (``!`` to ``~``): no space, no control character, no newline.
- spend cap: US dollars, 0 to 100000 with at most two decimals.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

KEY_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
PROVIDER = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')
ENV_NAME = re.compile(r'[A-Z_][A-Z0-9_]{0,127}')
CAP_TEXT = re.compile(r'[0-9]{1,6}(\.[0-9]{1,2})?')
TIMESTAMP = re.compile(r'[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]'
                       r'\.[0-9]{3}Z')
VALUE_MIN, VALUE_MAX = 8, 4096
CAP_MAX = 100000


def _full(pattern: re.Pattern, text: object) -> bool:
    return isinstance(text, str) and text.isascii() and pattern.fullmatch(text) is not None


def valid_key_id(text: object) -> bool:
    return _full(KEY_ID, text)


def valid_provider(text: object) -> bool:
    return _full(PROVIDER, text)


def valid_env_name(text: object) -> bool:
    return _full(ENV_NAME, text)


def valid_timestamp(text: object) -> bool:
    return _full(TIMESTAMP, text)


def valid_value(value: object) -> bool:
    return (isinstance(value, str) and VALUE_MIN <= len(value) <= VALUE_MAX
            and all('!' <= ch <= '~' for ch in value))


def decode_value(raw: bytes) -> str | None:
    """``raw`` as a value, or None. Never raises: a decoding error would carry the bytes it failed on."""
    if not isinstance(raw, bytes) or not VALUE_MIN <= len(raw) <= VALUE_MAX:
        return None
    if not all(0x21 <= byte <= 0x7E for byte in raw):
        return None
    return raw.decode('ascii')


def parse_cap(text: str) -> int | float | None:
    """A spend cap typed by the user, as the JSON number the index keeps, or None when it is not one."""
    if not _full(CAP_TEXT, text):
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if amount > CAP_MAX:
        return None
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def valid_cap(value: object) -> bool:
    """A cap as the index keeps it: a JSON number (never a boolean), finite, 0 to 100000, two decimals at most."""
    if type(value) not in (int, float):
        return False
    if not math.isfinite(value) or not 0 <= value <= CAP_MAX:
        return False
    return round(value, 2) == value
