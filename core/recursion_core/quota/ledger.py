"""Read one settle row per reservation id back from the store (F9).

Writing the row is ``Reservations.settle``. This module only reads. A new ``Ledger``
on the same store sees the rows already written. It does not fold usage events,
read meters, or replay the event log.
"""

from __future__ import annotations

import re

_RESERVATION_ID = re.compile(r"rsv-[0-9a-f]{32}")
_ONE = ("SELECT reservation_id, run_id, status, amount_micro_usd FROM quota_settle "
        "WHERE reservation_id = ?")
_ALL = ("SELECT reservation_id, run_id, status, amount_micro_usd FROM quota_settle ORDER BY row_id")


def _row(raw) -> dict:
    reservation_id, run_id, status, amount = raw
    return {"reservation_id": reservation_id, "run_id": run_id, "status": status,
            "amount_micro_usd": None if amount is None else int(amount)}


class Ledger:
    """The settle rows of one store. Reading writes nothing."""

    def __init__(self, db) -> None:
        self._db = db

    def read(self, reservation_id: str) -> dict | None:
        """The settle row for ``reservation_id``, or None when that id has none.

        A reservation id that is not ``rsv-`` plus 32 hex digits is refused. Unknown
        stays null: an unresolved row's amount is None, never 0.
        """

        if not isinstance(reservation_id, str) or not _RESERVATION_ID.fullmatch(reservation_id):
            raise ValueError("reservation_id")
        with self._db.transaction() as tx:
            found = tx.execute(_ONE, (reservation_id,))
        if not found:
            return None
        return _row(found[0])

    def read_all(self) -> list[dict]:
        """Every settle row, in the order they were written."""

        with self._db.transaction() as tx:
            found = tx.execute(_ALL)
        return [_row(raw) for raw in found]
