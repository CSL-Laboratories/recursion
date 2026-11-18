"""One rule for every string a settings file gives the record or the output.

``observe`` copies a model name, an approval mode, a sandbox mode, MCP server names and
the program name of an MCP command. Each passes this rule first:

* a model name has the model-name shape (letters, digits and ``. _ : / -``, 64
  characters at most) and is not equal to, and does not contain, another string value of
  4 characters or more anywhere in the same file;
* an approval or sandbox mode is kept only when it is a word of the lists below, else
  ``unknown``;
* a server name or a program name has the shape letters, digits, ``. _ -``, 64
  characters at most;
* a value is withheld whatever its field when it is key-shaped (``credential_shaped``):
  it starts with a known credential prefix, or a part of it between ``/ : @ ? # & = ,``
  or a space is 20 characters or more and starts with one, or it holds a random run.

A random run: inside a token of 20 or more characters drawn from letters, digits and
``- _ . + / =``, an unbroken run of 10 or more letters and digits whose class runs (a
lowercase word with an optional capital, an uppercase run, a digit run) are on average
shorter than 4 characters. Words, camel case, dates and version numbers have long class
runs; hex and base64 keys change class every two characters or so. A long harmless value
that looks random (a git hash, a UUID-like id, a run such as ``OpenAIGPT4xyz`` in a
long token) is withheld too: that is the rule's cost. A key of one class only (all
lowercase letters, or all digits) is not caught by the shape, only by its prefix.

The Codex ``[profiles.*]`` tables may repeat the top-level model; such a copy is not
counted as another value (``model_others``).

A withheld value is replaced by the plain word ``withheld``; no part of it is kept.
"""

from __future__ import annotations

import re

WITHHELD = "withheld"
UNKNOWN = "unknown"

# Value prefixes of common keys and tokens, compared without case.
CREDENTIAL_PREFIXES = (
    "sk-", "sk_", "pk_", "rk_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "glpat-", "xoxa-", "xoxb-", "xoxp-", "xoxs-", "akia", "asia", "aiza", "hf_", "npm_",
    "pypi-", "eyj", "xai-", "gsk_", "r8_", "pplx-", "nvapi-", "sg.", "ya29.", "glsa_", "dop_v1_",
    "shpat_", "whsec_", "xapp-", "xoxe",
)
APPROVAL_WORDS = frozenset({"untrusted", "on-failure", "on-request", "never"})
SANDBOX_WORDS = frozenset({"read-only", "workspace-write", "danger-full-access"})

_MODEL_SHAPE = re.compile(r"[A-Za-z0-9._:/-]{1,64}")
_NAME_SHAPE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MIN_SHARED = 4
_PARTS = re.compile(r"[/:@?#&=,\s]+")
_MIN_PART = 20
_TOKEN = re.compile(r"[A-Za-z0-9_.+/=-]{20,}")
_RUN = re.compile(r"[A-Za-z0-9]{10,}")
_CLASS_RUN = re.compile(r"[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_WORD_MEAN = 4


def credential_shaped(value: str) -> bool:
    folded = value.lower()
    if folded.startswith(CREDENTIAL_PREFIXES):
        return True
    for part in _PARTS.split(folded):
        if len(part) >= _MIN_PART and part.startswith(CREDENTIAL_PREFIXES):
            return True
    return random_shaped(value)


def random_shaped(value: str) -> bool:
    """True when a token of 20 or more characters holds a run whose class runs average under 4."""

    for token in _TOKEN.finditer(value):
        for run in _RUN.finditer(token.group()):
            text = run.group()
            if len(text) < _WORD_MEAN * len(_CLASS_RUN.findall(text)):
                return True
    return False


def model_value(value: str, others) -> str:
    """The model name, or ``withheld``. ``others`` are the file's other string values."""

    if credential_shaped(value) or not _MODEL_SHAPE.fullmatch(value):
        return WITHHELD
    for other in others:
        if len(other) >= _MIN_SHARED and other in value:
            return WITHHELD
    return value


def mode_value(value: str, words) -> str:
    """An approval or sandbox mode from ``words``, else ``unknown``."""

    if credential_shaped(value) or value not in words:
        return UNKNOWN
    return value


def name_value(value: str) -> str:
    """A server name or a program name, or ``withheld``."""

    if credential_shaped(value) or not _NAME_SHAPE.fullmatch(value):
        return WITHHELD
    return value


def model_others(parsed: dict, model: str) -> list:
    """The file's other string values for ``model_value``, without a profile's copy of the model."""

    others = string_values(parsed, skip="model")
    profiles = parsed.get("profiles")
    if type(profiles) is dict:
        for profile in profiles.values():
            if type(profile) is dict and profile.get("model") == model:
                others.remove(model)
    return others


def string_values(data, skip=None) -> list:
    """Every string value in ``data`` (not the keys), except the one object ``skip`` at the top."""

    found = []
    stack = [data]
    first = True
    while stack:
        item = stack.pop()
        if type(item) is dict:
            for key, value in item.items():
                if first and key == skip:
                    continue
                stack.append(value)
        elif type(item) is list:
            stack.extend(item)
        elif type(item) is str:
            found.append(item)
        first = False
    return found
