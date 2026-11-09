"""Control API: the one path every surface uses to start, read and stop runs (CSL-798, candidate).

PRODUCT.md 055cc7a5 (CSLR-D-011), 3.4 S6 and 11.3/11.8: Stop follows the #916 procedure on every surface, and a Stop
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
always finds it, and a same-key retry always sees it. If the write after launch fails
(``start_unconfirmed``), the API keeps the starter's cgroup in memory: Stop aims cleanup at it, and a
same-key retry records it once the registry answers. That launch stays stoppable while the registry
cannot even be read: Stop then aims at the record this process kept and answers ``"recorded": false``. A Stop that arrives before the launch is recorded
answers ``pending`` / ``cgroup_unknown`` and is kept as a stop intent (in the record and in memory); the
start acts on it once the launch is recorded, and its answer carries that stop result. A same-key retry
repeats an unconfirmed or failed answer and never upgrades it to ``started``: a starter that raised is
relaunched under the same run id (unless a Stop was asked for it), and a ``starting`` record with no
known cgroup (the handle had none, or a restart lost it) answers ``start_unconfirmed``. Reconciling such a
record against the host after a restart is the CSL-738 startup sweep.

Stop: the first call asks the cleanup layer to stop the run's tree, then asks it to confirm. Only a
confirmed empty cgroup yields ``stopped``; a populated cgroup, a confirm timeout, an unknown answer or a
cleanup error yields ``pending`` with that reason. A repeat stop on a stopped run sends nothing; on a
pending run it only re-checks, unless the earlier cleanup request was never sent (``stop.requested`` is
false: it raised, or it is a stop intent from before the launch was recorded), in which case it sends it
(``cgroup.kill`` and pidfd signals are safe to repeat).
Reads never change state and never call the cleanup layer.

``client_key`` dedupes a retried start per tenant and is bound to that start's engine and prompt (the
prompt is kept only as a SHA-256 digest of its UTF-8 bytes). A prompt that is not strict UTF-8 (an
unpaired surrogate, which JSON can carry) is ``request_invalid`` / ``prompt`` and nothing is launched,
with or without a key (bot review 4508015577). Another tenant may use the same key; the same tenant
reusing a key for a different request gets ``request_invalid`` / ``client_key_reused`` and nothing is
launched. The key and digest live in the run record (``start_key``) and are looked up through the
registry, so a ``ControlAPI`` rebuilt over the same durable registry after a restart still answers the
retried start with the original run (11.3, B-17).

Stop decisions and launch records are serialised per run, so a slow or hung cleanup on one run never
queues another run's Stop (11.3, D2-55 / B-19), and a launch never overwrites a Stop that landed during it.
Starts are serialised only per ``(tenant, client_key)``: a slow or hung starter delays only a retry of the
same start, never another start. The lock is dropped once that start, and any same-key start waiting on
it, have returned, so distinct keys do not keep locks for the life of the process (bot review 4517611266).
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
        # One stop or launch-record decision per run, so a kill is never sent twice and a Stop that lands
        # during a launch is never overwritten by it.
        self._run_locks: dict[str, threading.Lock] = {}
        # A retried start with the same (tenant, client_key) launches once; other starts never wait on it.
        # Value is [lock, in_flight]. The entry is removed when the count hits zero (bot review 4517611266).
        self._start_locks: dict[tuple[str, str], list] = {}
        # In-memory only (a restart loses them; reconciling then is the CSL-738 startup sweep):
        self._launched: dict[str, dict] = {}  # run_id -> record (with cgroup) of a launch whose write failed
        self._stop_wanted: set[str] = set()  # Stops that arrived before the launch was recorded
        self._failed: set[str] = set()  # starts that raised but whose start_failed write failed

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
        try:
            params["prompt"].encode("utf-8")  # JSON can carry an unpaired surrogate; reject it before launch
        except UnicodeEncodeError:
            return {"code": REQUEST_INVALID, "detail": "prompt"}
        key = params.get("client_key")
        if not isinstance(key, str):
            return self._start(params)  # no key: a fresh run id, nothing to dedupe
        slot_key = (params["tenant"], key)
        with self._locks_lock:
            slot = self._start_locks.get(slot_key)
            if slot is None:
                slot = [threading.Lock(), 0]
                self._start_locks[slot_key] = slot
            slot[1] += 1
            lock = slot[0]
        try:
            with lock:
                return self._start(params)
        finally:
            with self._locks_lock:
                slot[1] -= 1
                if slot[1] == 0 and self._start_locks.get(slot_key) is slot:
                    del self._start_locks[slot_key]

    def _start(self, params: Mapping) -> dict:
        key = params.get("client_key")
        start_key = None
        if isinstance(key, str):
            start_key = {"client_key": key, "engine": params["engine"],
                         "prompt_sha256": hashlib.sha256(params["prompt"].encode("utf-8")).hexdigest()}
        prior = self._reg("find_start", params["tenant"], key) if start_key is not None else None
        if prior is not None and prior.get("start_key") != start_key:
            return {"code": REQUEST_INVALID, "detail": "client_key_reused"}
        run_id = prior["run_id"] if prior is not None else self._new_run_id()
        rec = {"run_id": run_id, "tenant": params["tenant"], "engine": params["engine"], "state": "starting",
               "cgroup": None, "started_at": self._clock(), "stop": None, "start_key": start_key}
        with self._run_lock(run_id):
            if prior is not None:  # a retried start is not run twice, across restarts too (11.3, 11.8)
                answer = self._retry(self._current(prior))
                if answer is not None:
                    return answer
            self._reg("put", rec)  # launch intent: nothing runs that the registry cannot list, stop or dedupe
            self._failed.discard(run_id)
        try:
            handle = self._starter({**params, "run_id": run_id})
        except Exception as err:  # the launch path refused or failed; the API answers with a code
            with self._run_lock(run_id):
                # Re-read first: a Stop may have written its intent while the starter ran. If the read fails,
                # write nothing rather than overwrite that intent with the launch-intent copy (t1430u X1c).
                try:
                    current = self._reg("get", run_id)
                except _RegistryError:
                    current = None
                recorded = False
                if current is not None:
                    current["state"] = "start_failed"
                    recorded = self._try_put(current)
                if not recorded:  # the record still says starting; a same-key retry must still relaunch
                    self._failed.add(run_id)
            return {"code": START_FAILED, "run_id": run_id, "detail": type(err).__name__, "recorded": recorded}
        cgroup = handle.get("cgroup") if isinstance(handle, Mapping) else None
        if not isinstance(cgroup, str) or not cgroup:
            return {"code": START_UNCONFIRMED, "run": rec, "detail": "starter_handle_invalid"}
        with self._run_lock(run_id):
            # Known to this process even if the write below fails, so Stop and a retry can still aim at it
            # (red-team r3 finding 1).
            current = self._current(rec)  # re-read: keeps a stop intent written while the starter ran
            current.update(state="running", cgroup=cgroup)
            self._launched[run_id] = dict(current)
            if current.get("stop") is not None or run_id in self._stop_wanted:
                # A Stop arrived during the launch and answered pending; act on it now (red-team r3 finding 2).
                stop = self._stop(current)
                code = START_UNCONFIRMED if stop.get("recorded") is False else STARTED
                return {"code": code, "run": current, "duplicate": False, "stop": stop}
            if not self._try_put(current):
                return {"code": START_UNCONFIRMED, "run_id": run_id, "detail": "registry_unavailable"}
            self._launched.pop(run_id, None)
            return {"code": STARTED, "run": current, "duplicate": False}

    def _retry(self, prior: dict) -> dict | None:
        """Answer a same-key retry from the existing record, or None to relaunch it. Caller holds the run lock.

        A retry repeats an unconfirmed or failed answer; it never upgrades it to ``started`` (red-team r3
        finding 3)."""
        run_id, state = prior["run_id"], prior.get("state")
        if state == "start_failed" or (state == "starting" and run_id in self._failed):
            if prior.get("stop") is not None or run_id in self._stop_wanted:
                return {"code": START_FAILED, "run_id": run_id, "detail": "stop_requested", "duplicate": True}
            return None  # the starter refused: relaunch under the same run id
        if state != "starting" or prior.get("cgroup"):
            return {"code": STARTED, "run": prior, "duplicate": True}
        launched = self._launched.get(run_id)
        if launched is None:  # starter handle had no cgroup, or a restart lost it: nothing confirms the launch
            return {"code": START_UNCONFIRMED, "run": prior, "duplicate": True, "detail": "launch_unconfirmed"}
        # Launched here, but the write after launch failed. The kept record carries any Stop decided since
        # (its write-back failed too): carry it out or record it, never record a bare "running" over it.
        prior = dict(launched)
        if prior.get("stop") is not None:
            stop = self._stop(prior)  # a kept "stopped" is written back; anything else is continued
            code = START_UNCONFIRMED if stop.get("recorded") is False else STARTED
            return {"code": code, "run": prior, "duplicate": True, "stop": stop}
        if not self._try_put(prior):
            return {"code": START_UNCONFIRMED, "run": prior, "duplicate": True, "detail": "registry_unavailable"}
        self._launched.pop(run_id, None)
        return {"code": STARTED, "run": prior, "duplicate": True}

    def _current(self, rec: dict) -> dict:
        """The registry's copy of ``rec``, or ``rec`` itself if the registry cannot answer."""
        try:
            current = self._reg("get", rec["run_id"])
        except _RegistryError:
            current = None
        return current if current is not None else dict(rec)

    def _try_put(self, rec: dict) -> bool:
        try:
            self._reg("put", rec)
        except _RegistryError:
            return False
        return True

    def _run_lock(self, run_id: str) -> threading.Lock:
        with self._locks_lock:
            return self._run_locks.setdefault(run_id, threading.Lock())

    # -- stop ---------------------------------------------------------------------------------
    def stop(self, run_id: str) -> dict:
        if self._stop_target(run_id) is None:  # no lock is kept for ids that name no run
            return {"code": RUN_UNKNOWN, "run_id": run_id}
        with self._run_lock(run_id):
            rec = self._stop_target(run_id)
            if rec is None:
                return {"code": RUN_UNKNOWN, "run_id": run_id}
            return self._stop(rec)

    def _stop_target(self, run_id: str) -> dict | None:
        """The registry's record, or, when the registry cannot be read, the record this process kept for a
        launch whose write failed: that run is live and its cgroup is known, so Stop still reaches cleanup
        (S6). Any other read failure answers ``registry_unavailable``."""
        try:
            return self._reg("get", run_id)
        except _RegistryError:
            launched = self._launched.get(run_id)
            if launched is None:
                raise
            return dict(launched)

    def _stop(self, rec: dict) -> dict:
        """One stop decision on ``rec``. Caller holds the run lock."""
        run_id = rec["run_id"]
        if rec["state"] == "stopped":
            res = {"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": True}
            # A stopped launch this process still holds was never written: try again now.
            return self._recorded(res, rec) if run_id in self._launched else res
        if not rec.get("cgroup"):
            launched = self._launched.get(run_id)
            if launched is None:  # launch not confirmed yet (starting / start_failed): keep the Stop as intent
                return self._stop_intent(rec)
            # Launched, write after launch failed: aim at it now, from the kept record, so a Stop whose own
            # write-back failed earlier is continued rather than started again.
            rec.clear()
            rec.update(launched)
            if rec["state"] == "stopped":
                return self._recorded({"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": True}, rec)
        self._stop_wanted.discard(run_id)
        first = rec["state"] != "stop_pending"
        if rec.get("stop") is None:
            rec["stop"] = {"requested_at": self._clock(), "checks": 0, "reason": None, "requested": False}
        if not rec["stop"].get("requested", True):
            # First stop, a stop intent left during launch, or an earlier request raised before it was known
            # to be sent: (re)send it.
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

    def _stop_intent(self, rec: dict) -> dict:
        # Red-team r3 finding 2: the Stop is kept (in the record and in memory), and the start that is
        # still launching acts on it once the launch is recorded. The record keeps its launch state.
        self._stop_wanted.add(rec["run_id"])
        if rec.get("stop") is None:
            rec["stop"] = {"requested_at": self._clock(), "checks": 0, "reason": None, "requested": False}
        rec["stop"].update(checks=rec["stop"]["checks"] + 1, reason="cgroup_unknown")
        res = {"code": PENDING, "run_id": rec["run_id"], "state": "pending", "reason": "cgroup_unknown"}
        if not self._try_put(rec):
            res["recorded"] = False
        return res

    def _pending(self, rec: dict, reason: str) -> dict:
        rec["state"] = "stop_pending"
        rec["stop"].update(checks=rec["stop"]["checks"] + 1, reason=reason)
        return self._recorded({"code": PENDING, "run_id": rec["run_id"], "state": "pending", "reason": reason}, rec)

    def _recorded(self, res: dict, rec: dict) -> dict:
        # The cleanup layer's answer is the truth; a failed write-back is reported, not raised. The next
        # stop then sees the old state and asks again, which is safe (cgroup.kill and pidfds repeat).
        if not self._try_put(rec):
            res["recorded"] = False
            if rec["run_id"] in self._launched:  # keep the decision with the launch until a write lands
                self._launched[rec["run_id"]] = dict(rec, stop=dict(rec["stop"]) if rec.get("stop") else None)
        else:
            self._launched.pop(rec["run_id"], None)  # the cgroup is in the registry now
        return res
