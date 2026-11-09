"""Control API: the one path every surface uses to start, read and stop runs (CSL-798, candidate).

PRODUCT.md 398cd812, 3.4 S6 and 11.3/11.8: Stop follows the #916 procedure on every surface, and a Stop
that has not been confirmed answers "pending", never "stopped". Surfaces (the CLI now, the GUI and the
Control MCP later) call ``ControlAPI.call(method, params)`` with plain dicts and print what it returns;
none has its own Stop path.

Methods and typed result codes::

    run.start  {"tenant", "engine", "prompt", "client_key"?} -> started | request_invalid | start_failed
                                                                | start_unconfirmed | registry_unavailable
    run.status {"run_id"}                                    -> ok | run_unknown | request_invalid
                                                                | registry_unavailable
    run.list   {}                                            -> ok | registry_unavailable
    run.stop   {"run_id"}                                    -> stopped | pending | run_unknown | request_invalid
                                                                | registry_unavailable

A registry error never escapes ``call``: a read or a start that cannot reach the registry answers
``registry_unavailable`` and launches nothing. Once the cleanup layer has answered, Stop returns that
answer even if writing it back fails, with ``"recorded": false``.

Start writes a launch intent first: the API picks the run id, records the run as ``starting`` (with its
``start_key``), and only then calls the starter with that id. So a launched run is always listed, Stop
always finds it, and a same-key retry always sees it, even if the write after launch fails or the starter
hands back no cgroup (``start_unconfirmed``; the record stays ``starting``). A run with no recorded cgroup
answers Stop with ``pending`` / ``cgroup_unknown`` and the cleanup layer is not called. A starter that
raises leaves the record ``start_failed``; a same-key retry relaunches under the same run id.

Stop: the first call asks the cleanup layer to stop the run's tree, then asks it to confirm. Only a
confirmed empty cgroup yields ``stopped``; a populated cgroup, a confirm timeout, an unknown answer or a
cleanup error yields ``pending`` with that reason. A repeat stop on a stopped run sends nothing; on a
pending run it only re-checks, unless the earlier cleanup request itself raised (``stop.requested`` is
false), in which case it sends the request again (``cgroup.kill`` and pidfd signals are safe to repeat).
Reads never change state and never call the cleanup layer.

``client_key`` dedupes a retried start per tenant and is bound to that start's engine and prompt (the
prompt is kept only as a SHA-256 digest). Another tenant may use the same key; the same tenant reusing a
key for a different request gets ``request_invalid`` / ``client_key_reused`` and nothing is launched. The
key and digest live in the run record (``start_key``) and are looked up through the registry, so a
``ControlAPI`` rebuilt over the same durable registry after a restart still answers the retried start
with the original run (11.3, B-17).

Stop decisions are serialised per run, so a slow or hung cleanup on one run never queues another run's
Stop (11.3, D2-55 / B-19).
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable, Mapping

from recursion_core.control.registry import EMPTY, POPULATED, TIMEOUT, KillTree, RunRegistry

STARTED, OK, STOPPED, PENDING = "started", "ok", "stopped", "pending"
RUN_UNKNOWN, REQUEST_INVALID, START_FAILED, METHOD_UNKNOWN = (
    "run_unknown", "request_invalid", "start_failed", "method_unknown")
START_UNCONFIRMED, REGISTRY_UNAVAILABLE = "start_unconfirmed", "registry_unavailable"
CODES = frozenset({STARTED, OK, STOPPED, PENDING, RUN_UNKNOWN, REQUEST_INVALID, START_FAILED, METHOD_UNKNOWN,
                   START_UNCONFIRMED, REGISTRY_UNAVAILABLE})
SUCCESS_CODES = frozenset({STARTED, OK, STOPPED})
_PENDING_REASON = {POPULATED: "cgroup_populated", TIMEOUT: "confirm_timeout"}

# starter(params) -> {"cgroup"}: the daemon's launch path (route service + cgroup leaf), injected. params
# carries the API-chosen "run_id", which the starter must launch under.
Starter = Callable[[dict], Mapping[str, str]]


class _RegistryError(Exception):
    pass


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


class ControlAPI:
    def __init__(self, registry: RunRegistry, kill_tree: KillTree, starter: Starter, clock: Callable[[], float],
                 *, confirm_timeout_s: float = 5.0, new_run_id: Callable[[], str] = _new_run_id) -> None:
        self._registry, self._kill, self._starter, self._clock = registry, kill_tree, starter, clock
        self._new_run_id = new_run_id
        self._confirm_timeout_s = confirm_timeout_s
        self._locks_lock = threading.Lock()
        self._stop_locks: dict[str, threading.Lock] = {}  # one stop decision per run, so a kill is never sent twice
        self._start_lock = threading.Lock()  # a retried start with the same client_key launches once

    def call(self, method: str, params: Mapping | None = None) -> dict:
        try:
            return self._call(method, dict(params or {}))
        except _RegistryError as err:  # every method answers with a typed code (red-team r2 finding 1)
            return {"code": REGISTRY_UNAVAILABLE, "detail": str(err)}

    def _reg(self, op: str, *args):
        try:
            return getattr(self._registry, op)(*args)
        except Exception as err:
            raise _RegistryError(f"{op}:{type(err).__name__}") from err

    def _call(self, method: str, params: dict) -> dict:
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
        rec = self._reg("get", run_id)
        if rec is None:
            return {"code": RUN_UNKNOWN, "run_id": run_id}
        return {"code": OK, "run": rec}

    def list_runs(self) -> dict:
        return {"code": OK, "runs": self._reg("list")}

    # -- start --------------------------------------------------------------------------------
    def start(self, params: Mapping) -> dict:
        for name in ("tenant", "engine", "prompt"):
            if not isinstance(params.get(name), str) or not params[name]:
                return {"code": REQUEST_INVALID, "detail": name}
        with self._start_lock:
            return self._start(params)

    def _start(self, params: Mapping) -> dict:
        key = params.get("client_key")
        start_key = None
        if isinstance(key, str):
            start_key = {"client_key": key, "engine": params["engine"],
                         "prompt_sha256": hashlib.sha256(params["prompt"].encode()).hexdigest()}
        prior = self._reg("find_start", params["tenant"], key) if start_key is not None else None
        if prior is not None:  # a retried start is not run twice, across restarts too (11.3, 11.8)
            if prior.get("start_key") != start_key:
                return {"code": REQUEST_INVALID, "detail": "client_key_reused"}
            if prior.get("state") != "start_failed":
                return {"code": STARTED, "run": prior, "duplicate": True}
        run_id = prior["run_id"] if prior is not None else self._new_run_id()
        rec = {"run_id": run_id, "tenant": params["tenant"], "engine": params["engine"], "state": "starting",
               "cgroup": None, "started_at": self._clock(), "stop": None, "start_key": start_key}
        self._reg("put", rec)  # launch intent: nothing runs that the registry cannot list, stop or dedupe
        try:
            handle = self._starter({**params, "run_id": run_id})
        except Exception as err:  # the launch path refused or failed; the API answers with a code
            rec["state"] = "start_failed"
            return {"code": START_FAILED, "run_id": run_id, "detail": type(err).__name__,
                    "recorded": self._try_put(rec)}
        cgroup = handle.get("cgroup") if isinstance(handle, Mapping) else None
        if not isinstance(cgroup, str) or not cgroup:
            return {"code": START_UNCONFIRMED, "run": rec, "detail": "starter_handle_invalid"}
        rec.update(state="running", cgroup=cgroup)
        if not self._try_put(rec):
            return {"code": START_UNCONFIRMED, "run_id": run_id, "detail": "registry_unavailable"}
        return {"code": STARTED, "run": rec, "duplicate": False}

    def _try_put(self, rec: dict) -> bool:
        try:
            self._reg("put", rec)
        except _RegistryError:
            return False
        return True

    # -- stop ---------------------------------------------------------------------------------
    def stop(self, run_id: str) -> dict:
        if self._reg("get", run_id) is None:  # no lock is kept for ids that name no run
            return {"code": RUN_UNKNOWN, "run_id": run_id}
        with self._locks_lock:
            lock = self._stop_locks.setdefault(run_id, threading.Lock())
        with lock:
            rec = self._reg("get", run_id)
            if rec is None:
                return {"code": RUN_UNKNOWN, "run_id": run_id}
            if rec["state"] == "stopped":
                return {"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": True}
            if not rec.get("cgroup"):  # launch not recorded (starting / start_failed): nothing to aim cleanup at
                return {"code": PENDING, "run_id": run_id, "state": "pending", "reason": "cgroup_unknown"}
            first = rec["state"] != "stop_pending"
            if first:
                rec["stop"] = {"requested_at": self._clock(), "checks": 0, "reason": None, "requested": False}
            if not rec["stop"].get("requested", True):
                # First stop, or an earlier request raised before it was known to be sent: (re)send it.
                try:
                    self._kill.request_stop(rec["cgroup"])
                except Exception as err:
                    return self._pending(rec, f"cleanup_error:{type(err).__name__}")
                rec["stop"]["requested"] = True
            try:
                answer = self._kill.confirm(rec["cgroup"], self._confirm_timeout_s)
            except Exception as err:
                return self._pending(rec, f"cleanup_error:{type(err).__name__}")
            if answer != EMPTY:
                return self._pending(rec, _PENDING_REASON.get(answer, "confirm_unknown"))
            rec["state"] = "stopped"
            rec["stop"].update(checks=rec["stop"]["checks"] + 1, reason=None)
            res = {"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": not first}
            return self._recorded(res, rec)

    def _pending(self, rec: dict, reason: str) -> dict:
        rec["state"] = "stop_pending"
        rec["stop"].update(checks=rec["stop"]["checks"] + 1, reason=reason)
        return self._recorded({"code": PENDING, "run_id": rec["run_id"], "state": "pending", "reason": reason}, rec)

    def _recorded(self, res: dict, rec: dict) -> dict:
        # The cleanup layer's answer is the truth; a failed write-back is reported, not raised. The next
        # stop then sees the old state and asks again, which is safe (cgroup.kill and pidfds repeat).
        if not self._try_put(rec):
            res["recorded"] = False
        return res
