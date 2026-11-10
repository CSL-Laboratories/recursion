"""Request ids and the one close deadline for the control transport (CSL-719).

A reply is delivered only for the id that is still in flight. ``True`` is not ``1``:
JSON booleans and numbers are different ids, and a bool must never be used as a dict
key next to an int (they compare equal in Python). Close arms one deadline for every
handler. A handler still running at that deadline is cancelled, and a result that
arrives afterwards is ignored.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

MISSING_ID = "missing_id"
DUPLICATE_ID = "duplicate_id"
REPLAYED_ID = "replayed_id"
UNKNOWN_ID = "unknown_id"
CANCELLED = "cancelled"
CLOSING = "closing"

ACCEPTED = "accepted"
IGNORED = "ignored"
MISMATCH = "mismatch"

Clock = Callable[[], float]


def usable_id(request_id: object) -> bool:
    """A correlation id is a non-empty string or an int. Booleans are not ints here."""
    if isinstance(request_id, bool):
        return False
    if isinstance(request_id, int):
        return True
    return isinstance(request_id, str) and request_id != ""


def same_id(left: object, right: object) -> bool:
    """Equal ids of the same type. ``True`` does not match ``1``."""
    return type(left) is type(right) and left == right


@dataclass(frozen=True)
class Claim:
    """One attempt to run a handler for an id, or a refusal to run it."""

    accepted: bool
    code: str | None
    request_id: str | int | None
    event: threading.Event
    token: int | None


class _Record:
    def __init__(
        self,
        request_id: str | int | None,
        event: threading.Event,
        key: tuple[object, str | int] | None,
    ) -> None:
        self.request_id = request_id
        self.event = event
        self.key = key


class RequestTable:
    """In-flight ids for one server, and the shared close deadline."""

    def __init__(self, *, clock: Clock = time.monotonic, close_timeout_s: float = 5.0) -> None:
        if close_timeout_s < 0:
            raise ValueError("close_timeout_s must be >= 0")
        self._clock = clock
        self._close_timeout_s = close_timeout_s
        self._lock = threading.Lock()
        self._inflight: dict[int, _Record] = {}
        # Keyed by (connection scope, id). Two connections may both use id 0.
        # A bool is never a key: it would collide with 0 or 1.
        self._by_id: dict[tuple[object, str | int], int] = {}
        self._settled: dict[tuple[object, str | int], str] = {}
        self._deadline: float | None = None
        self._seq = 0

    def claim(self, request_id: object, *, scope: object = None) -> Claim:
        """Reserve ``request_id`` within ``scope`` (one connection) and return a claim.

        An unusable id is ``missing_id`` and does not run. An id already in flight in
        that scope is ``duplicate_id``. An id that already finished or was cancelled
        in that scope is ``replayed_id``. Another scope may use the same id.
        """
        with self._lock:
            if not usable_id(request_id):
                return self._refused(MISSING_ID, None)
            assert isinstance(request_id, (str, int))
            if self._past_deadline_locked():
                return self._refused(CLOSING, request_id)
            key = (scope, request_id)
            if key in self._by_id:
                return self._refused(DUPLICATE_ID, request_id)
            if key in self._settled:
                return self._refused(REPLAYED_ID, request_id)
            return self._start_locked(request_id, key)

    def claim_absent(self) -> Claim:
        """An omitted id. It is served, and it is not a duplicate of any other omitted id."""
        with self._lock:
            if self._past_deadline_locked():
                return self._refused(CLOSING, None)
            return self._start_locked(None, None)

    def finish(self, claim: Claim, response: dict[str, object]) -> str:
        """``accepted``, ``mismatch``, or ``ignored``.

        Ignored means the id was cancelled, the close deadline has passed, or this claim
        never owned the id. The caller must not write ``response`` in that case. A response
        whose id is present and is not this request's id is ``mismatch``: the payload is
        not this request's reply, and the id is spent so it cannot run again.
        """
        with self._lock:
            if not claim.accepted or claim.token is None or claim.token not in self._inflight:
                return IGNORED
            record = self._inflight[claim.token]
            if record.event.is_set() or self._past_deadline_locked():
                self._settle_locked(claim.token, CANCELLED)
                return IGNORED
            if (
                claim.request_id is not None
                and isinstance(response, dict)
                and "id" in response
                and not same_id(response.get("id"), claim.request_id)
            ):
                self._settle_locked(claim.token, MISMATCH)
                return MISMATCH
            self._settle_locked(claim.token, "done")
            return ACCEPTED

    def cancel(self, request_id: object) -> str:
        """Cancel one in-flight id. Unknown, missing, and already-settled ids do not guess."""
        with self._lock:
            if not usable_id(request_id):
                return MISSING_ID
            assert isinstance(request_id, (str, int))
            tokens = [token for key, token in self._by_id.items() if same_id(key[1], request_id)]
            if len(tokens) > 1:
                # Two connections are in flight with this id. Cancelling one of them
                # would be a guess, so cancel neither.
                return DUPLICATE_ID
            if len(tokens) == 1:
                self._settle_locked(tokens[0], CANCELLED)
                return CANCELLED
            states = [state for key, state in self._settled.items() if same_id(key[1], request_id)]
            if not states:
                return UNKNOWN_ID
            if all(state == CANCELLED for state in states):
                return CANCELLED
            return REPLAYED_ID

    def begin_close(self) -> float:
        """Arm the single close deadline. A second call keeps the first deadline."""
        with self._lock:
            if self._deadline is None:
                self._deadline = self._clock() + self._close_timeout_s
            return self._deadline

    def remaining(self) -> float:
        """Seconds left on the shared deadline. Zero once it has passed.

        Before ``begin_close`` this is the whole budget. It does not grow, and it does
        not depend on how many handlers are running.
        """
        with self._lock:
            if self._deadline is None:
                return self._close_timeout_s
            return max(0.0, self._deadline - self._clock())

    def past_deadline(self) -> bool:
        with self._lock:
            return self._past_deadline_locked()

    def cancel_overdue(self) -> list[str | int | None]:
        """Cancel every handler still running at or after the deadline. Otherwise do nothing."""
        with self._lock:
            if not self._past_deadline_locked():
                return []
            return self._abandon_locked()

    def abandon(self) -> list[str | int | None]:
        """Cancel every in-flight handler.

        ``close`` calls this after the shared wait, including when the clock is a hair
        short of the deadline: the wait is over, so whatever is still running is late.
        """
        with self._lock:
            return self._abandon_locked()

    def _abandon_locked(self) -> list[str | int | None]:
        cancelled: list[str | int | None] = []
        for token in list(self._inflight):
            cancelled.append(self._inflight[token].request_id)
            self._settle_locked(token, CANCELLED)
        return cancelled

    def _past_deadline_locked(self) -> bool:
        return self._deadline is not None and self._clock() >= self._deadline

    def _refused(self, code: str, request_id: str | int | None) -> Claim:
        return Claim(False, code, request_id, threading.Event(), None)

    def _start_locked(self, request_id: str | int | None, key: tuple[object, str | int] | None) -> Claim:
        self._seq += 1
        token = self._seq
        event = threading.Event()
        self._inflight[token] = _Record(request_id, event, key)
        if key is not None:
            self._by_id[key] = token
        return Claim(True, None, request_id, event, token)

    def _settle_locked(self, token: int, state: str) -> None:
        record = self._inflight.pop(token, None)
        if record is None:
            return
        if record.key is not None:
            self._by_id.pop(record.key, None)
            self._settled[record.key] = state
        record.event.set()
