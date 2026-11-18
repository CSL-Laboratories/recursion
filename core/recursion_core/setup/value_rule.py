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
* a value that starts with a known credential prefix is withheld whatever its shape.

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
    "pypi-", "eyj",
)
APPROVAL_WORDS = frozenset({"untrusted", "on-failure", "on-request", "never"})
SANDBOX_WORDS = frozenset({"read-only", "workspace-write", "danger-full-access"})

_MODEL_SHAPE = re.compile(r"[A-Za-z0-9._:/-]{1,64}")
_NAME_SHAPE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MIN_SHARED = 4


def credential_shaped(value: str) -> bool:
    folded = value.lower()
    return any(folded.startswith(prefix) for prefix in CREDENTIAL_PREFIXES)


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
