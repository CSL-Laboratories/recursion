"""Lane QUOTA's tables in the one event database, declared once through the transaction helper.

The helper is lane EVENTS' (``events/db.py``): ``db.register_tables(lane, version, ddl)`` once, at composition,
then ``tx.execute(sql, params)`` inside ``with db.transaction() as tx``. A lane declares its tables ONCE, so
every QUOTA table is in the one ``DDL`` below. Version 2 adds the settle row, the usage and effect notes and
the daemon's session spend. A store that already declared version 1 keeps that declaration: nothing here migrates it.

Every table here is append-only: rows are inserted and never updated or deleted. A reservation's state is
its latest open row (``held``, and a later ``provisional`` or ``unresolved`` marker), never a column changed in place.
A ``released`` row is what ends the hold.
"""

from __future__ import annotations

LANE = "quota"
VERSION = 2

# One row per admission decision and per later marker. ``reservation_id`` names one reservation; a reservation has
# at most one row of each state (the unique index). Money is an integer number of micro-USD (``amount_micro_usd``)
# and the same amount as exact decimal text (``amount_usd``), never a float. ``cap_usd`` is the cap the admission
# read (null when the route cannot spend), and ``spent_*`` what admission knew of the connection's spend, with its
# quality and provenance (null value when unknown, never 0). ``paid_fallback`` is a subscription's typed meter
# reading at that admission ('active' or 'inactive'; null when there was none).
#
# ``quota_usage`` is the run's reported cost: null means unknown, and unknown is never stored as 0. The latest row
# for a reservation is the one that counts. ``quota_effect`` records ``outcome_unknown`` and, later, ``reconciled``.
# ``quota_settle`` is one row per reservation id, status ``settled`` or ``unresolved``. ``quota_session_spend`` is
# the daemon's own session book: a cumulative total replaces the earlier one, and ``max_budget_micro_usd`` is the
# engine's budget flag, stored so it can be shown unused.
DDL = """
CREATE TABLE quota_reservation (
    row_id INTEGER PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held', 'released', 'refused', 'provisional', 'unresolved')),
    metered INTEGER NOT NULL CHECK (metered IN (0, 1)),
    basis TEXT,
    reason TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
    amount_micro_usd INTEGER,
    cap_usd TEXT,
    spent_usd TEXT,
    spent_quality TEXT CHECK (spent_quality IS NULL OR spent_quality IN ('known', 'lower_bound', 'unknown')),
    spent_provenance TEXT CHECK (spent_provenance IS NULL
                                 OR spent_provenance IN ('observed', 'modelled', 'inferred', 'unavailable')),
    paid_fallback TEXT CHECK (paid_fallback IS NULL OR paid_fallback IN ('active', 'inactive'))
);
CREATE UNIQUE INDEX quota_reservation_one_row_per_state ON quota_reservation (reservation_id, state);
CREATE INDEX quota_reservation_by_run ON quota_reservation (run_id);
CREATE INDEX quota_reservation_by_connection ON quota_reservation (connection_id);
CREATE TABLE quota_usage (
    row_id INTEGER PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    cost_micro_usd INTEGER
);
CREATE INDEX quota_usage_by_reservation ON quota_usage (reservation_id);
CREATE TABLE quota_effect (
    row_id INTEGER PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('outcome_unknown', 'reconciled'))
);
CREATE INDEX quota_effect_by_run ON quota_effect (run_id);
CREATE TABLE quota_settle (
    row_id INTEGER PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('settled', 'unresolved')),
    amount_micro_usd INTEGER,
    CHECK ((status = 'unresolved' AND amount_micro_usd IS NULL)
           OR (status = 'settled' AND amount_micro_usd IS NOT NULL AND amount_micro_usd >= 0))
);
CREATE UNIQUE INDEX quota_settle_one_row ON quota_settle (reservation_id);
CREATE TABLE quota_session_spend (
    row_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    cost_micro_usd INTEGER,
    reported_total_micro_usd INTEGER,
    max_budget_micro_usd INTEGER
);
CREATE INDEX quota_session_spend_by_session ON quota_session_spend (connection_id, session_id);
"""


def declare(db) -> None:
    """Declare the lane's tables on ``db`` (the helper). Composition calls this once, before the daemon serves."""

    db.register_tables(LANE, VERSION, DDL)
