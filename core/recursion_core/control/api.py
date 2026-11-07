"""Control API: the one path every surface uses to start, read and stop runs (CSL-798, candidate).

PRODUCT.md 398cd812, 3.4 S6 and 11.3/11.8: Stop follows the #916 procedure on every surface, and a Stop
that has not been confirmed answers "pending", never "stopped". Surfaces (the CLI now, the GUI and the
Control MCP later) call ``ControlAPI.call(method, params)`` with plain dicts and print what it returns;
none has its own Stop path.

Methods and typed result codes::

    run.start  {"tenant", "engine", "prompt", "client_key"?} -> started | request_invalid | start_failed
    run.status {"run_id"}                                    -> ok | run_unknown | request_invalid
    run.list   {}                                            -> ok
    run.stop   {"run_id"}                                    -> stopped | pending | run_unknown | request_invalid

Stop: the first call asks the cleanup layer to stop the run's tree, then asks it to confirm. Only a
confirmed empty cgroup yields ``stopped``; a populated cgroup, a confirm timeout, an unknown answer or a
cleanup error yields ``pending`` with that reason. A repeat stop on a stopped run sends nothing; on a
pending run it only re-checks. Reads never change state and never call the cleanup layer.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from recursion_core.control.registry import EMPTY, POPULATED, TIMEOUT, KillTree, RunRegistry

STARTED, OK, STOPPED, PENDING = "started", "ok", "stopped", "pending"
RUN_UNKNOWN, REQUEST_INVALID, START_FAILED, METHOD_UNKNOWN = (
    "run_unknown", "request_invalid", "start_failed", "method_unknown")
CODES = frozenset({STARTED, OK, STOPPED, PENDING, RUN_UNKNOWN, REQUEST_INVALID, START_FAILED, METHOD_UNKNOWN})
SUCCESS_CODES = frozenset({STARTED, OK, STOPPED})
_PENDING_REASON = {POPULATED: "cgroup_populated", TIMEOUT: "confirm_timeout"}

# starter(params) -> {"run_id", "cgroup"}: the daemon's launch path (route service + cgroup leaf), injected.
Starter = Callable[[dict], Mapping[str, str]]


class ControlAPI:
    def __init__(self, registry: RunRegistry, kill_tree: KillTree, starter: Starter, clock: Callable[[], float],
                 *, confirm_timeout_s: float = 5.0) -> None:
        self._registry, self._kill, self._starter, self._clock = registry, kill_tree, starter, clock
        self._confirm_timeout_s = confirm_timeout_s
        self._stop_lock = threading.Lock()  # one stop decision at a time, so a kill is never sent twice
        self._start_lock = threading.Lock()  # a retried start with the same client_key launches once
        self._keys: dict[str, str] = {}

    def call(self, method: str, params: Mapping | None = None) -> dict:
        params = dict(params or {})
        if method == "run.list":
            return self.list_runs()
        if method == "run.start":
            return self.start(params)
        handler = {"run.status": self.status, "run.stop": self.stop}.get(method)
        if handler is None:
            return {"code": METHOD_UNKNOWN, "detail": str(method)}
        run_id = params.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return {"code": REQUEST_INVALID, "detail": "run_id"}
        return handler(run_id)

    # -- reads --------------------------------------------------------------------------------
    def status(self, run_id: str) -> dict:
        rec = self._registry.get(run_id)
        if rec is None:
            return {"code": RUN_UNKNOWN, "run_id": run_id}
        return {"code": OK, "run": rec}

    def list_runs(self) -> dict:
        return {"code": OK, "runs": self._registry.list()}

    # -- start --------------------------------------------------------------------------------
    def start(self, params: Mapping) -> dict:
        for name in ("tenant", "engine", "prompt"):
            if not isinstance(params.get(name), str) or not params[name]:
                return {"code": REQUEST_INVALID, "detail": name}
        with self._start_lock:
            return self._start(params)

    def _start(self, params: Mapping) -> dict:
        key = params.get("client_key")
        if isinstance(key, str) and key in self._keys:  # a retried start is not run twice (11.8)
            return {"code": STARTED, "run": self._registry.get(self._keys[key]), "duplicate": True}
        try:
            handle = self._starter(dict(params))
        except Exception as err:  # the launch path refused or failed; the API answers with a code
            return {"code": START_FAILED, "detail": type(err).__name__}
        rec = {"run_id": handle["run_id"], "tenant": params["tenant"], "engine": params["engine"],
               "state": "running", "cgroup": handle["cgroup"], "started_at": self._clock(), "stop": None}
        self._registry.put(rec)
        if isinstance(key, str):
            self._keys[key] = rec["run_id"]
        return {"code": STARTED, "run": rec, "duplicate": False}

    # -- stop ---------------------------------------------------------------------------------
    def stop(self, run_id: str) -> dict:
        with self._stop_lock:
            rec = self._registry.get(run_id)
            if rec is None:
                return {"code": RUN_UNKNOWN, "run_id": run_id}
            if rec["state"] == "stopped":
                return {"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": True}
            first = rec["state"] != "stop_pending"
            if first:
                rec["stop"] = {"requested_at": self._clock(), "checks": 0, "reason": None}
                try:
                    self._kill.request_stop(rec["cgroup"])
                except Exception as err:
                    return self._pending(rec, f"cleanup_error:{type(err).__name__}")
            try:
                answer = self._kill.confirm(rec["cgroup"], self._confirm_timeout_s)
            except Exception as err:
                return self._pending(rec, f"cleanup_error:{type(err).__name__}")
            if answer != EMPTY:
                return self._pending(rec, _PENDING_REASON.get(answer, "confirm_unknown"))
            rec["state"] = "stopped"
            rec["stop"].update(checks=rec["stop"]["checks"] + 1, reason=None)
            self._registry.put(rec)
            return {"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": not first}

    def _pending(self, rec: dict, reason: str) -> dict:
        rec["state"] = "stop_pending"
        rec["stop"].update(checks=rec["stop"]["checks"] + 1, reason=reason)
        self._registry.put(rec)
        return {"code": PENDING, "run_id": rec["run_id"], "state": "pending", "reason": reason}
