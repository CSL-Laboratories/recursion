"""Run registry and cleanup interfaces the control API depends on (CSL-798, candidate). See README.md.

The durable registry (B8/B9) and the cgroup v2 + pidfd cleanup layer (B6) are other build items; the
control API only sees these two interfaces, and tests fake them. A run record is a plain dict::

    {"run_id", "tenant", "engine", "state": "running" | "stop_pending" | "stopped",
     "cgroup": "<cgroup path or handle id>", "started_at",
     "stop": None | {"requested_at", "checks", "reason"}}
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


class KillTree(Protocol):
    """cgroup + pidfd cleanup. ``request_stop`` runs the #916 procedure (SIGINT to the run, drain, SIGKILL
    whatever is left after the grace period, via ``cgroup.kill`` and pidfds); ``confirm`` reports whether
    the run's cgroup is empty. Never ``PR_SET_PDEATHSIG`` or ``preexec_fn``."""

    def request_stop(self, cgroup: str) -> None: ...

    def confirm(self, cgroup: str, timeout_s: float) -> str: ...  # EMPTY | POPULATED | TIMEOUT | UNKNOWN


class InMemoryRunRegistry:
    """Thread-safe in-memory registry for tests and the candidate CLI; not durable."""

    def __init__(self, records: list[dict] | None = None) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict] = {r["run_id"]: copy.deepcopy(r) for r in records or []}

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            rec = self._runs.get(run_id)
            return copy.deepcopy(rec) if rec is not None else None

    def list(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(self._runs[k]) for k in sorted(self._runs)]

    def put(self, record: dict) -> None:
        with self._lock:
            self._runs[record["run_id"]] = copy.deepcopy(record)
