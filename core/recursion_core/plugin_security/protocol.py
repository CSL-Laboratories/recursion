"""The core's language-neutral boundary (``csl-core/v0``): canonical JSON bytes in and out.

Plugins and consumers (including in-process plugins) reach the approval gate, the authority ledger
and the breaker only through ``CoreService.client_for(caller)``, a callable taking
request bytes and returning response bytes. The ops, their arguments and the order of
checks are listed in ``protocol.json`` beside this file, so a later Rust core can be
driven by the same test vectors (the owner, Nov 7: keep the core's boundaries
language-neutral). The core holds no consumer-specific code: which effect kinds need
which permission, and which permissions need an authority profile, are injected.

``effect.authorize`` runs the checks in this order and stops at the first refusal:
core health, caller is the effect's plugin, permission, run (owned by the caller, on
the effect's connection, not faulted, connection not tripped),
the one-use approval for this exact digest, then the authority profile and budgets.
Its commit section (below) checks health, permission, run and a veto once more, before
the ledger's budgets and the commit.

A veto is final for that effect digest (CSL-743): after ``approval.decide`` with
``veto``, ``effect.request`` for the same effect, ``approval.decide`` with ``approve``
on another approval for it and ``effect.authorize`` with one approved earlier all
refuse ``approval_vetoed``. The gate's refusal code goes on the wire unchanged.

A tripped connection clears only through the host ops ``breaker.probe_begin`` (one
lease per tripped connection) and ``breaker.probe_reset`` (spends the lease; clears the
trip only for a live lease and ``probe_ok`` true), so a consumer that holds only a
``client_for`` client can still run the leased probe.

The commit fence (review finding R4-1; decision D3 in CSL-DECISIONS.md: "Once a halt,
veto or key revocation is acknowledged, every authorization not yet committed is
refused"). ``effect.authorize`` checks, takes the approval and verifies the authority
profile without the fence: verification is the slow step, and a halt must not wait for
it. Its commit section then holds ``commit_fence``: it checks core health, permission
(a key revocation turns ``order`` off), the run's fault, the connection's trip and a veto
of the digest again, and only then commits: the ledger's charge for an order; for an
effect that is not charged, the end of the commit section is its commit, and the answer
is built and sent after it. A refusal there rolls the charge back, and the approval stays
used. The host ops that acknowledge a halt (``run.fault``, ``breaker.report`` when it
trips, ``key.revoke`` and ``approval.decide`` with ``veto``) change their state, then
drain the fence before they answer, so a commit section that read the old state has
committed by the time they answer and every later one reads the new state.
A trip is the one halt that can be cleared while an authorization is in flight (a
successful leased probe resets it), so the current state alone could revive an
authorization that an acknowledged trip had already cancelled. ``effect.authorize``
therefore reads the fence's position before any check, ``breaker.report`` (when it trips)
and ``breaker.probe_reset`` (when it resets) mark a change of that connection's trip state,
and the commit section refuses ``connection_tripped`` if the effect's connection changed
since the authorization started.

Lock order: the ledger's lock, then the fence, then the gate's and the breaker store's
locks, which the commit section reads under. A halt changes state under its own lock and
drains with no lock of this package held, so it never holds the fence while it waits for
a lock (``key.revoke`` takes the loader's lock, and the loader's lock is held when
``CoreHealth.poison`` runs). What the fence does not reach yet: ``CoreHealth.poison``,
the daemon's own fault, trip and reset writes (``daemon/breaker.py``) and other direct
writes to the breaker store neither drain it nor mark a trip change, so for them only the
recheck of the current state holds: a poison, fault or trip that lands after the commit
section's recheck and before its commit does not refuse that one commit, and a direct trip
that a direct reset clears before the recheck is not seen. Those are requests to their
keeper; ``commit_fence`` is injectable so that one fence can be shared with them.
"""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from typing import Callable, Iterator

from .approval import ApprovalGate, check_effect
from .authority import Authority
from .breaker import ConnectionBreaker
from .canonical import canonical, parse
from .errors import Refused
from .loader import CoreHealth, PluginLoader
from .manifest import RESERVED_PLUGIN_IDS

__all__ = ["HOST", "PROTOCOL", "PROTOCOL_VERSION", "CommitFence", "CoreService"]

PROTOCOL = json.loads((Path(__file__).with_name("protocol.json")).read_text(encoding="utf-8"))
PROTOCOL_VERSION = PROTOCOL["protocol"]
HOST = "host"
if HOST not in RESERVED_PLUGIN_IDS:  # no plugin manifest may name itself the host
    raise RuntimeError("host caller name must be a reserved plugin id")


class CommitFence:
    """The one point an authorization commits through (R4-1, D3).

    ``with fence:`` holds it for one commit section: the recheck and the commit. ``drain()``
    returns once every commit section that held the fence when it was called has ended, so a
    halt that changed its state and then drained can answer: a commit that read the old state
    is done, and every later commit section reads the new state. Not reentrant: entering again
    or draining on the thread that holds a commit section raises ``RuntimeError`` instead of
    waiting for itself, and nothing drains while holding a lock that a commit section takes.

    It also counts changes of the one halt that can be cleared while an authorization is in
    flight: a connection's trip (a successful leased probe resets it). ``position()`` is read
    when an authorization starts; ``mark_trip_change(connection)`` is called after a trip and
    after a reset; ``trip_changed_since(connection, position)`` tells the commit section that
    the connection's trip state changed while that authorization was in flight, even if a reset
    has cleared it since. A fault, a veto, a poison and a revocation are never undone in a
    running core, so the recheck of the current state already holds for them.
    """

    __slots__ = ("_lock", "_holder", "_marks_lock", "_position", "_trip_changes")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: int | None = None  # the thread holding a commit section, set and cleared under _lock
        self._marks_lock = threading.Lock()  # a leaf lock: nothing is taken while it is held
        self._position = 0
        self._trip_changes: dict[str, int] = {}

    def position(self) -> int:
        with self._marks_lock:
            return self._position

    def mark_trip_change(self, connection: str) -> None:
        with self._marks_lock:
            self._position += 1
            self._trip_changes[connection] = self._position

    def trip_changed_since(self, connection: str, position: int) -> bool:
        with self._marks_lock:
            return self._trip_changes.get(connection, 0) > position

    def __enter__(self) -> "CommitFence":
        if self._holder == threading.get_ident():
            raise RuntimeError("commit fence: this thread already holds a commit section")
        self._lock.acquire()
        self._holder = threading.get_ident()
        return self

    def __exit__(self, *exc: object) -> None:
        self._holder = None
        self._lock.release()

    def drain(self) -> None:
        # Only the holder ever sets _holder to its own id, so this thread reads its own id here exactly
        # when it holds the fence: a drain there would wait for itself for ever, so it is refused.
        if self._holder == threading.get_ident():
            raise RuntimeError("commit fence: a drain inside a commit section would wait for itself")
        with self._lock:
            pass


class CoreService:
    def __init__(self, *, loader: PluginLoader, gate: ApprovalGate, breaker: ConnectionBreaker,
                 health: CoreHealth, authority: Authority | None,
                 effect_permissions: dict[str, str], authority_required: frozenset,
                 commit_fence: CommitFence | None = None) -> None:
        self._loader = loader
        self._gate = gate
        self._breaker = breaker
        self._health = health
        self._authority = authority
        self._effect_permissions = dict(effect_permissions)
        self._authority_required = frozenset(authority_required)
        if commit_fence is not None and type(commit_fence) is not CommitFence:
            raise TypeError("commit_fence must be a CommitFence")
        self._commit_fence = commit_fence if commit_fence is not None else CommitFence()

    @property
    def commit_fence(self) -> CommitFence:
        """The fence this core commits through, set once at construction: share one by passing it in."""
        return self._commit_fence

    def client_for(self, caller: str) -> Callable[[bytes], bytes]:
        def call(request: bytes) -> bytes:
            return self.handle(caller, request)
        return call

    def handle(self, caller: str, request: bytes) -> bytes:
        try:
            message = parse(request)
            if type(message) is not dict or set(message) != {"protocol", "op", "args"} \
                    or message["protocol"] != PROTOCOL_VERSION or type(message["args"]) is not dict:
                raise Refused("protocol_request_invalid")
            op = message["op"]
            if op not in PROTOCOL["ops"]:
                raise Refused("protocol_unknown_op")
            if set(message["args"]) != set(PROTOCOL["ops"][op]["args"]):
                raise Refused("protocol_args_invalid")
            result = getattr(self, "_op_" + op.replace(".", "_"))(caller, **message["args"])
            body = {"protocol": PROTOCOL_VERSION, "ok": True, "result": result}
        except Refused as exc:
            body = {"protocol": PROTOCOL_VERSION, "ok": False, "refusal": {"code": exc.code}}
        except Exception:  # noqa: BLE001 - the boundary fails closed with a typed code, never raises
            body = {"protocol": PROTOCOL_VERSION, "ok": False, "refusal": {"code": "core_internal_error"}}
        return canonical(body)

    def _permission_for(self, kind: str) -> str:
        family = kind.split(".", 1)[0]
        if family not in self._effect_permissions:
            raise Refused("effect_kind_unmapped")
        return self._effect_permissions[family]

    def _check_plugin_effect(self, caller: str, effect: dict) -> tuple[str, dict]:
        self._health.require_healthy()
        check_effect(effect)
        if caller != effect["plugin_id"]:
            raise Refused("caller_not_effect_plugin")
        permission = self._permission_for(effect["kind"])
        if permission not in self._loader.permissions(caller):
            raise Refused("permission_required", permission)
        run = self._breaker.check(effect["run_id"], effect["connection"], caller)
        return permission, run

    def _op_plugin_permissions(self, caller: str, plugin_id: str) -> dict:
        return {"permissions": list(self._loader.permissions(plugin_id))}

    def _op_effect_request(self, caller: str, effect: dict) -> dict:
        self._check_plugin_effect(caller, effect)
        approval_id, digest = self._gate.request(effect)
        return {"approval_id": approval_id, "digest": digest}

    def _op_effect_authorize(self, caller: str, effect: dict, approval_id: object,
                             authority: object) -> dict:
        started = self.commit_fence.position()  # before any check: from here the call is in flight
        permission, run = self._check_plugin_effect(caller, effect)
        if type(approval_id) is not str:
            raise Refused("approval_required")
        digest = self._gate.take(approval_id, effect)
        commit = self._commit_section(caller, effect, digest, started)
        if permission not in self._authority_required:
            with commit:
                pass  # nothing is charged: the end of this section is the commit; the answer follows
            return {"authorized": True, "digest": digest, "charge": None}
        if self._authority is None:
            raise Refused("authority_unconfigured")
        charge = self._authority.charge(
            authority, plugin_id=caller, artifact_sha256=self._loader.artifact(caller),
            scope=effect["scope"], kind=effect["kind"], provider=run["provider"],
            model_version=run["model_version"], currency=effect["currency"], amount_minor=effect["amount_minor"],
            idempotency_key=approval_id, commit_guard=commit)
        return {"authorized": True, "digest": digest, "charge": charge}

    @contextlib.contextmanager
    def _commit_section(self, caller: str, effect: dict, digest: str, started: int) -> Iterator[None]:
        """Hold the fence, check every halt again, then let the caller commit inside."""
        with self.commit_fence:
            self._check_plugin_effect(caller, effect)  # poison, permission, run fault, trip now
            if self.commit_fence.trip_changed_since(effect["connection"], started):
                raise Refused("connection_tripped")  # tripped (or reset) while in flight
            if self._gate.is_vetoed(digest):
                raise Refused("approval_vetoed")
            yield

    def _acknowledge_halt(self, change: Callable[[], object]) -> object:
        """Change state, then drain the fence before answering, whether or not the change raised."""
        try:
            return change()
        finally:
            self.commit_fence.drain()

    def _op_approval_decide(self, caller: str, approval_id: object, digest: object,
                            credential: object, decision: object) -> dict:
        if decision not in ("approve", "veto"):
            raise Refused("decision_invalid")
        if type(approval_id) is not str or type(digest) is not str:
            raise Refused("approval_unknown")
        if decision == "approve":
            return {"state": self._gate.decide(approval_id, digest, credential, True)}
        return {"state": self._acknowledge_halt(lambda: self._gate.decide(approval_id, digest, credential, False))}

    def _require_host(self, caller: str) -> None:
        # The host name is reserved at manifest parse, so no loaded plugin carries it;
        # refuse anyway if one ever did.
        if caller != HOST or self._loader.artifact(HOST) is not None:
            raise Refused("host_only")

    def _op_run_bind(self, caller: str, run_id: str, connection: str, plugin_id: str,
                     provider: str, model_version: str) -> dict:
        self._require_host(caller)
        self._breaker.bind_run(run_id, connection, plugin_id, provider, model_version)
        return {}

    def _op_run_fault(self, caller: str, run_id: str) -> dict:
        self._require_host(caller)
        self._acknowledge_halt(lambda: self._breaker.fault_run(run_id))
        return {}

    def _op_key_revoke(self, caller: str, key_id: str) -> dict:
        self._require_host(caller)
        order_off = self._acknowledge_halt(lambda: self._loader.revoke_key(key_id))
        generation = self._loader.revocation_generation(key_id)
        if type(generation) is not int:
            raise Refused("revocation_not_durable", key_id)
        return {"order_off": order_off, "revocation_generation": generation}

    def _op_breaker_report(self, caller: str, connection: str, event_type: str) -> dict:
        self._require_host(caller)

        # An event that does not trip (the core's own timeouts and deadlines) changes nothing, so it
        # neither marks nor waits for a commit section. One that trips is acknowledged as a halt.
        tripped = self._breaker.report(connection, event_type)
        if tripped:
            self.commit_fence.mark_trip_change(connection)
            self.commit_fence.drain()
        return {"tripped": tripped}

    def _op_breaker_probe_begin(self, caller: str, connection: str) -> dict:
        self._require_host(caller)
        return {"lease": self._breaker.begin_probe(connection)}

    def _op_breaker_probe_reset(self, caller: str, connection: str, lease: object,
                                probe_ok: object) -> dict:
        self._require_host(caller)
        reset = self._breaker.reset_after_probe(connection, lease, probe_ok is True)
        if reset:
            # A reset is a change of the trip state too: an authorization in flight across it,
            # such as one racing the trip's own acknowledgement, is refused, never revived.
            self.commit_fence.mark_trip_change(connection)
        return {"reset": reset}
