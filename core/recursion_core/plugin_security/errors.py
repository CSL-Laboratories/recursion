"""One refusal type with a stable code, shared by every part of the plugin-security core."""

from __future__ import annotations

__all__ = ["Refused"]


class Refused(Exception):
    """A typed refusal. ``code`` is stable and goes on the wire; ``detail`` is never secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def refuse_sqlite_wal_bug(choice: str) -> None:
    """Before a plugin store connects: refuse ``store_sqlite_wal_bug`` on an SQLite with the WAL reset bug unless the
    person's choice is 'accept_risk' (the one rule: ``recursion_core.events.db.sqlite_wal_refusal``)."""
    from recursion_core.events.db import SQLITE_WAL_BUG, sqlite_wal_refusal
    message = sqlite_wal_refusal(choice)
    if message is not None:
        raise Refused(SQLITE_WAL_BUG, message)
