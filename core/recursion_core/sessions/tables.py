# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The lane's tables in the event store.

They are declared once, at composition, through the event store's helper (``store.register_tables``), and
reached only through the ``tx`` of a transaction (``tx.execute``). Every name begins ``sessions_`` (the
Liaison's ruling of 4:03 PM, Nov 14, 2026). No second database and no second connection is opened for any
of this state: a session, its turns, its queue, its deliveries and its work intents commit together with
the command's receipt and its events, or not at all.

``sessions_session``   one row per session: the session.v1 record WITHOUT its turns, and the columns a
                       list is paged and filtered by. ``unit_run_id`` is the run of the session's worker, or
                       null when it has none, so that recovery finds every session that may have a unit.
``sessions_turn``      one row per turn: the turn as session.v1 writes it.
``sessions_item``      one row per queued message: the queue.item.v1 record.
``sessions_delivery``  one row per delivery attempt: the PRIVATE binding committed before anything is written
                       toward an engine (queue.item.v1, $defs/delivery_binding), the record of the write, and
                       any late acceptance kept as evidence. A turn's own input (the prompt of
                       ``session.start``, the text of ``session.turn``) is delivered under the same rule and
                       has no item: ``item_id`` is null for it. An item has at most one delivery, ever.
``sessions_work``      one row per work intent: what a command needs done outside the store, with a stable
                       operation id and its recovery state (``service.py``).
"""
from __future__ import annotations

LANE = 'sessions'
VERSION = 1

DDL = """
CREATE TABLE sessions_session (
    session_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('idle', 'live', 'stop_pending', 'ended')),
    parent_session_id TEXT,
    unit_run_id TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    record TEXT NOT NULL
);
CREATE INDEX sessions_session_by_tenant ON sessions_session (tenant, session_id);
CREATE INDEX sessions_session_with_unit ON sessions_session (unit_run_id);
CREATE TABLE sessions_turn (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions_session (session_id),
    turn_index INTEGER NOT NULL CHECK (turn_index >= 0),
    record TEXT NOT NULL,
    UNIQUE (session_id, turn_index)
);
CREATE TABLE sessions_item (
    item_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions_session (session_id),
    item_order INTEGER NOT NULL CHECK (item_order >= 1),
    state TEXT NOT NULL CHECK (state IN ('queued', 'delivering', 'delivered', 'cancelled', 'outcome_unknown')),
    record TEXT NOT NULL,
    UNIQUE (session_id, item_order)
);
CREATE TABLE sessions_delivery (
    delivery_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions_session (session_id),
    item_id TEXT UNIQUE REFERENCES sessions_item (item_id),
    run_id TEXT NOT NULL,
    worker_incarnation TEXT NOT NULL,
    target_turn_id TEXT NOT NULL,
    how TEXT NOT NULL CHECK (how IN ('new_turn', 'mid_turn')),
    state TEXT NOT NULL CHECK (state IN ('delivering', 'delivered', 'outcome_unknown')),
    written INTEGER NOT NULL CHECK (written IN (0, 1)),
    evidence TEXT NOT NULL
);
CREATE INDEX sessions_delivery_open ON sessions_delivery (state, session_id);
CREATE TABLE sessions_work (
    operation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions_session (session_id),
    kind TEXT NOT NULL CHECK (kind IN ('launch', 'send', 'interrupt', 'stop')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'attempting', 'done', 'failed', 'outcome_unknown')),
    payload TEXT NOT NULL,
    outcome TEXT
);
CREATE INDEX sessions_work_by_state ON sessions_work (state, operation_id);
"""

TABLES = ('sessions_session', 'sessions_turn', 'sessions_item', 'sessions_delivery', 'sessions_work')


def declare(store) -> None:
    """Declare the lane's tables on the event store's helper. Once, at composition."""
    store.register_tables(LANE, VERSION, DDL)
