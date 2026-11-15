"""Diagnostics, delivered by a thread of their own, so that no thread of the session waits for stderr.

``serve``'s ``log`` (in the process: one JSON line on stderr per event) gets events from the reader (each
cancellation, each response a client sends, ``initialize``), from the reply queue (a stretch of dropped
answers), from whichever thread finds stdout failed (``output_lost``) and from ``serve`` itself (its last
line). Each of them hands its event to a ``DiagnosticQueue`` (``offer``), which never waits, and goes on;
the queue's thread calls ``log`` with the events in the order they were taken. So a client that captures
stderr and stops reading it never stops the reader: a Stop and a cancellation are still read and acted on,
and every answer still goes out.

BOUND. The queue holds at most ``max_bytes`` of events, each counted as its compact JSON; the event being
delivered counts until it has been delivered, and one event is always taken into an empty queue. An event
that does not fit is dropped and counted; the next event taken is preceded by
``{"event": "diagnostics_dropped", "dropped": n}``, and so is the end (``close``). Diagnostics are best
effort (the 2025-06-18 stdio transport lets a client capture, forward or ignore stderr): nothing the
session does depends on one being delivered.

A ``log`` that raises ends delivery: what is queued is discarded and every later ``offer`` returns
``False``.

``close(last)`` takes ``last`` whatever the bound (``serve``'s last line, so it is never dropped), then
nothing more, and waits until every event taken has been delivered (or discarded). A client that never
reads stderr therefore holds ``serve`` there at its end, as a synchronous last line would; reading and
admission are over by then.
"""

from __future__ import annotations

import collections
import json
import threading
from collections.abc import Callable

MAX_PENDING_DIAGNOSTIC_BYTES = 64 * 1024

Log = Callable[[dict], None]


def _size(event: dict) -> int:
    return len(json.dumps(event, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=repr))


class DiagnosticQueue:
    def __init__(self, log: Log, *, max_bytes: int = MAX_PENDING_DIAGNOSTIC_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._log = log
        self._max_bytes = max_bytes
        self._cond = threading.Condition()
        # (event, size); the first is the one being delivered
        self._pending: collections.deque[tuple[dict, int]] = collections.deque()
        self._bytes = 0
        self._dropped = 0
        self._failed = False
        self._closing = False
        self._thread = threading.Thread(target=self._run, name="mcp-diagnostics", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def offer(self, event: dict) -> bool:
        """Take one event without waiting; ``False`` when it was dropped (no room, closed, or ``log`` failed)."""

        size = _size(event)
        with self._cond:
            if self._failed or self._closing:
                return False
            if self._bytes and self._bytes + size > self._max_bytes:
                self._dropped += 1
                return False
            self._take_dropped_count()
            self._pending.append((event, size))
            self._bytes += size
            self._cond.notify_all()
            return True

    def _take_dropped_count(self) -> None:
        """Under the lock: queue the count of events dropped since the last one taken, if any."""

        if self._dropped:
            note = {"event": "diagnostics_dropped", "dropped": self._dropped}
            self._pending.append((note, _size(note)))
            self._bytes += self._pending[-1][1]
            self._dropped = 0

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._closing and not self._failed:
                    self._cond.wait()
                if self._failed or not self._pending:
                    return
                event, size = self._pending[0]
            try:
                self._log(event)
                delivered = True
            except Exception:  # noqa: BLE001 - a log that cannot take events gets no more of them
                delivered = False
            with self._cond:
                self._pending.popleft()
                self._bytes -= size
                if not delivered:
                    self._failed = True
                    self._pending.clear()
                    self._bytes = 0
                self._cond.notify_all()

    def close(self, last: dict | None = None) -> None:
        """Take ``last`` whatever the bound, then nothing more; wait until every event taken has been
        delivered (or discarded)."""

        with self._cond:
            if not self._closing and not self._failed:
                self._take_dropped_count()
                if last is not None:
                    self._pending.append((last, _size(last)))
                    self._bytes += self._pending[-1][1]
            self._closing = True
            self._cond.notify_all()
            while self._pending:
                self._cond.wait()
