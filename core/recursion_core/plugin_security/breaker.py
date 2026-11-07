"""Engine breaker keyed by connection (CSL-608, grill Q8) and the faulted-run register.

The breaker trips on the connection a route is billed through (an API key or a
subscription login), never on an engine or model name. Only a typed limit event
trips it; the core's own timeouts and deadlines never do (S3, S4). A tripped
connection resets only through a successful leased probe.

A run that ended in a fault is recorded here too, so an effect proposed by that run,
or by any run on a tripped connection, is refused at the approval gate (4.2, B15).
This is the domain-neutral half; any consumer's own breakers stay with the consumer.
"""

from __future__ import annotations

import re
import threading

from .errors import Refused

__all__ = ["ConnectionBreaker", "TYPED_LIMITS"]

TYPED_LIMITS = frozenset({"plan_limit", "rate_limit", "usage_window_exhausted"})
_NOT_ENGINE_FAULTS = frozenset({"core_timeout", "core_deadline", "caller_cancelled"})
_ID = re.compile(r"[A-Za-z0-9_.:@/-]{1,128}")


def _check_id(value: object, code: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise Refused(code)
    return value


class ConnectionBreaker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tripped: dict[str, str] = {}
        self._faulted_runs: set[str] = set()
        self._run_connection: dict[str, str] = {}

    def bind_run(self, run_id: str, connection: str) -> None:
        """Record the connection a run is billed through; a run cannot move connections."""
        run_id = _check_id(run_id, "invalid_run_id")
        connection = _check_id(connection, "invalid_connection")
        with self._lock:
            if self._run_connection.setdefault(run_id, connection) != connection:
                raise Refused("run_connection_mismatch")

    def report(self, connection: str, event_type: str) -> bool:
        """Report a typed event. Returns True when it tripped the connection."""
        connection = _check_id(connection, "invalid_connection")
        if event_type in _NOT_ENGINE_FAULTS:
            return False
        if event_type not in TYPED_LIMITS:
            raise Refused("untyped_breaker_event")
        with self._lock:
            self._tripped[connection] = event_type
        return True

    def fault_run(self, run_id: str) -> None:
        run_id = _check_id(run_id, "invalid_run_id")
        with self._lock:
            self._faulted_runs.add(run_id)

    def reset_after_probe(self, connection: str, probe_ok: bool) -> bool:
        with self._lock:
            if probe_ok is True and connection in self._tripped:
                del self._tripped[connection]
                return True
            return False

    def tripped(self, connection: str) -> bool:
        return connection in self._tripped

    def check(self, run_id: str, connection: str) -> None:
        """Refuse an effect from a faulted run, a tripped connection or a mismatched run."""
        with self._lock:
            bound = self._run_connection.get(run_id)
            if bound is None:
                raise Refused("run_unknown")
            if bound != connection:
                raise Refused("run_connection_mismatch")
            if run_id in self._faulted_runs:
                raise Refused("run_faulted")
            if connection in self._tripped:
                raise Refused("connection_tripped")
