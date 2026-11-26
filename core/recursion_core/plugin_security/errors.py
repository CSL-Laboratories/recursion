"""One refusal type with a stable code, shared by every part of the plugin-security core."""

from __future__ import annotations

import errno
import os

__all__ = ["Refused"]


class Refused(Exception):
    """A typed refusal. ``code`` is stable and goes on the wire; ``detail`` is never secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def refuse_sqlite_wal_bug(choice: str, *, creating: str | None = None) -> None:
    """Before a plugin store connects: refuse ``store_sqlite_wal_bug`` on an SQLite with the WAL reset bug unless the
    person's choice is 'accept_risk' (the one rule: ``recursion_core.events.db.sqlite_wal_refusal``).

    The order is the order of what stops the call: a choice that is not a value of the setting first
    (``store_sqlite_wal_bug_unknown_choice``, on every SQLite); then, for an installer step that makes the file
    ``creating`` exclusively, a path that is already there (the same ``FileExistsError`` its exclusive create
    raises, so a caller that reads it as 'already installed' is told that, on every SQLite); then the version."""
    from recursion_core.events.db import SQLITE_WAL_BUG, SQLITE_WAL_BUG_UNKNOWN_CHOICE, sqlite_wal_choice_refusal
    from recursion_core.events.db import sqlite_wal_refusal
    message = sqlite_wal_choice_refusal(choice)
    if message is not None:
        raise Refused(SQLITE_WAL_BUG_UNKNOWN_CHOICE, message)
    if creating is not None and os.path.lexists(creating):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), creating)
    message = sqlite_wal_refusal(choice)
    if message is not None:
        raise Refused(SQLITE_WAL_BUG, message)


def sqlite_wal_accepted(choice: str) -> None:
    """After a plugin store's open has succeeded: the 'accept_risk' WARNING (``recursion_core.events.db``)."""
    from recursion_core.events.db import sqlite_wal_accepted as accepted
    accepted(choice)
