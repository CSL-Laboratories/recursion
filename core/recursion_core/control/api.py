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
``registry_unavailable`` and launches nothing. A record read back without its required fields, or with
the wrong types, is that same failed read and is not written back. Every value is accepted only as an
exact builtin — ``type(v) is str``, ``type(v) is int`` (a ``bool`` is not an int), ``type(v) is bool``,
or ``type(v) is float`` for a timestamp — and copied into a new dict before it is used. A subclass is a
failed read, so a hostile ``__eq__``, ``__hash__``, ``__bool__`` or ``__add__`` is never called. A
``get`` whose ``run_id`` is not the id that was fetched is that same failed read, and Stop never drops
the in-memory launch of a different id. Once the cleanup layer has answered, Stop returns that answer
even if writing it back fails, with ``"recorded": false``. Any other exception inside ``call`` answers
``registry_unavailable`` / ``internal:<type>`` and does not clear a kill that was already requested.

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

Stop: the first call asks the cleanup layer to stop the run's tree, then asks it to confirm. Only an
exact ``str`` equal to empty yields ``stopped``. A populated cgroup, a confirm timeout, a str subclass,
any non-str (even one that compares equal to empty), or a cleanup error yields ``pending``.
``stop.requested`` is stored when ``request_stop`` returns, before confirm, so an answer that cannot be
classified does not signal again.
A repeat stop on a stopped run sends nothing; on a pending run it only re-checks, unless the earlier
cleanup request was never sent (``stop.requested`` is missing or false: it raised, the stored flag was
lost, or it is a stop intent from before the launch was recorded), in which case it sends it
(``cgroup.kill`` and pidfd signals are safe to repeat).
``run.list`` parses every row with that same record parser. A row that is not an exact record is
omitted, and the answer stays ``ok`` with ``malformed`` set to how many rows were omitted (zero when
every row parsed). One bad row does not hide the readable runs and is not returned. A ``list`` result
that is not a list, or a ``list`` call that raises, is ``registry_unavailable``.

Reads never change state and never call the cleanup layer.

``client_key`` dedupes a retried start per tenant and is bound to that start's engine and prompt (the
prompt is kept only as a SHA-256 digest). Another tenant may use the same key; the same tenant reusing a
key for a different request gets ``request_invalid`` / ``client_key_reused`` and nothing is launched. The
key and digest live in the run record (``start_key``) and are looked up through the registry, so a
``ControlAPI`` rebuilt over the same durable registry after a restart still answers the retried start
with the original run (11.3, B-17).

Stop decisions and launch records are serialised per run, so a slow or hung cleanup on one run never
queues another run's Stop (11.3, D2-55 / B-19), and a launch never overwrites a Stop that landed during it.
Starts are serialised only per ``(tenant, client_key)``: a slow or hung starter delays only a retry of the
same start, never another start.
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


def _confirm_reason(answer: object) -> str:
    """Pending reason for a confirm answer other than an exact ``empty`` string.

    A str subclass is not looked up: ``dict.get`` would hash it, and ``__hash__`` may raise after the
    kill has already been sent."""
    if type(answer) is not str:
        return "confirm_unknown"
    return _PENDING_REASON.get(answer, "confirm_unknown")

# starter(params) -> {"cgroup"}: the daemon's launch path (route service + cgroup leaf), injected. params
# carries the API-chosen "run_id", which the starter must launch under.
Starter = Callable[[dict], Mapping[str, str]]


class _RegistryError(Exception):
    pass


class _Malformed(Exception):
    """A registry value was not an exact builtin. Callers turn this into a failed read."""


_RECORD_FIELDS = ("run_id", "tenant", "engine", "state", "cgroup", "started_at", "stop", "start_key")
_STATES = frozenset({"starting", "start_failed", "running", "stop_pending", "stopped"})
_LAUNCHED_STATES = frozenset({"running", "stop_pending", "stopped"})


def _plain_str(value: object, *, nonempty: bool) -> str:
    """Exact ``str``. A subclass is rejected before ``__eq__``, ``__hash__`` or ``__bool__`` runs."""
    if type(value) is not str:
        raise _Malformed()
    if nonempty and value == "":
        raise _Malformed()
    return value


def _plain_int(value: object) -> int:
    """Exact ``int``. ``bool`` is a subclass of ``int`` and is rejected, as is any other subclass."""
    if type(value) is not int:
        raise _Malformed()
    return value


def _plain_bool(value: object) -> bool:
    if type(value) is not bool:
        raise _Malformed()
    return value


def _plain_number(value: object) -> int | float:
    """Timestamp from the injected clock: an exact ``int`` or an exact ``float``.

    ``checks`` is not a timestamp. Stop adds one to it, so it has to be an exact ``int`` (``_plain_int``).
    A ``bool`` is neither an ``int`` nor a ``float`` here."""
    if type(value) is int or type(value) is float:
        return value
    raise _Malformed()


def _parse_stop(stop: object) -> dict | None:
    """A stop object may omit ``requested``; absence means the kill has not been sent."""
    if stop is None:
        return None
    if type(stop) is not dict:
        raise _Malformed()
    if "requested_at" not in stop or "checks" not in stop or "reason" not in stop:
        raise _Malformed()
    reason = stop["reason"]
    if reason is not None:
        reason = _plain_str(reason, nonempty=False)
    parsed = {
        "requested_at": _plain_number(stop["requested_at"]),
        "checks": _plain_int(stop["checks"]),
        "reason": reason,
    }
    if "requested" in stop:
        parsed["requested"] = _plain_bool(stop["requested"])
    return parsed


def _parse_start_key(key: object) -> dict | None:
    if key is None:
        return None
    if type(key) is not dict:
        raise _Malformed()
    if "client_key" not in key or "engine" not in key or "prompt_sha256" not in key:
        raise _Malformed()
    return {
        "client_key": _plain_str(key["client_key"], nonempty=False),
        "engine": _plain_str(key["engine"], nonempty=False),
        "prompt_sha256": _plain_str(key["prompt_sha256"], nonempty=True),
    }


def _parsed_record(rec: object, expected_run_id: str | None) -> dict:
    """Copy ``rec`` into a new dict of exact builtins, or raise ``_Malformed``.

    ``expected_run_id``, when set, is the id that was fetched. The record's ``run_id`` is accepted only
    after it is known to be an exact ``str``, and only when it is that id. A short read or a mistyped
    value is not written back: doing so would drop ``cgroup`` or ``start_key``."""
    if type(rec) is not dict:
        raise _Malformed()
    if any(name not in rec for name in _RECORD_FIELDS):
        raise _Malformed()
    run_id = _plain_str(rec["run_id"], nonempty=True)
    if expected_run_id is not None and run_id != expected_run_id:
        raise _Malformed()
    state = _plain_str(rec["state"], nonempty=False)
    if state not in _STATES:
        raise _Malformed()
    cgroup = rec["cgroup"]
    if cgroup is not None:
        cgroup = _plain_str(cgroup, nonempty=True)
    if state in _LAUNCHED_STATES and cgroup is None:
        raise _Malformed()
    return {
        "run_id": run_id,
        "tenant": _plain_str(rec["tenant"], nonempty=True),
        "engine": _plain_str(rec["engine"], nonempty=True),
        "state": state,
        "cgroup": cgroup,
        "started_at": _plain_number(rec["started_at"]),
        "stop": _parse_stop(rec["stop"]),
        "start_key": _parse_start_key(rec["start_key"]),
    }


def _parse_record(rec: object, expected_run_id: str | None = None) -> dict | None:
    """The plain copy of ``rec``, or None when it must not be used or written back."""
    try:
        return _parsed_record(rec, expected_run_id)
    except _Malformed:
        return None
    except Exception:
        return None


def _snapshot(rec: dict) -> dict:
    """A new dict. ``stop`` and ``start_key`` are not shared with ``rec``."""
    stop, key = rec.get("stop"), rec.get("start_key")
    return {
        "run_id": rec["run_id"],
        "tenant": rec["tenant"],
        "engine": rec["engine"],
        "state": rec["state"],
        "cgroup": rec["cgroup"],
        "started_at": rec["started_at"],
        "stop": None if stop is None else dict(stop),
        "start_key": None if key is None else dict(key),
    }


def _parse_start_params(params: object) -> tuple[dict | None, dict | None]:
    """Caller input as exact strings, or ``(None, error)``. Absent ``client_key`` means no dedupe."""
    try:
        if not isinstance(params, Mapping):
            return None, {"code": REQUEST_INVALID, "detail": "params"}
        raw = dict(params)
    except Exception:
        return None, {"code": REQUEST_INVALID, "detail": "params"}
    plain: dict[str, str] = {}
    for name in ("tenant", "engine", "prompt"):
        value = raw.get(name)
        if type(value) is not str or value == "":
            return None, {"code": REQUEST_INVALID, "detail": name}
        plain[name] = value
    if "client_key" in raw and raw["client_key"] is not None:
        key = raw["client_key"]
        if type(key) is not str:
            return None, {"code": REQUEST_INVALID, "detail": "client_key"}
        plain["client_key"] = key
    return plain, None


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
        self._start_locks: dict[tuple[str, str], threading.Lock] = {}
        # In-memory only (a restart loses them; reconciling then is the CSL-738 startup sweep):
        self._launched: dict[str, dict] = {}  # run_id -> record (with cgroup) of a launch whose write failed
        self._stop_wanted: set[str] = set()  # Stops that arrived before the launch was recorded
        self._failed: set[str] = set()  # starts that raised but whose start_failed write failed

    def call(self, method: str, params: Mapping | None = None) -> dict:
        # The handler does not touch _launched, _stop_wanted or _failed. A kill already requested stays
        # requested when a later bug is turned into a code.
        try:
            if type(method) is not str:
                return {"code": METHOD_UNKNOWN, "detail": type(method).__name__}
            if params is None:
                raw: dict = {}
            else:
                try:
                    raw = dict(params)
                except Exception:
                    return {"code": REQUEST_INVALID, "detail": "params"}
            return self._call(method, raw)
        except _RegistryError as err:  # every method answers with a typed code (red-team r2 finding 1)
            return {"code": REGISTRY_UNAVAILABLE, "detail": str(err)}
        except Exception as err:
            return {"code": REGISTRY_UNAVAILABLE, "detail": f"internal:{type(err).__name__}"}

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
            return {"code": METHOD_UNKNOWN, "detail": method}
        run_id = params.get("run_id")
        if type(run_id) is not str or run_id == "":
            return {"code": REQUEST_INVALID, "detail": "run_id"}
        return handler(run_id)

    # -- reads --------------------------------------------------------------------------------
    def status(self, run_id: str) -> dict:
        rec = self._read_record(run_id)  # same shape check as Stop; a mistyped record is not "ok"
        if rec is None:
            return {"code": RUN_UNKNOWN, "run_id": run_id}
        return {"code": OK, "run": rec}

    def list_runs(self) -> dict:
        # Choice: omit a row the record parser rejects and count it. The readable rows are still
        # returned, under ``ok``. ``malformed`` is 0 when every row parsed. A non-list is a failed read.
        rows = self._reg("list")
        if type(rows) is not list:
            raise _RegistryError("list:malformed")
        runs: list[dict] = []
        malformed = 0
        for row in rows:
            try:
                parsed = _parse_record(row)
            except Exception:
                parsed = None
            if parsed is None:
                malformed += 1
            else:
                runs.append(parsed)
        return {"code": OK, "runs": runs, "malformed": malformed}

    # -- start --------------------------------------------------------------------------------
    def start(self, params: Mapping) -> dict:
        plain, err = _parse_start_params(params)
        if err is not None:
            return err
        key = plain.get("client_key")
        if key is None:
            return self._start(plain)  # no key: a fresh run id, nothing to dedupe
        # tenant and client_key are exact strs, so the lock dict cannot hash a hostile key.
        with self._locks_lock:
            lock = self._start_locks.setdefault((plain["tenant"], key), threading.Lock())
        with lock:
            return self._start(plain)

    def _now(self) -> int | float:
        value = self._clock()
        if type(value) is int or type(value) is float:
            return value
        raise _RegistryError("clock:malformed")

    def _start(self, plain: dict) -> dict:
        key = plain.get("client_key")
        start_key = None
        if key is not None:
            # surrogatepass: a lone surrogate is a real prompt and must not raise out of call.
            digest = hashlib.sha256(plain["prompt"].encode("utf-8", "surrogatepass")).hexdigest()
            start_key = {"client_key": key, "engine": plain["engine"], "prompt_sha256": digest}
        prior = self._read_start(plain["tenant"], key) if start_key is not None else None
        if prior is not None and prior["start_key"] != start_key:
            return {"code": REQUEST_INVALID, "detail": "client_key_reused"}
        run_id = prior["run_id"] if prior is not None else self._new_run_id()
        if type(run_id) is not str or run_id == "":
            raise _RegistryError("get:malformed")
        rec = {"run_id": run_id, "tenant": plain["tenant"], "engine": plain["engine"], "state": "starting",
               "cgroup": None, "started_at": self._now(), "stop": None, "start_key": start_key}
        with self._run_lock(run_id):
            if prior is not None:  # a retried start is not run twice, across restarts too (11.3, 11.8)
                answer = self._retry(self._current(prior), run_id)
                if answer is not None:
                    return answer
            self._reg("put", rec)  # launch intent: nothing runs that the registry cannot list, stop or dedupe
            self._failed.discard(run_id)
        launch = {"tenant": plain["tenant"], "engine": plain["engine"], "prompt": plain["prompt"], "run_id": run_id}
        if key is not None:
            launch["client_key"] = key
        try:
            handle = self._starter(launch)
        except Exception as err:  # the launch path refused or failed; the API answers with a code
            with self._run_lock(run_id):
                # Mark the failure before the re-read. A torn read must not skip the mark: the stored
                # record still says starting, and a same-key retry would stay start_unconfirmed forever.
                self._failed.add(run_id)
                # Re-read first: a Stop may have written its intent while the starter ran. If the read fails,
                # write nothing rather than overwrite that intent with the launch-intent copy (t1430u X1c).
                try:
                    current = self._read_record(run_id)
                except _RegistryError:
                    current = None
                recorded = False
                if current is not None:
                    current["state"] = "start_failed"
                    recorded = self._try_put(current)
                if recorded:  # the registry itself now says start_failed
                    self._failed.discard(run_id)
            return {"code": START_FAILED, "run_id": run_id, "detail": type(err).__name__, "recorded": recorded}
        cgroup = None
        if isinstance(handle, Mapping):
            try:
                cgroup = handle.get("cgroup")
            except Exception:
                cgroup = None
        if type(cgroup) is not str or cgroup == "":
            return {"code": START_UNCONFIRMED, "run": rec, "detail": "starter_handle_invalid"}
        with self._run_lock(run_id):
            # Known to this process before the re-read, so a torn read cannot discard the cgroup
            # (red-team r3 finding 1). Stop and a retry can still aim at it if the write below fails.
            held = _snapshot(rec)
            held["state"] = "running"
            held["cgroup"] = cgroup
            self._launched[run_id] = held
            current = _snapshot(self._current(rec))  # re-read: keeps a stop intent written while the starter ran
            if current.get("run_id") != run_id:
                # The re-read named another run. Keep this launch; do not write its cgroup onto that run.
                current = _snapshot(held)
            current["state"] = "running"
            current["cgroup"] = cgroup
            self._launched[run_id] = _snapshot(current)
            if current.get("stop") is not None or run_id in self._stop_wanted:
                # A Stop arrived during the launch and answered pending; act on it now (red-team r3 finding 2).
                stop = self._stop(current, run_id)
                code = START_UNCONFIRMED if stop.get("recorded") is False else STARTED
                return {"code": code, "run": current, "duplicate": False, "stop": stop}
            if not self._try_put(current):
                return {"code": START_UNCONFIRMED, "run_id": run_id, "detail": "registry_unavailable"}
            self._launched.pop(run_id, None)
            return {"code": STARTED, "run": current, "duplicate": False}

    def _retry(self, prior: dict, run_id: str) -> dict | None:
        """Answer a same-key retry from the existing record, or None to relaunch it. Caller holds the run lock.

        A retry repeats an unconfirmed or failed answer; it never upgrades it to ``started`` (red-team r3
        finding 3). ``state`` is an exact str by the time it is compared."""
        if prior.get("run_id") != run_id or type(prior.get("state")) is not str:
            raise _RegistryError("get:malformed")
        state = prior["state"]
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
        prior = _snapshot(launched)
        if prior.get("run_id") != run_id:
            raise _RegistryError("get:malformed")
        if prior.get("stop") is not None:
            stop = self._stop(prior, run_id)  # a kept "stopped" is written back; anything else is continued
            code = START_UNCONFIRMED if stop.get("recorded") is False else STARTED
            return {"code": code, "run": prior, "duplicate": True, "stop": stop}
        if not self._try_put(prior):
            return {"code": START_UNCONFIRMED, "run": prior, "duplicate": True, "detail": "registry_unavailable"}
        self._launched.pop(run_id, None)
        return {"code": STARTED, "run": prior, "duplicate": True}

    def _read_record(self, run_id: str) -> dict | None:
        """The registry's plain copy, or a failed read when that copy is not this id."""
        if type(run_id) is not str or run_id == "":
            raise _RegistryError("get:malformed")
        rec = self._reg("get", run_id)
        if rec is None:
            return None
        parsed = _parse_record(rec, expected_run_id=run_id)
        if parsed is None:
            raise _RegistryError("get:malformed")
        return parsed

    def _read_start(self, tenant: str, client_key: str) -> dict | None:
        """The indexed start, parsed, and only when it is this tenant and key."""
        rec = self._reg("find_start", tenant, client_key)
        if rec is None:
            return None
        parsed = _parse_record(rec)
        if parsed is None or parsed["tenant"] != tenant:
            raise _RegistryError("find_start:malformed")
        key = parsed["start_key"]
        if key is None or key.get("client_key") != client_key:
            raise _RegistryError("find_start:malformed")
        return parsed

    def _current(self, rec: dict) -> dict:
        """The registry's copy of ``rec``, or ``rec`` itself if the registry cannot answer.

        A short read, or a read whose ``run_id`` is not the one fetched, is a failed read. The copy
        already held is complete; the torn view is not returned and so cannot be written back."""
        try:
            current = self._read_record(rec["run_id"])
        except _RegistryError:
            current = None
        return current if current is not None else _snapshot(rec)

    def _try_put(self, rec: dict) -> bool:
        plain = _parse_record(rec)
        if plain is None:
            return False
        try:
            self._reg("put", plain)
        except _RegistryError:
            return False
        return True

    def _run_lock(self, run_id: str) -> threading.Lock:
        if type(run_id) is not str or run_id == "":
            raise _RegistryError("get:malformed")
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
            return self._stop(rec, run_id)

    def _stop_target(self, run_id: str) -> dict | None:
        """The registry's record, or, when the registry cannot be read, the record this process kept for a
        launch whose write failed: that run is live and its cgroup is known, so Stop still reaches cleanup
        (S6). Any other read failure answers ``registry_unavailable``. The fallback is only this id."""
        try:
            return self._read_record(run_id)
        except _RegistryError:
            launched = self._launched.get(run_id)
            if launched is None:
                raise
            return _snapshot(launched)

    def _stop(self, rec: dict, run_id: str) -> dict:
        """One stop decision on ``rec``, which must be the run ``run_id``. Caller holds the run lock."""
        if type(run_id) is not str or rec.get("run_id") != run_id:
            raise _RegistryError("get:malformed")
        plain = _parse_record(rec, expected_run_id=run_id)
        if plain is None:
            raise _RegistryError("get:malformed")
        rec = plain
        if rec["state"] == "stopped":
            res = {"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": True}
            # A stopped launch this process still holds was never written: try again now.
            return self._recorded(res, rec, run_id) if run_id in self._launched else res
        if not rec["cgroup"]:
            launched = self._launched.get(run_id)
            if launched is None:  # launch not confirmed yet (starting / start_failed): keep the Stop as intent
                return self._stop_intent(rec, run_id)
            # Launched, write after launch failed: aim at it now, from the kept record, so a Stop whose own
            # write-back failed earlier is continued rather than started again.
            kept = _parse_record(_snapshot(launched), expected_run_id=run_id)
            if kept is None:
                raise _RegistryError("get:malformed")
            rec = kept
            if rec["state"] == "stopped":
                return self._recorded(
                    {"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": True}, rec, run_id)
        self._stop_wanted.discard(run_id)
        first = rec["state"] != "stop_pending"
        if rec.get("stop") is None:
            rec["stop"] = {"requested_at": self._now(), "checks": 0, "reason": None, "requested": False}
        if not rec["stop"].get("requested", False):
            # First stop, a missing flag, a stop intent left during launch, or an earlier request that
            # raised before it was known to be sent: (re)send it. A missing flag is not "already sent".
            cgroup = rec["cgroup"]
            if type(cgroup) is not str or cgroup == "":
                raise _RegistryError("get:malformed")
            try:
                self._kill.request_stop(cgroup)
            except Exception as err:
                return self._pending(rec, f"cleanup_error:{type(err).__name__}", run_id)
            rec["stop"]["requested"] = True
            # The signal has been sent. Store that before confirm: an unclassifiable answer, or a
            # pending write that does not land, must not make the next stop signal again.
            if run_id in self._launched:
                self._launched[run_id] = _snapshot(rec)
            self._try_put(rec)
        try:
            answer = self._kill.confirm(rec["cgroup"], self._confirm_timeout_s)
        except Exception as err:
            return self._pending(rec, f"cleanup_error:{type(err).__name__}", run_id)
        if type(answer) is not str or answer != EMPTY:
            return self._pending(rec, _confirm_reason(answer), run_id)
        rec["state"] = "stopped"
        self._bump(rec["stop"], None)
        res = {"code": STOPPED, "run_id": run_id, "state": "stopped", "repeat": not first}
        return self._recorded(res, rec, run_id)

    def _bump(self, stop: dict, reason: str | None) -> None:
        checks = stop.get("checks")
        if type(checks) is not int:
            raise _RegistryError("get:malformed")
        stop["checks"] = checks + 1
        stop["reason"] = reason

    def _stop_intent(self, rec: dict, run_id: str) -> dict:
        # Red-team r3 finding 2: the Stop is kept (in the record and in memory), and the start that is
        # still launching acts on it once the launch is recorded. The record keeps its launch state.
        self._stop_wanted.add(run_id)
        if rec.get("stop") is None:
            rec["stop"] = {"requested_at": self._now(), "checks": 0, "reason": None, "requested": False}
        self._bump(rec["stop"], "cgroup_unknown")
        res = {"code": PENDING, "run_id": run_id, "state": "pending", "reason": "cgroup_unknown"}
        if not self._try_put(rec):
            res["recorded"] = False
        return res

    def _pending(self, rec: dict, reason: str, run_id: str) -> dict:
        rec["state"] = "stop_pending"
        self._bump(rec["stop"], reason)
        return self._recorded(
            {"code": PENDING, "run_id": run_id, "state": "pending", "reason": reason}, rec, run_id)

    def _recorded(self, res: dict, rec: dict, run_id: str) -> dict:
        # The cleanup layer's answer is the truth; a failed write-back is reported, not raised. The next
        # stop then sees the old state and asks again, which is safe (cgroup.kill and pidfds repeat).
        # Only the run being stopped may leave _launched. A record that names another id is not written
        # and does not pop that other id's launch.
        if rec.get("run_id") != run_id:
            res["recorded"] = False
            return res
        if not self._try_put(rec):
            res["recorded"] = False
            if run_id in self._launched:  # keep the decision with the launch until a write lands
                self._launched[run_id] = _snapshot(rec)
        else:
            self._launched.pop(run_id, None)  # the cgroup is in the registry now
        return res
