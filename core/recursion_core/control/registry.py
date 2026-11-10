"""Run registry and cleanup interfaces the control API depends on (CSL-798, candidate). See README.md.

The durable registry (B8/B9) and the cgroup v2 + pidfd cleanup layer (B6) are other build items; the
control API only sees these two interfaces, and tests fake them. A run record is a plain dict::

    {"run_id", "tenant", "engine",
     "state": "starting" | "start_failed" | "running" | "stop_pending" | "stopped",
     "cgroup": None | "<cgroup path or handle id>", "started_at",
     "stop": None | {"requested_at", "checks", "reason", "requested"},
     "start_key": None | {"client_key", "engine", "prompt_sha256"},
     "generation": <int>,
     "members": [{"member_id", "pid"}],
     "stop_attestation": None | {"proof": "empty", "cgroup", "member_ids", "generation"}}

``start_key`` is stored with the record so start dedupe survives a daemon restart: a durable registry
must persist it and answer ``find_start`` from what it persisted. A ``starting`` record is the launch
intent the control API writes before it calls the starter; ``cgroup`` is None until the launch is recorded.

Updates are a compare-and-swap on ``generation``. ``put`` of an existing run stores the record only when
``record["generation"]`` is the generation ``get`` returned; the stored generation becomes that value plus
one. A writer that read an older generation raises ``StaleGeneration`` and does not overwrite a stop.
``state == "stopped"`` is stored only with an attestation that names this generation and the current
member ids, and only when none of those members is still alive. A member join after that stop is refused.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from typing import Protocol

# Answers from KillTree.confirm. Only EMPTY means the run's cgroup holds no process.
EMPTY, POPULATED, TIMEOUT, UNKNOWN = "empty", "populated", "timeout", "unknown"


class StaleGeneration(Exception):
    """A compare-and-swap named a generation that is no longer the stored one."""

    def __init__(self, run_id: str, expected: int | None, actual: int | None) -> None:
        self.run_id, self.expected, self.actual = run_id, expected, actual
        super().__init__(f"{run_id}: expected generation {expected}, have {actual}")


class StopUnattested(Exception):
    """``stopped`` was written without proof that every current member is gone."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(run_id)


class JoinRefused(Exception):
    """A member join onto a stopped run, an unknown run, or a conflicting member id."""

    def __init__(self, run_id: str, detail: str = "stopped") -> None:
        self.run_id, self.detail = run_id, detail
        super().__init__(f"{run_id}: {detail}")


class LockNotHeld(Exception):
    """``release`` named a key this owner does not hold."""


class LockOrderError(Exception):
    """A nested acquire asked for a key that sorts before a key this owner already holds.

    Callers pass the whole set to one ``acquire`` (order does not matter; it is sorted) or nest
    only toward keys that sort later. Either way two acquires cannot wait on each other.
    """

    def __init__(self, needed: str, held: str) -> None:
        self.needed, self.held = needed, held
        super().__init__(f"lock {needed} sorts before held lock {held}")


class OwnerLockTable:
    """Locks named by stable keys and owned by a caller-minted identity, not by a thread id.

    ``acquire`` sorts its keys and takes them under one condition, so two callers that pass the
    same keys in opposite orders cannot deadlock. A nested acquire of a key that sorts before a
    key this owner already holds raises ``LockOrderError`` instead of waiting. ``release`` checks
    the owner token, so a different thread, or a thread whose id was reused, cannot drop the lock.
    A key with no holder and no waiter is deleted.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._owner: dict[str, str] = {}
        self._count: dict[str, int] = {}

    def acquire(self, owner: str, keys: list[str]) -> None:
        if not isinstance(owner, str) or not owner:
            raise ValueError("owner")
        ordered = sorted(set(keys))
        if not ordered:
            return
        with self._cv:
            held = [key for key, who in self._owner.items() if who == owner]
            fresh = [key for key in ordered if self._owner.get(key) != owner]
            if held and fresh and min(fresh) < max(held):
                raise LockOrderError(min(fresh), max(held))
            while any(self._owner.get(key) not in (None, owner) for key in ordered):
                self._cv.wait()
            for key in ordered:
                if self._owner.get(key) == owner:
                    self._count[key] += 1
                else:
                    self._owner[key] = owner
                    self._count[key] = 1

    def release(self, owner: str, keys: list[str]) -> None:
        with self._cv:
            for key in sorted(set(keys)):
                if self._owner.get(key) != owner:
                    raise LockNotHeld(key)
                self._count[key] -= 1
                if self._count[key] == 0:
                    del self._count[key]
                    del self._owner[key]
            self._cv.notify_all()

    def held_keys(self) -> list[str]:
        with self._cv:
            return sorted(self._owner)

    def owner_of(self, key: str) -> str | None:
        with self._cv:
            return self._owner.get(key)


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
    runs accumulate (a durable registry should index it too). The index keeps every run id for a key, in
    first-seen order. Changing or clearing the first record's key leaves the next record that still
    carries that key (bot review 4517642164).

    ``processes`` is ``pid -> bool`` (alive). With no table, a member that has a pid cannot be proved
    gone, so a stop that still lists that member is refused. ``commit_gate``, when set, is called with
    a snapshot after the generation check and before the store, and the check is repeated afterwards,
    so tests can park two writers on one generation.
    """

    def __init__(self, records: list[dict] | None = None, *,
                 processes: Callable[[int], bool] | None = None) -> None:
        self._lock = threading.Lock()
        self.locks = OwnerLockTable()
        self._processes = processes
        self.commit_gate: Callable[[dict], None] | None = None
        self._runs: dict[str, dict] = {}
        self._starts: dict[tuple[str, str], list[str]] = {}  # (tenant, client_key) -> run ids, first seen first
        for raw in records or []:
            rec = copy.deepcopy(raw)
            rec.setdefault("generation", 1)
            rec.setdefault("members", [])
            rec.setdefault("stop_attestation", None)
            self._store(rec)

    def process_alive(self, pid: int | None) -> bool:
        """True when ``pid`` is a member we cannot show is gone."""
        if pid is None:
            return False
        if self._processes is None:
            return True
        return bool(self._processes(pid))

    def _store(self, rec: dict) -> None:
        old = self._runs.get(rec["run_id"])
        old_key = _start_index_key(old) if old is not None else None
        new_key = _start_index_key(rec)
        if old_key is not None and old_key != new_key:
            self._unlink_start(old_key, rec["run_id"])
        self._runs[rec["run_id"]] = rec
        if new_key is None:
            return
        ids = self._starts.setdefault(new_key, [])
        if rec["run_id"] not in ids:  # same key on update keeps its place; a new duplicate is appended
            ids.append(rec["run_id"])

    def _unlink_start(self, key: tuple[str, str], run_id: str) -> None:
        ids = self._starts.get(key)
        if not ids or run_id not in ids:
            return
        ids.remove(run_id)
        if not ids:
            del self._starts[key]

    def _drop_dangling_index(self) -> list[dict]:
        """Drop index ids whose run is gone. Caller holds ``_lock``."""
        dropped: list[dict] = []
        for key, ids in list(self._starts.items()):
            kept = [run_id for run_id in ids if run_id in self._runs]
            for run_id in ids:
                if run_id not in self._runs:
                    dropped.append({"index_key": key, "run_id": run_id})
            if kept:
                self._starts[key] = kept
            else:
                del self._starts[key]
        return dropped

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            rec = self._runs.get(run_id)
            return copy.deepcopy(rec) if rec is not None else None

    def list(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(self._runs[k]) for k in sorted(self._runs)]

    def put(self, record: dict) -> None:
        """Insert a new run, or replace one when ``record["generation"]`` is current.

        An update that omits ``generation``, or carries an older one, raises ``StaleGeneration``
        and leaves the stored record unchanged. A transition to ``stopped`` raises ``StopUnattested``
        unless the attestation names this generation and every current member is gone.
        """
        if self.commit_gate is None:
            with self._lock:
                self._commit_locked(record)
            return
        with self._lock:
            self._commit_locked(record, store=False)
            snapshot = {"op": "put", "run_id": record["run_id"], "generation": record.get("generation")}
        self.commit_gate(snapshot)
        with self._lock:
            self._commit_locked(record)

    def compare_and_swap(self, run_id: str, expected_generation: int, record: dict) -> dict:
        """Store ``record`` only when the run's generation is still ``expected_generation``.

        ``expected_generation`` 0 inserts. On success the stored generation is one higher, and the
        returned value is a copy of what was stored.
        """
        rec = copy.deepcopy(record)
        rec["run_id"] = run_id
        rec["generation"] = expected_generation
        self.put(rec)
        stored = self.get(run_id)
        if stored is None:  # a gate or a subclass refused the write after a passing check
            raise StaleGeneration(run_id, expected_generation, None)
        return stored

    def join_member(self, run_id: str, expected_generation: int, member_id: str, pid: int) -> dict:
        """Add a member if the generation still matches and the run is not stopped.

        The same ``(member_id, pid)`` is idempotent and does not bump the generation. A new member
        bumps it, so a stop attestation built from the previous generation cannot commit over the join.
        """
        if not isinstance(member_id, str) or not member_id or not isinstance(pid, int):
            raise JoinRefused(run_id, "member")

        def attempt() -> dict:
            with self._lock:
                return self._join_locked(run_id, expected_generation, member_id, pid)

        if self.commit_gate is None:
            return attempt()
        with self._lock:
            self._join_locked(run_id, expected_generation, member_id, pid, store=False)
            snapshot = {"op": "join", "run_id": run_id, "generation": expected_generation,
                        "member_id": member_id}
        self.commit_gate(snapshot)
        return attempt()

    def reap_orphans(self, is_alive: Callable[[int], bool]) -> list[dict]:
        """Drop members whose process is gone, and index ids that name no run.

        A reaped member bumps that run's generation and clears a stop attestation, so an in-flight
        stop has to be proved again. Reaping does not itself record ``stopped``.
        """
        reaped: list[dict] = []
        with self._lock:
            for run_id, rec in list(self._runs.items()):
                dead = [member for member in rec.get("members") or [] if not is_alive(member["pid"])]
                if not dead:
                    continue
                live = [member for member in rec["members"] if is_alive(member["pid"])]
                updated = copy.deepcopy(rec)
                updated["members"] = live
                updated["generation"] = rec["generation"] + 1
                updated["stop_attestation"] = None
                self._store(updated)
                reaped.extend({"run_id": run_id, "member_id": member["member_id"], "pid": member["pid"]}
                              for member in dead)
            reaped.extend(self._drop_dangling_index())
        return reaped

    def _commit_locked(self, record: dict, *, store: bool = True) -> None:
        run_id = record["run_id"]
        current = self._runs.get(run_id)
        if current is None:
            if record.get("generation") not in (None, 0):
                raise StaleGeneration(run_id, record.get("generation"), None)
            stored = self._apply(None, 0, record)
        else:
            expected = record.get("generation", None)
            if expected != current["generation"]:
                raise StaleGeneration(run_id, expected if isinstance(expected, int) else None, current["generation"])
            stored = self._apply(current, expected, record)
        if store:
            self._store(stored)

    def _apply(self, current: dict | None, expected: int, record: dict) -> dict:
        stored = copy.deepcopy(record)
        stored.pop("generation", None)
        if current is None:
            members = [_member(m) for m in stored.get("members") or []]
        else:
            members = [_member(m) for m in current.get("members") or []]
        if stored.get("state") == "stopped":
            if current is not None and current.get("state") == "stopped":
                attestation = current.get("stop_attestation")
                if not isinstance(attestation, dict) or attestation.get("proof") != "empty":
                    raise StopUnattested(stored["run_id"])
                stored["stop_attestation"] = copy.deepcopy(attestation)
                stored["members"] = []
            else:
                if not self._attestation_ok(stored, members, expected):
                    raise StopUnattested(stored.get("run_id", ""))
                stored["members"] = []  # proved gone; a stopped run does not keep member rows
        else:
            stored["members"] = members
            stored["stop_attestation"] = None
        stored["generation"] = expected + 1
        return stored

    def _attestation_ok(self, record: dict, members: list[dict], expected: int) -> bool:
        attestation = record.get("stop_attestation")
        if not isinstance(attestation, dict) or attestation.get("proof") != "empty":
            return False
        if attestation.get("generation") != expected or attestation.get("cgroup") != record.get("cgroup"):
            return False
        if not record.get("cgroup"):
            return False
        member_ids = attestation.get("member_ids")
        if member_ids != sorted(member["member_id"] for member in members):
            return False
        return not any(self.process_alive(member.get("pid")) for member in members)

    def _join_locked(self, run_id: str, expected_generation: int, member_id: str, pid: int,
                     *, store: bool = True) -> dict:
        current = self._runs.get(run_id)
        if current is None:
            raise JoinRefused(run_id, "unknown")
        if current["generation"] != expected_generation:
            raise StaleGeneration(run_id, expected_generation, current["generation"])
        if current.get("state") == "stopped":
            raise JoinRefused(run_id, "stopped")
        members = [_member(m) for m in current.get("members") or []]
        for member in members:
            if member["member_id"] == member_id:
                if member["pid"] == pid:
                    return copy.deepcopy(current)
                raise JoinRefused(run_id, "member_id")
        if not store:
            return copy.deepcopy(current)
        updated = copy.deepcopy(current)
        updated["members"] = sorted([*members, {"member_id": member_id, "pid": pid}],
                                    key=lambda item: item["member_id"])
        updated["generation"] = expected_generation + 1
        updated["stop_attestation"] = None
        self._store(updated)
        return copy.deepcopy(updated)

    def find_start(self, tenant: str, client_key: str) -> dict | None:
        with self._lock:
            ids = self._starts.get((tenant, client_key))
            if not ids:
                return None
            for run_id in ids:  # a dangling index id is skipped; reap_orphans drops it
                rec = self._runs.get(run_id)
                if rec is not None:
                    return copy.deepcopy(rec)
            return None


def _member(member: dict) -> dict:
    return {"member_id": member["member_id"], "pid": member["pid"]}


def _start_index_key(rec: dict) -> tuple[str, str] | None:
    key = rec.get("start_key")
    if not isinstance(key, dict) or not isinstance(key.get("client_key"), str):
        return None
    return (rec.get("tenant"), key["client_key"])
