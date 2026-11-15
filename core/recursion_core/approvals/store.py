"""The approval queue's durable state, in the event store (lane EVENTS' helper), through the transaction a
caller passes in.

Every function here takes ``tx``, an open transaction of the one event store (``with store.transaction()
as tx``), and opens none of its own: the code that opened it owns it, a method handler gets the
dispatcher's (``ctx.tx``), and the queue's own work opens one at a time. Inside a transaction nothing calls
the registry, the daemon or an engine; drawing and telling a driver happen after the commit.

Tables (declared once per process, ``register_tables("approvals", 1, DDL)``; named ``<lane>_<name>`` by
the Liaison's ruling of Nov 14, 4:03 PM):

* ``approvals_records``: one row per request, the whole record as RFC 8785 text plus the columns the
  queue looks up by (state, deadline, log, whether a surface was told of it).
* ``approvals_bindings``: the PRIVATE binding of an engine's request (approval id to the driver's pending
  engine request). It is never on the wire: no method returns it, no event carries it.

Events, in the same transaction as the change they report: ``approval.requested`` (the record as it was
requested, immutable) and ``approval.answered`` (approved, denied, expired; vetoed comes with the plugin
path).
"""

from __future__ import annotations

import json
import logging

from recursion_core.approvals.effects import text_of
from recursion_core.approvals.model import ApprovalRefused, restored
from recursion_core.approvals.record import milliseconds, summary_of

log = logging.getLogger(__name__)

__all__ = ["DDL", "LANE", "VERSION", "answered_event", "binding_of", "due", "insert", "list_summaries", "load",
           "log_of", "mark_shown", "next_deadline", "pending", "requested_event", "retire_binding", "save",
           "set_aside", "unresolved_engine", "unshown"]

LANE = "approvals"
VERSION = 1
DDL = ("CREATE TABLE approvals_records (approval_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
       "expires_ms INTEGER NOT NULL, log_id TEXT NOT NULL, source TEXT NOT NULL, shown INTEGER NOT NULL, "
       "record TEXT NOT NULL); "
       "CREATE TABLE approvals_bindings (approval_id TEXT PRIMARY KEY, binding TEXT NOT NULL)")


def log_of(record: dict) -> str:
    """The log an approval's events go to: its session, or ``run:<run_id>`` for a run with no session."""
    if record["session_id"] is not None:
        return record["session_id"]
    return "run:" + (record["run_id"] if record["run_id"] is not None else record["effect"]["run_id"])


def _envelope(record: dict, kind: str, payload: dict) -> dict:
    return {"session_id": log_of(record), "run_id": record["run_id"], "turn_id": record["turn_id"], "type": kind,
            "source": "core", "trust": "trusted", "payload": payload}


def requested_event(record: dict) -> dict:
    return _envelope(record, "approval.requested", restored(record))


def answered_event(record: dict) -> dict:
    return _envelope(record, "approval.answered", {
        "approval_id": record["approval_id"], "answer": record["answer"], "answered_by": record["answered_by"],
        "revision": record["revision"], "state": record["state"]})


def insert(tx, record: dict, binding: dict | None) -> None:
    """A new request: its row, its private binding (an engine's), and ``approval.requested``, together."""
    tx.execute("INSERT INTO approvals_records VALUES (?, ?, ?, ?, ?, ?, ?)",
               (record["approval_id"], record["state"], milliseconds(record["expires_at"]), log_of(record),
                record["source"], 0, text_of(record)))
    if binding is not None:
        tx.execute("INSERT INTO approvals_bindings VALUES (?, ?)", (record["approval_id"], text_of(binding)))
    tx.append(requested_event(record))


def load(tx, approval_id: str):
    """The record as the core holds it (``restored``), or ``None`` when there is no such row. A row that is
    there but is not a JSON object is ``record_invalid``: it is not an unknown approval."""
    rows = tx.execute("SELECT record FROM approvals_records WHERE approval_id = ?", (approval_id,))
    if not rows:
        return None
    try:
        held = json.loads(rows[0][0])
    except (ValueError, TypeError):
        raise ApprovalRefused("record_invalid") from None
    if type(held) is not dict:
        raise ApprovalRefused("record_invalid")
    return restored(held)


def set_aside(tx, approval_id: str) -> None:
    """A row that cannot be read leaves the pending queries: it is never drawn, answered or swept again."""
    tx.execute("UPDATE approvals_records SET state = 'unreadable' WHERE approval_id = ?", (approval_id,))


def retire_binding(tx, approval_id: str) -> None:
    """The private binding of an engine request whose driver is gone: nothing can carry it out through it."""
    tx.execute("DELETE FROM approvals_bindings WHERE approval_id = ?", (approval_id,))


def unresolved_engine(tx) -> list:
    """Engine requests a restart must deal with: still pending, or approved and not carried out."""
    return [row[0] for row in tx.execute("SELECT approval_id FROM approvals_records WHERE source = 'engine_tool' "
                                         "AND state IN ('pending', 'approved') ORDER BY approval_id")]


def binding_of(tx, approval_id: str):
    rows = tx.execute("SELECT binding FROM approvals_bindings WHERE approval_id = ?", (approval_id,))
    if not rows:
        return None
    try:
        return json.loads(rows[0][0])
    except ValueError:
        return None


def save(tx, record: dict, *, answered: bool = False) -> None:
    """Write a changed record back; with ``answered``, append ``approval.answered`` in the same transaction."""
    tx.execute("UPDATE approvals_records SET state = ?, expires_ms = ?, record = ? WHERE approval_id = ?",
               (record["state"], milliseconds(record["expires_at"]), text_of(record), record["approval_id"]))
    if answered:
        tx.append(answered_event(record))


def mark_shown(tx, approval_id: str) -> None:
    tx.execute("UPDATE approvals_records SET shown = 1 WHERE approval_id = ?", (approval_id,))


def pending(tx) -> list:
    return [row[0] for row in tx.execute("SELECT approval_id FROM approvals_records WHERE state = 'pending' "
                                         "ORDER BY expires_ms, approval_id")]


def due(tx, now_ms: int) -> list:
    """Pending requests whose deadline has come (``expires_at`` at or before now)."""
    return [row[0] for row in tx.execute("SELECT approval_id FROM approvals_records WHERE state = 'pending' "
                                         "AND expires_ms <= ? ORDER BY expires_ms, approval_id", (now_ms,))]


def next_deadline(tx):
    rows = tx.execute("SELECT MIN(expires_ms) FROM approvals_records WHERE state = 'pending'")
    return rows[0][0] if rows else None


def unshown(tx) -> list:
    """Pending requests that committed but whose surfaces were never told (the daemon stopped in between)."""
    return [row[0] for row in tx.execute("SELECT approval_id FROM approvals_records WHERE state = 'pending' "
                                         "AND shown = 0 ORDER BY expires_ms, approval_id")]


def list_summaries(tx, *, session_id=None, state=None, limit: int = 100, after: str | None = None) -> tuple:
    """Summaries (never a whole record, never a binding), in approval-id order, from just after ``after``."""
    clauses, params = [], []
    if session_id is not None:
        clauses.append("log_id = ?")
        params.append(session_id)
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if after is not None:
        clauses.append("approval_id > ?")
        params.append(after)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = tx.execute("SELECT approval_id, record FROM approvals_records" + where
                      + (" AND " if where else " WHERE ") + "state != 'unreadable' ORDER BY approval_id LIMIT ?",
                      (*params, limit + 1))
    summaries = []
    for approval_id, text in rows[:limit]:
        try:
            summaries.append(summary_of(json.loads(text)))
        except (ValueError, TypeError, KeyError):  # an unreadable row cannot be shown or answered
            log.error("approval %s cannot be read; left out of the list", approval_id)
    more = len(rows) > limit
    return summaries, (rows[limit - 1][0] if more and rows[:limit] else None)
