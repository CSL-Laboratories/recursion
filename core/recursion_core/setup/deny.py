"""Sign-in paths a setup check must never open.

The list is seeded from the stores the daemon already names
(``daemon.engine_user.SIGNIN_STORES`` and ``subscription_routes.policy.ENGINE_STORE_ENTRIES``)
and from ``~/.claude/.credentials.json``, which that sign-in table leaves unconfirmed.
A path here is a file or a directory prefix. This module only compares path text.
It opens nothing.
"""

from __future__ import annotations

import os

from recursion_core.daemon.engine_user import SIGNIN_STORES
from recursion_core.subscription_routes.policy import ENGINE_STORE_ENTRIES

# Claude Code's credential file is not a confirmed store_path. It is still denied.
_CLAUDE_CREDENTIALS = ".claude/.credentials.json"


def denied_paths(home: str) -> tuple[str, ...]:
    """Absolute paths under ``home`` that a check must not open, in a stable order."""

    if type(home) is not str or not home:
        return ()
    root = os.path.abspath(home)
    found: list[str] = []
    seen: set[str] = set()

    def add(raw) -> None:
        path = _place(root, raw)
        if path is None or path in seen:
            return
        seen.add(path)
        found.append(path)

    add(_CLAUDE_CREDENTIALS)
    for entry in ENGINE_STORE_ENTRIES:
        add(entry)
    for row in SIGNIN_STORES.values():
        store = row.get("store_path") if isinstance(row, dict) else None
        add(store)
    return tuple(found)


def is_denied(path, home: str) -> bool:
    """True when ``path`` is a denied path or a file inside a denied directory."""

    text = _decode(path)
    if text is None or type(home) is not str or not home:
        return False
    try:
        text = os.path.abspath(text)
    except (TypeError, ValueError, OSError):
        return False
    for denied in denied_paths(home):
        if text == denied:
            return True
        prefix = denied if denied.endswith(os.sep) else denied + os.sep
        if text.startswith(prefix):
            return True
    return False


def _place(home: str, raw) -> str | None:
    if type(raw) is not str or not raw or raw == "unconfirmed" or raw.startswith("unconfirmed"):
        return None
    if raw.startswith("~/"):
        return os.path.abspath(os.path.join(home, raw[2:]))
    if raw.startswith("~"):
        rest = raw[1:].lstrip("/")
        return os.path.abspath(os.path.join(home, rest)) if rest else None
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(home, raw))


def _decode(path):
    if isinstance(path, bytes):
        try:
            return os.fsdecode(path)
        except (UnicodeDecodeError, ValueError):
            return None
    if isinstance(path, os.PathLike):
        try:
            return os.fspath(path)
        except TypeError:
            return None
    if type(path) is str:
        return path
    return None
