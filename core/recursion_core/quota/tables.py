"""Lane QUOTA's tables in the one event database, declared once through the transaction helper.

The helper is lane EVENTS' (``events/db.py``): ``db.register_tables(lane, version, ddl)`` once, at composition,
then ``tx.execute(sql, params)`` inside ``with db.transaction() as tx``. A lane declares its tables ONCE, so
every QUOTA table is in the one ``DDL`` below; the ledger's tables join it in slice 2 under a new version.

Every table here is append-only: rows are inserted and never updated or deleted. A reservation's state is
its latest row (``held``, then ``released``), never a column changed in place.
"""

from __future__ import annotations

LANE = "quota"
VERSION = 1

# One row per admission decision and per release. ``reservation_id`` names one reservation; a reservation has at
# most one ``held`` row and one ``released`` row (the unique index), and a refused admission has one ``refused``
# row of its own. Money is kept as decimal text, never a float: ``amount_usd`` is what the hold reserves ('0'
# when nothing may be spent), ``cap_usd`` the cap the admission read (null when the route cannot spend), and
# ``spent_*`` what admission knew of the connection's spend, with its quality and provenance (null value when
# unknown, never 0). ``paid_fallback`` is a subscription's typed meter reading at that admission ('active' or
# 'inactive'; null when there was none): the latest one stands while the meters give no reading.
DDL = """
CREATE TABLE quota_reservation (
    row_id INTEGER PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held', 'released', 'refused')),
    metered INTEGER NOT NULL CHECK (metered IN (0, 1)),
    basis TEXT,
    reason TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
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
"""


def declare(db) -> None:
    """Declare the lane's tables on ``db`` (the helper). Composition calls this once, before the daemon serves."""

    db.register_tables(LANE, VERSION, DDL)
