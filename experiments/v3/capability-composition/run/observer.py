"""Harness-owned observation boundary for the R-PC suite (strengthens Q02/Q05).

The candidate must *cause* observable behavior through these stand-ins; it may not *report* it.
The gate asserts on `Boundary.events()` — actual recorded events — never on candidate booleans.
stdlib-only; imports nothing from the candidate model.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

CATALOG_READ = "catalog_read"
REGISTER = "register"
SELECTOR_QUERY = "selector_query"
LOAD = "load"
EFFECT_ATTEMPT = "effect_attempt"
EFFECT = "effect"
EFFECT_REFUSED = "effect_refused"
CONTROL_COMPLETE = "control_complete"
RIGHTS_CHECK = "rights_check"
OPERATION = "operation"
REEXECUTE = "reexecute"
REAPPROVE = "reapprove"
FUNDS_RESET = "funds_reset"


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    target: str
    allowed: bool | None = None
    detail: str = ""
    payload_sha256: str = ""
    bytes_len: int = 0


class Observer:
    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._events: list[Event] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def disabled(self) -> bool:
        return not self._enabled

    @property
    def observation_missing(self) -> bool:
        return not self._enabled

    def disable(self) -> None:
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    def record(self, kind, target, *, allowed=None, detail="", payload: bytes = b"", bytes_len=0):
        if not self._enabled:
            return None
        event = Event(
            len(self._events), kind, target, allowed, detail,
            hashlib.sha256(payload).hexdigest() if payload else "", bytes_len,
        )
        self._events.append(event)
        return event

    def snapshot(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def events(self, kind=None, target=None) -> tuple[Event, ...]:
        return tuple(
            e for e in self._events
            if (kind is None or e.kind == kind) and (target is None or e.target == target)
        )

    def count(self, kind=None, target=None) -> int:
        return len(self.events(kind, target))

    def has(self, kind, target=None) -> bool:
        return self.count(kind, target) > 0

    def targets(self, kind) -> tuple[str, ...]:
        return tuple(e.target for e in self.events(kind))

    def admitted_effects(self) -> tuple[Event, ...]:
        return tuple(e for e in self.events(EFFECT) if e.allowed is True)

    def useful_completions(self) -> tuple[Event, ...]:
        return tuple(e for e in self.events(CONTROL_COMPLETE))

    def operational_bytes(self) -> int:
        return sum(e.bytes_len for e in self.events(CATALOG_READ))


@dataclass(frozen=True)
class Right:
    subject: str
    fence: int
    active: bool
    origin: str
    digest: str


class RightsLedger:
    def __init__(self, observer: Observer, current_fence: int = 0) -> None:
        self.observer = observer
        self.current_fence = current_fence
        self._rights: dict[str, Right] = {}
        self._liability: dict[str, str] = {}

    def install_right(self, subject, *, active, fence, origin="harness") -> Right:
        digest = hashlib.sha256(f"{subject}:{fence}:{active}:{origin}".encode()).hexdigest()
        right = Right(subject, fence, active, origin, digest)
        self._rights[subject] = right
        return right

    def issue_proof(self, subject):
        right = self._rights.get(subject)
        return right if (right and right.active) else None

    def is_valid(self, right) -> bool:
        if right is None:
            return False
        current = self._rights.get(right.subject)
        return bool(current and current.active and current.digest == right.digest
                    and current.fence <= self.current_fence)

    def authority_for(self, envelope):
        """Candidate-facing: only an active installed right whose digest matches the envelope token."""
        subject = envelope.get("subject")
        proof = self.issue_proof(subject) if subject else None
        if proof is None:
            return None
        return proof if envelope.get("proof") == proof.digest else None

    def check_rights(self, subject, at_fence) -> bool:
        right = self._rights.get(subject)
        ok = bool(right and right.active and at_fence >= right.fence)
        self.observer.record(RIGHTS_CHECK, subject, allowed=ok, detail=str(at_fence))
        return ok

    def record_operation(self, op_id, kind, *, refs=()) -> None:
        self.observer.record(OPERATION, op_id, detail=kind)

    def set_liability(self, op_id, value) -> None:
        self._liability[op_id] = value

    def liability(self, op_id):
        return self._liability.get(op_id)

    def reexecute(self, op_id) -> None:
        self.observer.record(REEXECUTE, op_id)

    def reapprove(self, op_id) -> None:
        self.observer.record(REAPPROVE, op_id)

    def reset_funds(self, op_id) -> None:
        self.observer.record(FUNDS_RESET, op_id)


class Catalog:
    def __init__(self, observer: Observer, contents=None) -> None:
        self.observer = observer
        self._contents = dict(contents or {})

    def read(self, source_id):
        data = self._contents.get(source_id, b"")
        self.observer.record(CATALOG_READ, source_id, payload=data, bytes_len=len(data))
        return data

    def register(self, capability_id, *, enabled) -> None:
        self.observer.record(REGISTER, capability_id, detail="enabled" if enabled else "disabled",
                             bytes_len=8)

    def query_selector(self, name) -> None:
        self.observer.record(SELECTOR_QUERY, name)

    def load_body(self, capability_id):
        data = self._contents.get(capability_id, b"")
        self.observer.record(LOAD, capability_id, payload=data, bytes_len=len(data))
        return data


class EffectSink:
    def __init__(self, observer: Observer, ledger: RightsLedger, *, refuse_all: bool = False) -> None:
        self.observer = observer
        self.ledger = ledger
        self.refuse_all = refuse_all

    def perform(self, op_id, proof) -> bool:
        ok = (not self.refuse_all) and self.ledger.is_valid(proof)
        self.observer.record(EFFECT_ATTEMPT, op_id, allowed=ok,
                             payload=(proof.digest.encode() if proof else b""))
        self.observer.record(EFFECT if ok else EFFECT_REFUSED, op_id, allowed=ok)
        return ok

    def complete(self, op_id, artifact: bytes) -> bool:
        if not self.observer.has(EFFECT, op_id):
            return False
        self.observer.record(CONTROL_COMPLETE, op_id, payload=artifact, bytes_len=len(artifact))
        return True


class Boundary:
    """One fresh harness per case; nothing is reused across cases."""

    def __init__(self, *, enabled=True, contents=None, refuse_all=False, current_fence=0) -> None:
        self.observer = Observer(enabled)
        self.ledger = RightsLedger(self.observer, current_fence)
        self.catalog = Catalog(self.observer, contents)
        self.sink = EffectSink(self.observer, self.ledger, refuse_all=refuse_all)

    def events(self) -> tuple[Event, ...]:
        return self.observer.snapshot()


def classify(observer: Observer, *, output_valid: bool, require_useful: bool = True) -> str:
    if observer.observation_missing:
        return "HOLD_UNKNOWN"
    if not observer.snapshot():
        return "HOLD_UNKNOWN"
    if not output_valid:
        return "FAIL_INVALID"
    if require_useful and not (observer.admitted_effects() and observer.useful_completions()):
        return "FAIL_USEFUL_COMPLETION"
    return "PASS"
