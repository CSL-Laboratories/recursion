# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""Lane SESSIONS: a session a person can talk to, above runs.

A RUN is one supervised unit with its own Stop. A SESSION is a durable record with turns, a queue of
messages and at most one live unit. This package keeps that record and decides what may happen to it; it
starts no unit, writes nothing to an engine and calls no daemon method itself. What a session needs from
outside (a unit launched, an input written, a Stop asked) is committed as a typed WORK INTENT in the same
transaction as the command's receipt, and the lane's service (``service.py``) carries it out, records the
outcome, and recovers it after a restart. The engine side is reached only through ``ports.py``.

The modules:

``records``   ids, times, and the checks every stored record passes before it is written (session.v1,
              queue.item.v1 and the private delivery binding, as #368 publishes them).
``tables``    the lane's tables in the event store, all named ``sessions_<name>``.
``store``     reads and writes of those tables through the event store's transaction (``tx``).
``machine``   the turn state machine: the only code that changes a session record.
``delivery``  the queue and its delivery rule (queue.item.v1 and the queue.delivery vectors).
``events``    the event.v1 envelopes this lane appends.
``ports``     what the lane needs from the engine side, as interfaces; nothing here implements them.
``service``   the work intents, the service that carries them out, and recovery after a restart.
``hooks``     the registrant ``control/hooks.py`` imports by name. It registers NOTHING until the seams
              merge (the lane's start note); ``install`` does the registering, for tests.

The method handlers are in ``control/methods/session.py``.
"""
