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
The registry write is a compare-and-swap on the record's generation, so a stop is also not overwritten by
a writer that does not hold that lock. ``stopped`` carries an attestation that the member set was empty.
Starts are serialised only per ``(tenant, client_key)``: a slow or hung starter delays only a retry of the
same start, never another start. The lock is dropped once that start, and any same-key start waiting on
it, have returned, so distinct keys do not keep locks for the life of the process (bot review 4517611266).
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from recursion_core.control.registry import (
    EMPTY, POPULATED, TIMEOUT, KillTree, LivenessUnknown, OwnerLockTable, RunRegistry, StaleGeneration,
    StopUnattested,
)

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
        # Exclusion is the registry's owner lock table when it has one, so two ControlAPIs over the same
        # registry take the same locks. The owner is a token minted for the in-flight call, not a thread
        # id (thread ids are reused). Start keys sort before run keys, and each acquire sorts its keys,
        # so a start that also takes the run lock cannot deadlock with a stop that takes only the run lock.
        self._locks: OwnerLockTable = getattr(registry, "locks", None) or OwnerLockTable()
        self._local = threading.local()
        # A retried start with the same (tenant, client_key) launches once; other starts never wait on it.
        # Value is [owner token, in_flight]. The entry is removed when the count hits zero (bot review
        # 4517611266). The token is not the lock; exclusion is ``self._locks``.
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
        self._enter_owner()
        try:
            if not isinstance(key, str):
                return self._start(params)  # no key: a fresh run id, nothing to dedupe
            slot_key = (params["tenant"], key)
            with self._locks_lock:
                slot = self._start_locks.get(slot_key)
                if slot is None:
                    slot = [self._local.owner, 0]
                    self._start_locks[slot_key] = slot
                slot[1] += 1
            lock_key = _start_lock_key(slot_key)
            self._locks.acquire(self._local.owner, [lock_key])
            try:
                return self._start(params)
            finally:
                self._locks.release(self._local.owner, [lock_key])
                with self._locks_lock:
                    slot[1] -= 1
                    if slot[1] == 0 and self._start_locks.get(slot_key) is slot:
                        del self._start_locks[slot_key]
        finally:
            self._exit_owner()

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
                # The run already has a generation. A relaunch has to CAS that generation; a blind put
                # would be a stale write and is refused.
                current = self._current(prior)
                if current is not None and "generation" in current:
                    current.update(state="starting", cgroup=None, engine=params["engine"], start_key=start_key,
                                   started_at=self._clock())
                    rec = current
            # Launch intent, then keep the committed snapshot (its generation) on this record. A later
            # outage must not leave Stop holding a copy that has no generation.
            if self._commit(rec) != "ok":
                raise _RegistryError("put:StaleGeneration")
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
            if not self._put_running(current, cgroup):
                return {"code": START_UNCONFIRMED, "run_id": run_id, "detail": "registry_unavailable"}
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
        # Launched here, but the write after launch failed. The registry row may have moved since (a
        # metadata write can land while running and stopped writes fail). Stop that row: adoption overlays
        # the retained cgroup and the proof is built for its generation. The cached payload is not written
        # over the fresh row, and a bare "running" is never recorded over a Stop.
        if prior.get("stop") is not None or launched.get("stop") is not None or run_id in self._stop_wanted:
            stop = self._stop(prior)
            code = START_UNCONFIRMED if stop.get("recorded") is False else STARTED
            return {"code": code, "run": prior, "duplicate": True, "stop": stop}
        kept = dict(launched)
        if not self._put_running(kept, kept.get("cgroup")):
            return {"code": START_UNCONFIRMED, "run": prior, "duplicate": True, "detail": "registry_unavailable"}
        return {"code": STARTED, "run": kept, "duplicate": True}

    def _current(self, rec: dict) -> dict:
        """The registry's copy of ``rec``, or ``rec`` itself if the registry cannot answer."""
        try:
            current = self._reg("get", rec["run_id"])
        except _RegistryError:
            current = None
        return current if current is not None else dict(rec)

    def _try_put(self, rec: dict) -> bool:
        """Write ``rec`` if its generation is still current. A stale generation is not a successful write."""
        try:
            return self._commit(rec) == "ok"
        except _RegistryError:
            return False

    def _commit(self, rec: dict) -> str:
        """``ok``, ``stale``, ``unattested``, ``liveness``, or raise ``_RegistryError``.

        A stale write leaves the registry untouched. ``unattested`` is a proof the registry refused,
        which is not a disk failure and not a stopped run. ``liveness`` is a probe that failed inside
        the put: that is not proof the member is gone, and not a disk failure. On success ``rec``
        becomes the snapshot this put stored, not a later read. A registry that does not return that
        snapshot is refreshed in full, so a newer generation is never paired with an older payload.
        """
        try:
            stored = self._registry.put(dict(rec))
        except StaleGeneration:
            return "stale"
        except StopUnattested:
            return "unattested"
        except LivenessUnknown:
            return "liveness"
        except Exception as err:
            raise _RegistryError(f"put:{type(err).__name__}") from err
        if isinstance(stored, dict):
            rec.clear()
            rec.update(stored)
            return "ok"
        self._refresh(rec)
        return "ok"

    def _refresh(self, rec: dict) -> None:
        try:
            fresh = self._registry.get(rec["run_id"])
        except Exception:
            return
        if fresh is None:
            return
        rec.clear()
        rec.update(fresh)

    def _reread(self, rec: dict) -> dict | None:
        try:
            return self._reg("get", rec["run_id"])
        except _RegistryError:
            return None

    def _put_running(self, rec: dict, cgroup: str | None) -> bool:
        """CAS ``rec`` as running. A stale generation is retried once from a fresh read.

        If that read already carries a Stop, this does not write ``running`` over it: it continues the
        Stop and reports success only when that continuation was recorded. Returns False when the
        registry did not accept a running record and no Stop was carried out.
        """
        try:
            outcome = self._commit(rec)
        except _RegistryError:
            return False
        if outcome == "ok":
            self._forget_launch_if_persisted(rec)
            return True
        if outcome != "stale":
            return False
        fresh = self._reread(rec)
        if fresh is None:
            return False
        if fresh.get("state") == "stopped" or fresh.get("stop") is not None or rec["run_id"] in self._stop_wanted:
            fresh["cgroup"] = fresh.get("cgroup") or cgroup
            self._launched[rec["run_id"]] = dict(fresh)
            if fresh.get("state") == "stopped":
                rec.clear()
                rec.update(fresh)
                return True
            stop = self._stop(fresh)
            rec.clear()
            rec.update(fresh)
            # A local "stopped" is not persistence. The stop's recorded flag is the only success signal.
            return stop.get("recorded") is not False
        fresh.update(state="running", cgroup=cgroup or fresh.get("cgroup"))
        self._launched[rec["run_id"]] = dict(fresh)
        try:
            outcome = self._commit(fresh)
        except _RegistryError:
            outcome = "down"
        rec.clear()
        rec.update(fresh)
        if outcome != "ok":
            return False
        self._forget_launch_if_persisted(fresh)
        return True

    def _process_alive(self, pid: int | None) -> bool:
        checker = getattr(self._registry, "process_alive", None)
        if checker is None or pid is None:
            return False
        return bool(checker(pid))

    def _block_stop(self, rec: dict) -> str | None:
        """A stop is not attested while a member's process is still alive.

        An error from the process table is not proof the member is gone, so it stays a pending reason
        instead of escaping ``call``.
        """
        try:
            for member in rec.get("members") or []:
                if self._process_alive(member.get("pid")):
                    return "member_alive"
        except Exception as err:
            return f"cleanup_error:{type(err).__name__}"
        return None

    def _attestation(self, rec: dict) -> dict | None:
        """Proof for the cgroup identity on ``rec``. None when that record has no generation to bind."""
        if "generation" not in rec:
            return None
        members = rec.get("members") or []
        return {"proof": "empty", "cgroup": rec.get("cgroup"), "cgroup_marker": rec.get("cgroup_marker"),
                "member_ids": sorted(member["member_id"] for member in members),
                "generation": rec["generation"]}

    def _enter_owner(self) -> None:
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self._local.owner = uuid.uuid4().hex
        self._local.depth = depth + 1

    def _exit_owner(self) -> None:
        self._local.depth -= 1
        if self._local.depth == 0:
            self._local.owner = None

    @contextmanager
    def _run_lock(self, run_id: str) -> Iterator[None]:
        key = _run_lock_key(run_id)
        owner = self._local.owner
        self._locks.acquire(owner, [key])
        try:
            yield
        finally:
            self._locks.release(owner, [key])

    # -- stop ---------------------------------------------------------------------------------
    def stop(self, run_id: str) -> dict:
        self._enter_owner()
        try:
            if self._stop_target(run_id) is None:  # no lock is kept for ids that name no run
                return {"code": RUN_UNKNOWN, "run_id": run_id}
            with self._run_lock(run_id):
                rec = self._stop_target(run_id)
                if rec is None:
                    return {"code": RUN_UNKNOWN, "run_id": run_id}
                return self._stop(rec)
        finally:
            self._exit_owner()

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

    def _cgroup_identity(self, rec: dict) -> tuple:
        return (rec.get("cgroup"), rec.get("cgroup_marker"))

    def _carry_stop(self, stop: dict | None, source: dict, dest: dict) -> dict:
        """Copy ``stop`` onto ``dest``. ``requested`` is true only for the cgroup identity it was sent to."""
        if isinstance(stop, dict):
            carried = dict(stop)
        elif isinstance(dest.get("stop"), dict):
            carried = dict(dest["stop"])
        else:
            carried = {"requested_at": self._clock(), "checks": 0, "reason": None, "requested": False}
        if self._cgroup_identity(source) != self._cgroup_identity(dest):
            carried["requested"] = False
        return carried

    def _adopt_unrecorded_launch(self, rec: dict) -> dict | None:
        """Point a cgroup-less registry row at the launch this process kept.

        The row stays the base. The handle contributes its cgroup and any unwritten cleanup, and the
        row's generation is copied back onto the handle. A handle that already says stopped is not
        written as-is: the caller confirms that cgroup and builds a new proof. Returns a finished
        answer, or None when ``rec`` is ready for cleanup.
        """
        if rec.get("cgroup"):
            return None
        run_id = rec["run_id"]
        launched = self._launched.get(run_id)
        if launched is None:  # launch not confirmed yet: keep the Stop as intent
            return self._stop_intent(rec)
        # The registry row is newer than the handle (its engine, generation, and anything else written
        # after the launch). Keep that row and take only the cgroup the handle saw, plus a cleanup
        # decision that never landed. A handle that already says stopped is confirmed again so the
        # proof is rebuilt for this generation instead of replayed.
        if launched.get("cgroup"):
            rec["cgroup"] = launched["cgroup"]
        if launched.get("cgroup_marker"):
            rec["cgroup_marker"] = launched["cgroup_marker"]
        if isinstance(launched.get("stop"), dict):
            rec["stop"] = dict(launched["stop"])
        rec.pop("stop_attestation", None)
        if rec.get("generation") is not None:
            self._launched[run_id]["generation"] = rec["generation"]
        return None

    def _stop(self, rec: dict) -> dict:
        """One stop decision on ``rec``. Caller holds the run lock.

        A changed cgroup is cleaned up once more. That budget stays in this frame, so falling back to
        the retained launch cannot open another attempt.
        """
        # A stopped row stays stopped. Adoption is for a launch that has not been recorded, and it
        # must not turn a terminal stop into a pending cgroup-unknown answer.
        if rec.get("state") == "stopped":
            res = {"code": STOPPED, "run_id": rec["run_id"], "state": "stopped", "repeat": True}
            # A stopped launch this process still holds was never written: try again now.
            return self._recorded(res, rec) if rec["run_id"] in self._launched else res
        reconfirms_left = 1
        while True:
            adopted = self._adopt_unrecorded_launch(rec)
            if adopted is not None:
                return adopted
            result = self._stop_attempt(rec, reconfirm=reconfirms_left > 0)
            if not result.pop("_reconfirm", False):
                return result
            reconfirms_left -= 1
            if reconfirms_left < 0:  # the attempt above already consumed the one reconfirm
                return {"code": PENDING, "run_id": rec["run_id"], "state": "pending", "reason": "cgroup_changed",
                        "recorded": False}

    def _stop_attempt(self, rec: dict, *, reconfirm: bool) -> dict:
        """Request and confirm the cgroup on ``rec``, then record that decision. No recursion."""
        run_id = rec["run_id"]
        self._stop_wanted.discard(run_id)
        first = rec["state"] != "stop_pending"
        if rec.get("stop") is None:
            rec["stop"] = {"requested_at": self._clock(), "checks": 0, "reason": None, "requested": False}
        # ``requested`` is only true for the cgroup path and marker stored on the stop itself.
        # A later edit that points the run at another cgroup has not been requested.
        sent = (rec["stop"].get("cgroup"), rec["stop"].get("cgroup_marker"))
        if rec["stop"].get("requested") and sent != self._cgroup_identity(rec):
            rec["stop"]["requested"] = False
        if not rec["stop"].get("requested", True):
            # First stop, a stop intent left during launch, or an earlier request raised before it was known
            # to be sent: (re)send it. A request is not reused for a different cgroup identity.
            try:
                self._kill.request_stop(rec["cgroup"])
            except Exception as err:
                return self._pending(rec, f"cleanup_error:{type(err).__name__}")
            rec["stop"]["requested"] = True
            rec["stop"]["cgroup"] = rec.get("cgroup")
            rec["stop"]["cgroup_marker"] = rec.get("cgroup_marker")
        try:
            answer = self._kill.confirm(rec["cgroup"], self._confirm_timeout_s)
        except Exception as err:
            return self._pending(rec, f"cleanup_error:{type(err).__name__}")
        if answer != EMPTY:
            return self._pending(rec, _PENDING_REASON.get(answer, "confirm_unknown"))
        blocked = self._block_stop(rec)
        if blocked:  # confirm said empty, but a member is still alive: that is not an attested stop
            return self._pending(rec, blocked)
        proof = self._attestation(rec)
        if proof is None:  # cleanup ran, but this copy cannot name a generation: typed, not KeyError
            res = {"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": not first, "recorded": False}
            if run_id in self._launched:
                # Not stored, so the handle must not become a stopped snapshot.
                kept = dict(rec)
                if isinstance(rec.get("stop"), dict):
                    kept["stop"] = dict(rec["stop"])
                self._launched[run_id] = kept
            return res
        rec["state"] = "stopped"
        rec["stop_attestation"] = proof
        rec["stop"].update(checks=rec["stop"]["checks"] + 1, reason=None)
        res = {"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": not first}
        return self._recorded(res, rec, reconfirm=reconfirm)

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

    def _pending_instead(self, rec: dict, reason: str) -> tuple[str, dict]:
        """Write ``rec`` as pending with ``reason``. The stored snapshot replaces ``rec`` on success."""
        stop = rec.get("stop")
        if not isinstance(stop, dict):
            stop = {"requested_at": self._clock(), "checks": 0, "reason": None, "requested": False}
            rec["stop"] = stop
        rec["state"] = "stop_pending"
        rec["stop_attestation"] = None
        stop["checks"] = stop.get("checks", 0) + 1
        stop["reason"] = reason
        res = {"code": PENDING, "run_id": rec["run_id"], "state": "pending", "reason": reason}
        try:
            outcome = self._commit(rec)
        except _RegistryError:
            outcome = "down"
        return outcome, res

    def _unattested_to_pending(self, outcome: str, res: dict, rec: dict) -> tuple[str, dict]:
        """A refused proof, or a probe that failed inside the put, does not answer ``stopped``."""
        if outcome == "liveness" and res.get("code") == STOPPED:
            return self._pending_instead(rec, "liveness_unknown")
        if outcome != "unattested" or res.get("code") != STOPPED:
            return outcome, res
        blocked = self._block_stop(rec)
        return self._pending_instead(rec, blocked or "stop_unattested")

    def _lost_stop_is_pending(self, outcome: str, res: dict, rec: dict) -> tuple[str, dict]:
        """The one retry lost. Pending, unless a later writer stored a recorded stop."""
        if outcome != "stale" or res.get("code") != STOPPED:
            return outcome, res
        latest = self._reread(rec)
        if latest is None:
            # The lost write is not a stopped run. The row could not be read, so a live member
            # may still be on it.
            run_id = rec.get("run_id")
            return "down", {"code": PENDING, "run_id": run_id, "state": "pending", "reason": "unknown"}
        if latest.get("state") == "stopped" and _recorded_stop(latest):
            rec.clear()
            rec.update(latest)
            return "ok", {"code": STOPPED, "run_id": latest["run_id"], "state": "stopped", "repeat": True}
        if latest.get("state") == "stopped":
            # An empty proof, or a member still listed, is not a recorded stop.
            reason = "generation_conflict"
        elif self._cgroup_identity(latest) != self._cgroup_identity(rec):
            reason = "cgroup_changed"
        else:
            reason = self._block_stop(latest) or "generation_conflict"
        latest["stop"] = self._carry_stop(rec.get("stop"), rec, latest)
        outcome, res = self._pending_instead(latest, reason)
        if outcome == "stale":
            # The pending put lost. A writer may have stored a real stop in that window.
            stored = self._reread(latest)
            if stored is not None and _recorded_stop(stored):
                rec.clear()
                rec.update(stored)
                return "ok", {"code": STOPPED, "run_id": stored["run_id"], "state": "stopped", "repeat": True}
        rec.clear()
        rec.update(latest)
        return outcome, res

    def _recorded(self, res: dict, rec: dict, *, reconfirm: bool = False) -> dict:
        # The cleanup layer's answer is the truth; a failed write-back is reported, not raised. The next
        # stop then sees the old state and asks again, which is safe (cgroup.kill and pidfds repeat).
        # A stale generation is not a failed disk: the record moved under us, so the stop is applied to
        # the fresh record once. A member that joined in that window keeps the stop pending.
        try:
            outcome = self._commit(rec)
        except _RegistryError:
            outcome = "down"
        # A refused proof is not a stopped run. A lost disk write still answers with the cleanup
        # result below; this is only the attestation refusal.
        outcome, res = self._unattested_to_pending(outcome, res, rec)
        if outcome == "stale":
            fresh = self._reread(rec)
            if fresh is None:
                # The lost write is not a stopped run. The row could not be read, so a live
                # member may still be on it. Same answer as ``_lost_stop_is_pending``.
                if res.get("code") == STOPPED:
                    res = {"code": PENDING, "run_id": rec.get("run_id"), "state": "pending", "reason": "unknown"}
                outcome = "down"
            elif fresh.get("state") == "stopped" and _recorded_stop(fresh):
                # An older pending or populated result must not demote a stop that already committed.
                rec.clear()
                rec.update(fresh)
                res = {"code": STOPPED, "run_id": fresh["run_id"], "state": "stopped", "repeat": True}
                outcome = "ok"
            elif fresh.get("state") == "stopped":
                # ``state == stopped`` with an empty proof, or with a member still listed, is not
                # a recorded stop. Answer pending instead of repeat-stopped.
                fresh["stop"] = self._carry_stop(rec.get("stop"), rec, fresh)
                outcome, res = self._pending_instead(fresh, "generation_conflict")
                rec.clear()
                rec.update(fresh)
            elif res.get("code") == STOPPED and self._cgroup_identity(fresh) != self._cgroup_identity(rec):
                # The empty result belongs to the cgroup we confirmed, not to the one stored now.
                fresh["stop"] = self._carry_stop(rec.get("stop"), rec, fresh)
                if reconfirm:
                    rec.clear()
                    rec.update(fresh)
                    return {"_reconfirm": True}
                fresh["state"] = "stop_pending"
                fresh["stop"].update(checks=fresh["stop"].get("checks", 0) + 1, reason="cgroup_changed")
                fresh["stop_attestation"] = None
                res = {"code": PENDING, "run_id": rec["run_id"], "state": "pending", "reason": "cgroup_changed"}
                try:
                    outcome = self._commit(fresh)
                except _RegistryError:
                    outcome = "down"
                rec.clear()
                rec.update(fresh)
            else:
                fresh["stop"] = self._carry_stop(rec.get("stop"), rec, fresh)
                if res.get("code") == STOPPED:
                    blocked = self._block_stop(fresh)
                    if blocked or not fresh.get("cgroup"):
                        reason = blocked or "cgroup_unknown"
                        fresh["state"] = "stop_pending"
                        if fresh.get("stop") is None:
                            fresh["stop"] = {"requested_at": self._clock(), "checks": 0, "reason": None,
                                             "requested": False}
                        fresh["stop"].update(checks=fresh["stop"].get("checks", 0) + 1, reason=reason)
                        fresh["stop_attestation"] = None
                        res = {"code": PENDING, "run_id": rec["run_id"], "state": "pending", "reason": reason}
                    else:
                        fresh["state"] = "stopped"
                        fresh["stop_attestation"] = self._attestation(fresh)
                else:
                    fresh["state"] = rec.get("state", fresh.get("state"))
                    fresh["stop_attestation"] = None
                try:
                    outcome = self._commit(fresh)
                except _RegistryError:
                    outcome = "down"
                rec.clear()
                rec.update(fresh)
        # The one retry above can itself lose, or refuse the proof. A later member, or any later
        # writer, then owns the row: answer pending unless that row is already stopped.
        outcome, res = self._unattested_to_pending(outcome, res, rec)
        outcome, res = self._lost_stop_is_pending(outcome, res, rec)
        if outcome != "ok":
            res["recorded"] = False
            if rec["run_id"] in self._launched:  # keep the decision with the launch until a write lands
                self._launched[rec["run_id"]] = self._unrecorded_launch(rec)
        else:
            # A pending row that lost the cgroup is not persistence. The retained handle stays until the
            # cgroup, or a stopped proof that names one, is what got stored.
            self._forget_launch_if_persisted(rec)
        return res

    def _launch_persisted(self, rec: dict) -> bool:
        """True when ``rec`` stores the launched cgroup, or a stopped proof that names one."""
        if rec.get("cgroup"):
            return True
        attestation = rec.get("stop_attestation")
        return (rec.get("state") == "stopped" and isinstance(attestation, dict)
                and attestation.get("proof") == "empty" and bool(attestation.get("cgroup")))

    def _forget_launch_if_persisted(self, rec: dict) -> None:
        if self._launch_persisted(rec):
            self._launched.pop(rec["run_id"], None)

    def _unrecorded_launch(self, rec: dict) -> dict:
        """The handle after a write that did not land.

        A stopped copy is kept only when that stop was stored. This path did not store it, so the
        next stop must not answer ``stopped`` from the handle while a member is still on the row.
        """
        snap = self._launch_snapshot(rec)
        if snap.get("state") == "stopped":
            snap["state"] = "stop_pending"
            snap["stop_attestation"] = None
        return snap

    def _launch_snapshot(self, rec: dict) -> dict:
        """The handle to keep after a failed write. A cgroup-less copy does not replace a retained cgroup."""
        retained = self._launched.get(rec["run_id"])
        kept = dict(rec)
        if isinstance(rec.get("stop"), dict):
            kept["stop"] = dict(rec["stop"])
        if not kept.get("cgroup") and isinstance(retained, dict) and retained.get("cgroup"):
            kept["cgroup"] = retained["cgroup"]
            if not kept.get("cgroup_marker"):
                kept["cgroup_marker"] = retained.get("cgroup_marker")
        return kept


def _recorded_stop(row: dict) -> bool:
    """True when ``row`` stores a stop whose attestation names this cgroup and generation."""
    proof = row.get("stop_attestation")
    if row.get("state") != "stopped" or not isinstance(proof, dict) or proof.get("proof") != "empty":
        return False
    cgroup = row.get("cgroup")
    if not isinstance(cgroup, str) or not cgroup or proof.get("cgroup") != cgroup:
        return False
    if proof.get("cgroup_marker") != row.get("cgroup_marker"):
        return False
    proof_gen, row_gen = proof.get("generation"), row.get("generation")
    if not isinstance(proof_gen, int) or not isinstance(row_gen, int) or proof_gen > row_gen:
        return False
    if not isinstance(proof.get("member_ids"), list) or row.get("members"):
        return False
    return True


def _start_lock_key(slot: tuple[str, str]) -> str:
    return f"a-start:{slot[0]}\0{slot[1]}"


def _run_lock_key(run_id: str) -> str:
    return f"b-run:{run_id}"
