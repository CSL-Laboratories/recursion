"""Lane EVENTS: the one database of the core's shared state and its transaction helper.

The event log, idempotency receipts and the tables other lanes declare (session records, queue items,
approval state, usage) live in ONE SQLite database behind one helper, ``events.db``, so that changes that
belong together commit together. ``events.db.EventStore`` is the helper over a real file;
``events.db.FakeStore`` is the same code over a temporary file with durability off, for other lanes'
tests. The run registry is not here: it stays the daemon's own file and reaches this log afterwards
(lane EVENTS' later slices, through SPINE's change marker).
"""
