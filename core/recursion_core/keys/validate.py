"""The shapes the key store accepts. Every check is a full match over ASCII; none raises with its input attached.

- key id: 1 to 128 of ``A-Z a-z 0-9 . _ : -``, starting with a letter or digit. It is the route's connection
  id (``route.connection.id``), the ``token`` of ``schemas/control/v1/common.v1.json`` NARROWED: an id that
  starts with ``.``, ``_``, ``:`` or ``-`` is refused, so that no id can ever be read as a helper's option. A
  route whose connection id starts so cannot have a saved key. A NEW id (``add``, ``remove``) is also at most 64
  characters and not shaped like a key (``looks_like_a_key``), so that a key pasted where the id goes is
  refused, not saved as the id and shown.
- provider: any lower-case vendor id, 1 to 64 of ``a-z 0-9 . _ -``, starting with a letter or digit ("API keys
  are configurable for every vendor", CONTRACT 275). ``PROVIDERS`` below is the published list of the vendors
  the contract names; the key store owns it (the Liaison's ruling of Nov 14, 2026) and lane ROUTING matches a
  routing login's vendor against it, exactly. A key saved under another id is kept and listed, and no routing
  login can name it until its id is added here.
- variable name: the environment variable the engine reads the key from, 1 to 128 of ``A-Z 0-9 _``, not
  starting with a digit. The engine adapters check their own allowlists (``ANTHROPIC_API_KEY`` for Claude
  Code, ``CODEX_API_KEY`` or ``OPENAI_API_KEY`` for Codex); the store checks only the shape.
- value: 8 to 4096 visible ASCII characters (``!`` to ``~``): no space, no control character, no newline.
- spend cap: US dollars, 0 to 100000 with at most two decimals.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

KEY_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
PROVIDER = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')

# The published provider ids: one per vendor whose API key the product contract names (contract lines are the
# pinned PRODUCT.md at d84cdd26). A vendor the contract adds is one more line here. GitHub Copilot is not one:
# the own loop reaches it on the user's own Copilot sign-in and "Recursion ... holds no GitHub or Copilot token"
# (CONTRACT 715-719).
PROVIDERS = (
    ('anthropic', 'CONTRACT 272-274: an Anthropic API key serves the own loop and any other harness'),
    ('openai', 'CONTRACT 612: Codex reads CODEX_API_KEY or OPENAI_API_KEY'),
    ('google', 'CONTRACT 571-573 and 612: paid Gemini API keys (GEMINI_API_KEY); E5 Antigravity and E7 Gemini CLI'),
    ('xai', 'CONTRACT 548, 591-600: E4 Grok CLI, and the own loop\'s xai adapter'),
    ('cursor', 'CONTRACT 548 and 565: E6 Cursor CLI'),
    ('opencode', 'CONTRACT 548 and 582: E3 OpenCode CLI'),
    ('nous', 'CONTRACT 603: the own loop on the nous adapter (Lucas, Nov 8)'),
    ('refuseless', 'CONTRACT 5079: "I use refuseless.com a lot" (Lucas, Nov 8, asking for a way to store API keys)'),
)
PROVIDER_IDS = tuple(provider for provider, _ in PROVIDERS)
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


ID_SAVE_MAX = 64
ID_WORD_MAX = 16             # letters and digits between two of . _ : -
ID_DIGITS_MAX = 8            # digits in a row (a date, 20261114, is 8)
ID_CLASS_CHANGES_MAX = 4     # changes between lower case, upper case and digit inside the words, in all
_ID_WORD = re.compile(r'[A-Za-z0-9]+')
_DIGITS = re.compile(r'[0-9]+')


def _kind(ch: str) -> int:
    return 0 if 'a' <= ch <= 'z' else 1 if 'A' <= ch <= 'Z' else 2


def looks_like_a_key(text: str) -> bool:
    """True when ``text`` has the shape of an API key, not of a name.

    A connection id is a name: a few words of letters and digits between ``. _ : -``, as route files spell them
    (``codex-key``, ``login-B``, ``gpt4o-mini``, ``OpenAI-prod``). A key is long runs of mixed characters, split
    or not. So an id is key-shaped when a word is longer than 16 characters, when more than 8 digits stand in a
    row, or when its words change between lower case, upper case and digits more than 4 times in all
    (``Ab3d-Ef5h`` changes 6 times, ``OpenAI-prod`` twice, ``gpt4o-mini`` twice)."""
    words = _ID_WORD.findall(text)
    if any(len(word) > ID_WORD_MAX for word in words):
        return True
    if any(len(run) > ID_DIGITS_MAX for run in _DIGITS.findall(text)):
        return True
    changes = sum(_kind(a) != _kind(b) for word in words for a, b in zip(word, word[1:]))
    return changes > ID_CLASS_CHANGES_MAX


def id_problem(text: object) -> str | None:
    """Why ``text`` cannot be saved (or removed) as a key id, in words that never repeat it; None when it can.

    Narrower than ``valid_key_id`` (which reads an index the store already wrote): at most 64 characters, and not
    the shape of a key (``looks_like_a_key``), so that a key typed in the id's place is refused instead of being
    saved as the id and shown by ``list``."""
    if not valid_key_id(text):
        return 'a key id is 1 to 64 of A-Z a-z 0-9 . _ : -, starting with a letter or digit'
    if len(text) > ID_SAVE_MAX or looks_like_a_key(text):
        return ('this looks like a key, not a key id, so it is refused and not repeated here; a key is never taken '
                'from the command line')
    return None


def valid_provider(text: object) -> bool:
    """Any well-formed vendor id; ``known_provider`` says whether it is one of the published ones."""
    return _full(PROVIDER, text)


def known_provider(text: object) -> bool:
    return isinstance(text, str) and text in PROVIDER_IDS


def valid_env_name(text: object) -> bool:
    return _full(ENV_NAME, text)


def valid_timestamp(text: object) -> bool:
    """RFC 3339 with three decimals and ``Z`` (``common.v1.json``'s ``timestamp``), and a real UTC calendar instant:
    no February 31st, no year 0000 (the schema's ``x-instant``)."""
    if not _full(TIMESTAMP, text):
        return False
    try:
        datetime.strptime(text[:19] + '+0000', '%Y-%m-%dT%H:%M:%S%z')
    except ValueError:
        return False
    return True


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
    """A cap as the index keeps it: a JSON number (never a boolean), 0 to 100000, two decimals at most.

    Only comparisons are used, and they are total: NaN and both infinities fail the range, and a whole number far
    too large for a float (``10**500``) is compared exactly instead of being converted, which would raise.
    """
    if type(value) not in (int, float):
        return False
    if not 0 <= value <= CAP_MAX:
        return False
    return round(value, 2) == value
