"""Per-route-session owner inside one process (D-RP2-2).

Concurrent sessions are independent. Mutations on one
``(tenant, route, session)`` triple are serialized. A second mutation
while that triple is held is a typed Busy reject. Generation and
operation ids are process-local integers. This module has no
distributed fence.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from scripts.v3.route_adapter.errors import Busy, CustodyDenied, StaleGeneration
from scripts.v3.route_adapter.session_key import SessionKey

CONTENTION = "busy-reject"
DISTRIBUTED_FENCING = False

_PUBLIC_CREDENTIAL_FIELDS = ("tenant_id", "provider", "name")


@dataclass(frozen=True)
class Operation:
    """A locally issued operation. ``generation`` is not a cluster fence."""

    key: SessionKey
    generation: int
    op_id: str


@dataclass
class _Hold:
    operation: Operation
    events: list[dict[str, object]]


class RouteAdapterTypeError(TypeError):
    """A caller passed a non-string message. The payload is not kept."""


class SessionOwner:
    """The only mutation authority for the sessions it tracks.

    One lock in this process. Nothing here expires an owner so a
    replacement can start, and nothing here is shared with another
    process.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[SessionKey, _Hold] = {}
        self._generation: dict[SessionKey, int] = {}
        self._transcript: dict[SessionKey, list[dict[str, object]]] = {}
        self._discarded: list[str] = []
        self._op_seq = 0

    def begin(
        self,
        key: SessionKey,
        *,
        credential: dict[str, str] | None = None,
    ) -> Operation:
        public = _public_credential(credential)
        with self._lock:
            if key in self._active:
                raise Busy(key)
            generation = self._generation.get(key, 0) + 1
            self._generation[key] = generation
            self._op_seq += 1
            operation = Operation(
                key=key,
                generation=generation,
                op_id=f"local-{self._op_seq}",
            )
            hold = _Hold(operation=operation, events=[])
            self._active[key] = hold
            self._append(
                hold,
                {
                    "kind": "started",
                    "op_id": operation.op_id,
                    "generation": generation,
                    "credential": public,
                },
            )
            return operation

    def send(self, operation: Operation, message: str) -> None:
        if not isinstance(message, str):
            raise RouteAdapterTypeError()
        with self._lock:
            hold = self._require_active(operation)
            self._append(
                hold,
                {
                    "kind": "sent",
                    "op_id": operation.op_id,
                    "generation": operation.generation,
                    "message": message,
                },
            )

    def observe(self, operation: Operation) -> tuple[dict[str, object], ...]:
        with self._lock:
            events = [
                _copy_event(event)
                for event in self._transcript.get(operation.key, ())
                if event.get("op_id") == operation.op_id
                and event.get("generation") == operation.generation
            ]
            return tuple(events)

    def cancel(self, operation: Operation) -> bool:
        """Release the active owner immediately. A stale id does not.

        Cancellation does not wait behind another operation on this
        session: the session has only one active owner, and this call
        takes it directly.
        """

        with self._lock:
            hold = self._active.get(operation.key)
            if hold is None or hold.operation != operation:
                self._discarded.append(operation.op_id)
                return False
            self._append(
                hold,
                {
                    "kind": "cancelled",
                    "op_id": operation.op_id,
                    "generation": operation.generation,
                },
            )
            del self._active[operation.key]
            return True

    def complete(self, operation: Operation) -> bool:
        """Record a terminal completion, or discard a stale one.

        A discarded completion does not release a newer owner and does
        not append an event for that newer generation.
        """

        with self._lock:
            hold = self._active.get(operation.key)
            if hold is None or hold.operation != operation:
                self._discarded.append(operation.op_id)
                return False
            self._append(
                hold,
                {
                    "kind": "completed",
                    "op_id": operation.op_id,
                    "generation": operation.generation,
                },
            )
            del self._active[operation.key]
            return True

    def is_busy(self, key: SessionKey) -> bool:
        with self._lock:
            return key in self._active

    def generation_of(self, key: SessionKey) -> int:
        with self._lock:
            return self._generation.get(key, 0)

    def discarded(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._discarded)

    def snapshot(self) -> dict[str, object]:
        """Public adapter state. Credential material is not stored here."""

        with self._lock:
            keys = set(self._transcript) | set(self._active) | set(self._generation)
            sessions = []
            for key in sorted(keys, key=lambda item: (item.tenant, item.route, item.session)):
                sessions.append(
                    {
                        "tenant": key.tenant,
                        "route": key.route,
                        "session": key.session,
                        "generation": self._generation.get(key, 0),
                        "busy": key in self._active,
                        "events": [_copy_event(event) for event in self._transcript.get(key, ())],
                    }
                )
            return {
                "contention": CONTENTION,
                "distributed_fencing": DISTRIBUTED_FENCING,
                "discarded_stale": list(self._discarded),
                "sessions": sessions,
            }

    def _require_active(self, operation: Operation) -> _Hold:
        hold = self._active.get(operation.key)
        if hold is None or hold.operation != operation:
            self._discarded.append(operation.op_id)
            raise StaleGeneration(operation.op_id, operation.generation)
        return hold

    def _append(self, hold: _Hold, event: dict[str, object]) -> None:
        hold.events.append(event)
        transcript = self._transcript.setdefault(hold.operation.key, [])
        transcript.append(dict(event))


def _copy_event(event: dict[str, object]) -> dict[str, object]:
    copied = dict(event)
    credential = copied.get("credential")
    if isinstance(credential, dict):
        copied["credential"] = dict(credential)
    return copied


def _public_credential(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if set(value) != set(_PUBLIC_CREDENTIAL_FIELDS):
        raise CustodyDenied("credential-field-rejected")
    public: dict[str, str] = {}
    for field in _PUBLIC_CREDENTIAL_FIELDS:
        item = value[field]
        if not isinstance(item, str) or not item:
            raise CustodyDenied("credential-field-rejected")
        public[field] = item
    return public
