"""The core's language-neutral boundary (``csl-core/v0``): canonical JSON bytes in and out.

Plugins and consumers (including in-process plugins) reach the approval gate, the authority ledger
and the breaker only through ``CoreService.client_for(caller)``, a callable taking
request bytes and returning response bytes. The ops, their arguments and the order of
checks are listed in ``protocol.json`` beside this file, so a later Rust core can be
driven by the same test vectors (Lucas, Nov 7: keep the core's boundaries
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
of the digest again, and only then commits (the ledger's charge for an order; the answer
itself for an effect that is not charged). A refusal there rolls the charge back, and the
approval stays used. The host ops that acknowledge a halt (``run.fault``,
``breaker.report``, ``key.revoke`` and ``approval.decide`` with ``veto``) change their
state, then drain the fence before they answer, so a commit section that read the old
state has committed by the time they answer and every later one reads the new state.

Lock order: the ledger's lock, then the fence, then the gate's and the breaker store's
locks, which the commit section reads under. A halt changes state under its own lock and
drains with no lock of this package held, so it never holds the fence while it waits for
a lock (``key.revoke`` takes the loader's lock, and the loader's lock is held when
``CoreHealth.poison`` runs). What the fence does not reach yet: ``CoreHealth.poison``,
the daemon's own fault and trip writes (``daemon/breaker.py``) and other direct writes to
the breaker store do not drain it, so for them only the recheck holds: a poison, fault or
trip that lands after the commit section's recheck and before its commit does not refuse
that one commit. Those are requests to their keeper; ``commit_fence`` is injectable so
that one fence can be shared with them.
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
    is done, and every later commit section reads the new state. Not reentrant: a commit
    section never drains, and nothing drains while holding a lock that a commit section takes.
    """

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self) -> "CommitFence":
        self._lock.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._lock.release()

    def drain(self) -> None:
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
        self.commit_fence = commit_fence if commit_fence is not None else CommitFence()

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
        permission, run = self._check_plugin_effect(caller, effect)
        if type(approval_id) is not str:
            raise Refused("approval_required")
        digest = self._gate.take(approval_id, effect)
        commit = self._commit_section(caller, effect, digest)
        if permission not in self._authority_required:
            with commit:
                pass  # nothing is charged: the answer is the commit
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
    def _commit_section(self, caller: str, effect: dict, digest: str) -> Iterator[None]:
        """Hold the fence, check every halt again, then let the caller commit inside."""
        with self.commit_fence:
            self._check_plugin_effect(caller, effect)  # poison, permission, run fault, trip
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
        return {"tripped": self._acknowledge_halt(lambda: self._breaker.report(connection, event_type))}

    def _op_breaker_probe_begin(self, caller: str, connection: str) -> dict:
        self._require_host(caller)
        return {"lease": self._breaker.begin_probe(connection)}

    def _op_breaker_probe_reset(self, caller: str, connection: str, lease: object,
                                probe_ok: object) -> dict:
        self._require_host(caller)
        return {"reset": self._breaker.reset_after_probe(connection, lease, probe_ok is True)}
