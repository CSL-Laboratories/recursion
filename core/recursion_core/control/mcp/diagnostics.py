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

A ``log`` that raises, whatever it raises, ends delivery: what is queued is discarded, every later ``offer`` returns ``False``,
and ``close`` takes no ``last`` (that log takes nothing more).

``close(last, timeout)`` takes ``last`` whatever the bound (``serve``'s last line, so the bound never drops
it), then nothing more, and waits until every event taken has been delivered (or discarded) or until
``timeout`` seconds have passed, whichever comes first (``None``, the default, is no limit); it returns
``True`` when nothing is left: every event delivered, or discarded by a log that failed.
``serve`` gives it ``DIAGNOSTIC_DRAIN_S``, so a client that never reads stderr does not keep the process
alive after end of input: what stderr has not taken by then is lost with the queue's thread, a daemon
thread. ``close`` starts the thread if ``start`` was never called, so events offered before it are still
delivered.
"""

from __future__ import annotations

import collections
import json
import threading
from collections.abc import Callable

MAX_PENDING_DIAGNOSTIC_BYTES = 64 * 1024
DIAGNOSTIC_DRAIN_S = 2.0
MAX_WAIT_S = 24 * 60 * 60  # a day: far below every platform's threading.TIMEOUT_MAX (Windows: about 49 days)

Log = Callable[[dict], None]


def check_seconds(value: float | None, name: str) -> None:
    """``ValueError`` unless ``value`` is ``None`` (no limit) or a number of seconds from 0 to ``MAX_WAIT_S``
    (a day); a bool is not a number here. NaN, an infinity and anything over a day are refused: a wait longer
    than ``threading.TIMEOUT_MAX`` raises ``OverflowError`` only once it begins, after the work it follows,
    and ``None`` already means no limit."""

    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= MAX_WAIT_S:
        raise ValueError(f"{name} must be None or a number of seconds from 0 to {MAX_WAIT_S}")


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
        self._started = False
        self._thread = threading.Thread(target=self._run, name="mcp-diagnostics", daemon=True)

    def start(self) -> None:
        """Start the queue's thread; once only (a second call does nothing)."""

        with self._cond:
            if self._started:
                return
            self._started = True
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
            except BaseException:  # noqa: BLE001 - a log that cannot take events gets no more of them
                delivered = False
            with self._cond:
                self._pending.popleft()
                self._bytes -= size
                if not delivered:
                    self._failed = True
                    self._pending.clear()
                    self._bytes = 0
                self._cond.notify_all()

    def close(self, last: dict | None = None, timeout: float | None = None) -> bool:
        """Take ``last`` whatever the bound, then nothing more; wait until every event taken has been
        delivered (or discarded), at most ``timeout`` seconds (``None``: no limit). ``True`` when nothing
        is left."""

        check_seconds(timeout, "timeout")  # before anything changes
        self.start()  # a queue never started still delivers what it took
        with self._cond:
            if not self._closing and not self._failed:
                self._take_dropped_count()
                if last is not None:
                    self._pending.append((last, _size(last)))
                    self._bytes += self._pending[-1][1]
            self._closing = True
            self._cond.notify_all()
            return self._cond.wait_for(lambda: not self._pending, timeout)
