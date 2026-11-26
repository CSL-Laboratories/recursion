"""The answers the reader owes, written by a thread of their own, so that the reader never waits for stdout.

The reader of ``server.serve`` answers some requests itself: refusals (parse errors, invalid requests,
unknown methods, invalid params, ``Server busy``, a worker that could not be started), ``ping``,
``initialize`` and ``tools/list``. It hands each answer to a ``ReplyQueue`` (``offer``) and goes on
reading; the queue's thread writes the answers in the order they were handed over, through the same
``FrameWriter`` as the calls' answers. So a Stop or a cancellation that comes after any number of such
answers is read and acted on while stdout is slow or stalled.

EXHAUSTION. The queue holds at most ``max_bytes`` of encoded answers; the answer being written counts until
it has been written. An answer is always taken into an empty queue, whatever its size. An answer that does
not fit is dropped: ``offer`` returns ``False`` and nothing is written for that request. Dropping it drops
no effect: every answer the reader owes is for a request that sent nothing to the daemon, and the one
whose answer changes the session's state (``initialize``) changes it only when its answer was taken
(``server.LegacySession._initialize``). The client gets no answer for that request; the 2025-06-18
lifecycle ("Timeouts") has it give up waiting and send a cancellation, which is ignored (the request is
not in flight), and it may send the request again. Only a client that has stopped reading stdout while it
goes on writing fills the queue. Stop calls and cancellations need no answer from the reader to be
admitted, so they are read, admitted and sent whatever the queue holds (a Stop refused ``Server busy``
because its own queue is full is refused like any other call). The first answer dropped after one was
taken logs ``{"event": "replies_dropped", "pending_bytes": n}``; the next answer taken logs
``{"event": "replies_resumed", "dropped": count}``: two lines per stretch of drops, however long. Both go
to ``log`` through a ``diagnostics.DiagnosticQueue`` of the reply queue's own, so ``offer`` never waits for
``log`` either (a client that stops reading stderr too).

OUTPUT FAILURE. When a write returns ``False`` (stdout failed, at this write or at an earlier one, a
call's answer included: the writer is one), or raises, the answers still queued are discarded and every
later ``offer`` returns ``False``; nothing is logged for them (``server.py``, OUTPUT FAILURE, logs the failure
once).

``close(timeout)`` waits until every answer taken has been written, or discarded, and ends the thread. That
wait has no limit, since an answer is never dropped: a client that stops reading stdout while keeping it
open holds it until it reads again or stdout fails. It always ends with the write under way, since a write
that raises is a failed write (OUTPUT FAILURE) and the thread never ends with an answer still pending. Then
it gives the queue's diagnostics at most ``timeout`` seconds (``DiagnosticQueue.close``;
``DIAGNOSTIC_DRAIN_S`` unless given; ``None`` is no limit), so a log that has stopped taking events does not
hold it. It starts the threads if ``start`` was never called, so answers
offered before it are still written.
"""

from __future__ import annotations

import collections
import threading
from collections.abc import Callable

from .diagnostics import DIAGNOSTIC_DRAIN_S, DiagnosticQueue, check_seconds
from .framing import FrameWriter, encode

MAX_PENDING_REPLY_BYTES = 1024 * 1024

Log = Callable[[dict], None]


def _quiet(_event: dict) -> None:
    pass


class ReplyQueue:
    def __init__(self, writer: FrameWriter, *, max_bytes: int = MAX_PENDING_REPLY_BYTES,
                 log: Log | None = None) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._writer = writer
        self._max_bytes = max_bytes
        self._diagnostics = DiagnosticQueue(log or _quiet)
        self._cond = threading.Condition()
        # (message, encoded size); the first is the one being written
        self._pending: collections.deque[tuple[dict, int]] = collections.deque()
        self._bytes = 0
        self._dropped = 0
        self._lost = False
        self._closing = False
        self._started = False
        self._thread = threading.Thread(target=self._run, name="mcp-replies", daemon=True)

    def start(self) -> None:
        """Start the queue's thread and its diagnostics' thread; once only (a second call does nothing)."""

        with self._cond:
            if self._started:
                return
            self._started = True
        self._diagnostics.start()
        self._thread.start()

    def offer(self, message: dict) -> bool:
        """Take one answer without waiting; ``False`` when it was dropped (no room, or output lost). A
        message JSON cannot hold raises, as ``framing.encode`` does; the reader builds none."""

        size = len(encode(message))  # what a direct write would refuse is refused here, on the reader's thread
        event = None
        with self._cond:
            if self._lost or self._closing:
                return False
            if self._bytes and self._bytes + size > self._max_bytes:
                self._dropped += 1
                if self._dropped == 1:
                    event = {"event": "replies_dropped", "pending_bytes": self._bytes}
                taken = False
            else:
                if self._dropped:
                    event = {"event": "replies_resumed", "dropped": self._dropped}
                    self._dropped = 0
                self._pending.append((message, size))
                self._bytes += size
                self._cond.notify_all()
                taken = True
        if event is not None:
            self._diagnostics.offer(event)  # never waits for log (EXHAUSTION)
        return taken

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._closing and not self._lost:
                    self._cond.wait()
                if self._lost or not self._pending:
                    return
                message, size = self._pending[0]
            try:
                written = self._writer.write(message)
            except BaseException:  # noqa: BLE001 - a writer that raises wrote nothing, and never will
                written = False
            with self._cond:
                self._pending.popleft()
                self._bytes -= size
                if not written:  # stdout has failed: nothing queued will ever be written
                    self._lost = True
                    self._pending.clear()
                    self._bytes = 0
                self._cond.notify_all()

    def close(self, timeout: float | None = DIAGNOSTIC_DRAIN_S) -> bool:
        """Wait until every answer taken has been written (or discarded), with no limit: an answer is never
        dropped. Then give the queue's diagnostics at most ``timeout`` seconds (``None``: no limit); ``True``
        when nothing of theirs is left (delivered, or discarded by a log that failed)."""

        check_seconds(timeout, "timeout")  # before anything changes
        self.start()  # a queue never started still writes what it took
        with self._cond:
            while self._pending:
                self._cond.wait()
            self._closing = True
            self._cond.notify_all()
        return self._diagnostics.close(timeout=timeout)
