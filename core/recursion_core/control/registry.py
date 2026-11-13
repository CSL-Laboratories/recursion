"""Run registry and cleanup interfaces the control API depends on (CSL-798, candidate). See README.md.

The durable registry (B8/B9) and the cgroup v2 + pidfd cleanup layer (B6) are other build items; the
control API only sees these two interfaces, and tests fake them. A run record is a plain dict::

    {"run_id", "tenant", "engine",
     "state": "starting" | "start_failed" | "running" | "stop_pending" | "stopped",
     "cgroup": None | "<cgroup path or handle id>", "started_at",
     "stop": None | {"requested_at", "checks", "reason", "requested", "cgroup"},
     "start_key": None | {"client_key", "engine", "prompt_sha256"}}

``start_key`` is stored with the record so start dedupe survives a daemon restart: a durable registry
must persist it and answer ``find_start`` from what it persisted. A ``starting`` record is the launch
intent the control API writes before it calls the starter; ``cgroup`` is None until the launch is recorded.
"""

from __future__ import annotations

import copy
import threading
from typing import Protocol

# Answers from KillTree.confirm. Only EMPTY means the run's cgroup holds no process.
EMPTY, POPULATED, TIMEOUT, UNKNOWN = "empty", "populated", "timeout", "unknown"


class RunRegistry(Protocol):
    def get(self, run_id: str) -> dict | None: ...  # a copy; mutating it changes nothing

    def list(self) -> list[dict]: ...  # copies

    def put(self, record: dict) -> None: ...

    def find_start(self, tenant: str, client_key: str) -> dict | None: ...  # a copy of the run started with it


class KillTree(Protocol):
    """cgroup + pidfd cleanup. ``request_stop`` runs the #916 procedure (SIGINT to the run, drain, SIGKILL
    whatever is left after the grace period, via ``cgroup.kill`` and pidfds); ``confirm`` reports whether
    the run's cgroup is empty. Never ``PR_SET_PDEATHSIG`` or ``preexec_fn``."""

    def request_stop(self, cgroup: str) -> None: ...

    def confirm(self, cgroup: str, timeout_s: float) -> str: ...  # EMPTY | POPULATED | TIMEOUT | UNKNOWN


class InMemoryRunRegistry:
    """Thread-safe in-memory registry for tests and the candidate CLI; not durable.

    ``find_start`` is an indexed lookup on ``(tenant, client_key)``, so keyed starts do not slow down as
    runs accumulate (a durable registry should index it too)."""

    def __init__(self, records: list[dict] | None = None) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict] = {}
        self._starts: dict[tuple[str, str], str] = {}  # (tenant, client_key) -> run_id
        for r in records or []:
            self._store(copy.deepcopy(r))

    def _store(self, rec: dict) -> None:
        old = self._runs.get(rec["run_id"])
        if old is not None:
            okey = _start_index_key(old)
            if okey is not None and self._starts.get(okey) == rec["run_id"]:
                del self._starts[okey]
        self._runs[rec["run_id"]] = rec
        key = _start_index_key(rec)
        if key is not None:
            self._starts.setdefault(key, rec["run_id"])

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            rec = self._runs.get(run_id)
            return copy.deepcopy(rec) if rec is not None else None

    def list(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(self._runs[k]) for k in sorted(self._runs)]

    def put(self, record: dict) -> None:
        with self._lock:
            self._store(copy.deepcopy(record))

    def find_start(self, tenant: str, client_key: str) -> dict | None:
        with self._lock:
            run_id = self._starts.get((tenant, client_key))
            return copy.deepcopy(self._runs[run_id]) if run_id is not None else None


def _start_index_key(rec: dict) -> tuple[str, str] | None:
    key = rec.get("start_key")
    if not isinstance(key, dict) or not isinstance(key.get("client_key"), str):
        return None
    return (rec.get("tenant"), key["client_key"])
