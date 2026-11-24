"""What a relay key may reach: rights in the core's one vocabulary, and the relay's table of method to right.

A paired device's rights are chosen by the person when the device is paired, in the core's own words
(``read``, ``run``, ``approve``, ``manage``: ``control.credentials.SCOPES``) plus single methods where a
grant names one. The default of a paired device is watch and Stop, written ``read`` plus the one method
``run.stop`` (``pairing.DEVICE_SCOPES``). The relay forwards a frame only when the key's rights cover its
method, and refuses every other frame itself, before anything reaches the daemon (``needs``).

``METHOD_RIGHTS`` is the relay's copy of the scope column of the core's published method table
(``schemas/control/v1/methods.table.v1.json``, table version ``TABLE_VERSION``). A test pins this copy
against that file, so a row the core adds or moves makes the relay's tests fail until the copy follows.
The socket transport's own two ops, ``ping`` and ``health``, look and change nothing: they need ``read``.
Anything else (an op that is not ``control``, a method the table does not have, a method that is not a
plain string) needs all four rights, as the core's own check treats a name its table does not have.

Pure: nothing here reads a file, the clock or the environment.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# The core's vocabulary of rights, in its own order (control.credentials.SCOPES).
RIGHTS = ("read", "run", "approve", "manage")
ALL_RIGHTS = frozenset(RIGHTS)
# All four rights together: everything. A relay key holds them only where the person's forced command names them.
FULL_RIGHTS = RIGHTS
# What a method the table does not have needs: all four rights together.
NEEDS_ALL = "all"
CONTROL_OP = "control"
TRANSPORT_OPS = {"ping": "read", "health": "read"}
TABLE_VERSION = "1.0-draft.10"
# The scope column of the published method table, row for row, in its order.
METHOD_RIGHTS: Mapping[str, str] = {
    "run.start": "run",
    "run.status": "read",
    "run.list": "read",
    "run.stop": "run",
    "route.list": "read",
    "plugin.list": "read",
    "session.start": "run",
    "session.turn": "run",
    "session.steer": "run",
    "session.queue": "run",
    "session.interrupt": "run",
    "session.resume": "run",
    "session.fork": "run",
    "session.export": "read",
    "session.list": "read",
    "session.status": "read",
    "session.stop": "run",
    "queue.update": "run",
    "queue.cancel": "run",
    "approval.list": "read",
    "approval.open": "approve",
    "approval.answer": "approve",
    "events.read": "read",
    "events.follow": "read",
    "route.explain": "read",
    "operator.read": "read",
    "budget.show": "read",
    "workspace.diff": "read",
    "workspace.checkpoint": "run",
    "workspace.restore": "run",
    "workspace.publish": "manage",
    "plugin.status": "read",
    "plugin.call": "run",
    "key.list": "read",
    "surface.prefs.get": "read",
    "surface.prefs.set": "run",
    "doctor": "read",
    "methods.list": "read",
}


class RightsRefused(ValueError):
    """A list of rights the relay cannot hold. ``word`` is the item refused."""

    def __init__(self, word: object, detail: str) -> None:
        super().__init__(detail)
        self.word = word


def key_rights(words: Iterable[str]) -> tuple[str, ...]:
    """A key's rights, checked and in one order: whole rights first (``RIGHTS`` order), then single methods
    (table order). An empty list, a repeated word and a word that is neither a right nor a method of the
    table are refused (``RightsRefused``); so is one string in place of a list."""
    if isinstance(words, (str, bytes)):
        raise RightsRefused(words, "rights are a list of words, not one string")
    try:
        items = tuple(words)
    except TypeError:
        raise RightsRefused(words, "rights are a list of words") from None
    if not items:
        raise RightsRefused(items, "a key needs at least one right")
    for word in items:
        if type(word) is not str or (word not in ALL_RIGHTS and word not in METHOD_RIGHTS):
            raise RightsRefused(word, f"unknown right {word!r}")
    if len(set(items)) != len(items):
        raise RightsRefused(items, "a right is named twice")
    return (tuple(right for right in RIGHTS if right in items)
            + tuple(method for method in METHOD_RIGHTS if method in items))


def frame_method(frame: Mapping[str, Any]) -> tuple[str, str]:
    """``(name, right)``: what the frame asks for, and the right that covers it (``NEEDS_ALL`` when none does).

    The name is the method of a ``control`` frame, the op of a transport op, and ``op:<op>`` for any other
    op; it is for the refusal's text only, never looked up again."""
    op = frame.get("op")
    if type(op) is str and op == CONTROL_OP:
        method = frame.get("method")
        if type(method) is str and method in METHOD_RIGHTS:
            return method, METHOD_RIGHTS[method]
        return (method if type(method) is str else f"{type(method).__name__}"), NEEDS_ALL
    if type(op) is str and op in TRANSPORT_OPS:
        return op, TRANSPORT_OPS[op]
    return f"op:{op if type(op) is str else type(op).__name__}", NEEDS_ALL


def needs(rights: Iterable[str], frame: Mapping[str, Any]) -> tuple[str, str] | None:
    """``None`` when ``rights`` cover the frame, else ``(name, right it needs)``.

    A whole right covers every method of its row; a single method covers that method only. A key holding
    all four rights is covered for everything, a name the table does not have included."""
    held = frozenset(rights)
    if held >= ALL_RIGHTS:
        return None
    name, right = frame_method(frame)  # NEEDS_ALL is never held: only all four together reach such a frame
    if right in held or (name in METHOD_RIGHTS and name in held):
        return None
    return name, right
