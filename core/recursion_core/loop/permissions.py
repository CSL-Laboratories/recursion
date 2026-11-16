"""Permission requests: a tool that changes something asks the core first, and waits.

The loop writes a ``permission_request`` line naming its own request id (the ``engine_request_id`` of
approval.v1's private binding), the tool, the directory the call runs in, and the input exactly as the model
gave it. The core mints the approval id, draws the request, and answers on standard input:

    {"kind": "approval_answer", "approval_id": "apr-...", "decision": "approve" | "deny",
     "request_id": "perm-...", "input": {...}}          request_id required; input optional

``input``, when present, is the bound input (approval.v1: "the driver sends back the bound input"). The loop runs
only what it asked for: a bound input that is not equal to the pending one (numbers compare by value, so 1.0
and 1 are equal) is refused and the call is denied.

No answer by the deadline means denied (approval.v1: "expired ... means denied"); so does an interrupt. The
deadline is a moment, fixed when the request is written (a monotonic clock), and it is kept where the answer is
taken: an answer that arrives after it is refused and the call stays denied, whether or not the waiting turn has
woken yet. An interrupt is final the same way: once the turn is cancelled, a later approval does not revive the
call (the registry also checks the turn again after an approval, before anything runs).

What is asked for is an independent copy of the call's input, taken when the request is made: the request line
shows that copy, a later change to the caller's objects (a nested list included) does not reach it, and an
approval without a bound input runs a fresh copy of it.

When the tool's precheck names facts the model's input does not show (the file a path leads to through a link),
they are written on the request beside the input (``resolved_path``). Only the names in ``REQUEST_FACTS`` are
written, and only as JSON values: any other name (one that would collide with a field of the request, such as
``input`` or ``tool``) or a value that is not JSON is refused before anything is asked (``check_facts``). A
call larger than approval.v1's provisional size limit (262,144 bytes of tool, directory and input) is denied
without asking, because the core would refuse to draw it; the model is told to send it in smaller parts.

A run started with the full-access profile, acknowledged for that run, skips the wait (grill Q20); every
call it lets through is still written as a ``permission_request`` line with ``"profile": "full_access"``, so
the record shows what ran without asking.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from recursion_core.loop.providers import base as b

DEFAULT_DEADLINE_S = 900.0  # approval.v1's examples expire after fifteen minutes
MAX_EFFECT_BYTES = 262_144  # approval.v1, PROVISIONAL
ASK, FULL_ACCESS = "ask", "full_access"
REQUEST_ID_MAX = 256  # approval.v1 binding: engine_request_id
REQUEST_FACTS = frozenset({"resolved_path"})  # the facts a request may show beside the input

APPROVED, DENIED, EXPIRED, TOO_LARGE, MISMATCH, CANCELLED = (
    "approved", "denied", "expired", "too_large", "bound_input_differs", "cancelled")


@dataclass(frozen=True)
class Decision:
    outcome: str
    input: Mapping[str, Any] | None = None
    approval_id: str | None = None

    @property
    def approved(self) -> bool:
        return self.outcome == APPROVED


def same_input(a: Any, b_: Any) -> bool:
    """Equal as JSON values: numbers by value (1.0 and 1, -0.0 and 0), but a boolean is never a number, and
    objects need the same keys."""

    if isinstance(a, bool) or isinstance(b_, bool):
        return isinstance(a, bool) and isinstance(b_, bool) and a == b_
    if isinstance(a, (int, float)) and isinstance(b_, (int, float)):
        return a == b_
    if isinstance(a, str) or isinstance(b_, str):
        return isinstance(a, str) and isinstance(b_, str) and a == b_
    if a is None or b_ is None:
        return a is None and b_ is None
    if isinstance(a, list) and isinstance(b_, list):
        return len(a) == len(b_) and all(same_input(x, y) for x, y in zip(a, b_))
    if isinstance(a, Mapping) and isinstance(b_, Mapping):
        return set(a) == set(b_) and all(same_input(a[k], b_[k]) for k in a)
    return False


def snapshot(value: Any) -> Any:
    """An independent copy of a JSON value: objects and arrays are copied all the way down, so nothing is shared
    with the caller. Other kinds are kept as they are (the schema check refuses what is not JSON)."""

    if isinstance(value, dict):
        return {k: snapshot(v) for k, v in value.items()}
    if isinstance(value, list):
        return [snapshot(v) for v in value]
    if isinstance(value, tuple):
        return tuple(snapshot(v) for v in value)
    return value


def check_facts(facts: Any) -> dict[str, Any] | None:
    """The facts a precheck named, as a request may show them: None when there are none (or the precheck returned
    something that is not a mapping, which is ignored), else an independent copy. Raises ValueError for a name
    outside ``REQUEST_FACTS`` or a value that is not JSON (DeepSeek N2): the call is then refused, never asked."""

    if not isinstance(facts, Mapping) or not facts:
        return None
    checked = {}
    for name, value in facts.items():
        if name not in REQUEST_FACTS:
            raise ValueError(f"a permission request cannot show a fact named {name!r}")
        value = snapshot(value)
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError(f"the fact {name!r} is not a JSON value") from None
        checked[name] = value
    return checked


@dataclass
class _Pending:
    request_id: str
    input: Mapping[str, Any]
    expires: float  # on the gate's clock
    done: threading.Event = field(default_factory=threading.Event)
    decision: Decision | None = None


class PermissionGate:
    def __init__(self, writer, *, profile: str = ASK, deadline_s: float = DEFAULT_DEADLINE_S,
                 clock=time.monotonic) -> None:
        if profile not in (ASK, FULL_ACCESS):
            raise ValueError("permission profile")
        self._writer = writer
        self.profile = profile
        self.deadline_s = deadline_s
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._next = 1

    def _mint(self) -> str:
        with self._lock:
            n = self._next
            self._next += 1
        return f"perm-{n}"

    def ask(self, turn_id: str, call: b.ToolCall, cwd: str, cancel: b.CancelToken,
            facts: Mapping[str, Any] | None = None) -> Decision:
        """Ask for one call and wait. Called on the turn's thread. ``facts`` (from the tool's precheck) are
        written on the request beside the input; facts ``check_facts`` refuses raise ValueError before anything is
        asked."""

        args = snapshot(dict(call.input or {}))  # this copy is the request; nothing outside holds it
        size = len(json.dumps({"tool": call.name, "cwd": cwd, "input": args}, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
        if size > MAX_EFFECT_BYTES:
            return Decision(TOO_LARGE)
        if cancel.cancelled:
            return Decision(CANCELLED)  # nothing is asked for a turn that has been interrupted
        extra = check_facts(facts) or {}
        request_id = self._mint()
        if self.profile == FULL_ACCESS:
            self._writer.write("permission_request", turn_id=turn_id, request_id=request_id, call_id=call.call_id,
                               tool=call.name, cwd=cwd, input=snapshot(args), profile=FULL_ACCESS, **extra)
            return Decision(APPROVED, snapshot(args))
        pending = _Pending(request_id, args, self._clock() + self.deadline_s)
        with self._lock:
            self._pending[request_id] = pending

        def on_cancel() -> None:
            # Final: an answer that comes after this finds the request decided, and is refused.
            with self._lock:
                if pending.decision is None:
                    pending.decision = Decision(CANCELLED)
            pending.done.set()

        unregister = cancel.on_cancel(on_cancel)
        try:
            with self._lock:
                # Cancelled after the check above, before the callback was in place: the callback has already
                # decided it, and nothing is asked (astra C5, DeepSeek N3).
                decided = pending.decision is not None
            if not decided:
                self._writer.write("permission_request", turn_id=turn_id, request_id=request_id,
                                   call_id=call.call_id, tool=call.name, cwd=cwd, input=snapshot(args), profile=ASK,
                                   deadline_s=self.deadline_s, **extra)
            while True:
                with self._lock:
                    self._expire(pending)
                    if pending.decision is not None:
                        break
                    remaining = pending.expires - self._clock()
                pending.done.wait(remaining)
        finally:
            unregister()
            with self._lock:
                self._pending.pop(request_id, None)
        if cancel.cancelled:
            return Decision(CANCELLED)  # an interrupt is final, whatever came before it
        decision = pending.decision
        if decision.outcome == EXPIRED:
            self._writer.write("permission_expired", turn_id=turn_id, request_id=request_id)
        return decision

    def _expire(self, pending: _Pending) -> None:
        """Under the lock: an undecided request whose moment has passed is decided, denied, for good."""

        if pending.decision is None and self._clock() >= pending.expires:
            pending.decision = Decision(EXPIRED)
            pending.done.set()

    def answer(self, request_id: str, approval_id: str, decision: str, bound: Mapping[str, Any] | None) -> str | None:
        """Deliver an answer from the input thread. Returns an error code, or None when it was taken."""

        with self._lock:
            pending = self._pending.get(request_id)
            if pending is not None:
                self._expire(pending)  # the deadline holds here too, before the waiting turn has woken
            if pending is None or pending.decision is not None:
                return "no_pending_request"
            if decision == "approve":
                if bound is not None and not same_input(bound, pending.input):
                    pending.decision = Decision(MISMATCH, approval_id=approval_id)
                else:
                    pending.decision = Decision(APPROVED, snapshot(dict(bound)) if bound is not None
                                                else snapshot(pending.input), approval_id)
            else:
                pending.decision = Decision(DENIED, approval_id=approval_id)
            pending.done.set()
        return None

    def waiting(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._pending)


REFUSALS = {
    DENIED: "denied: the user did not approve this call",
    EXPIRED: "denied: nobody answered the permission request before its deadline",
    TOO_LARGE: (f"denied without asking: the call is larger than the {MAX_EFFECT_BYTES} bytes a permission request "
                "can show; make the change in smaller parts"),
    MISMATCH: "denied: the approved input differs from the call that was asked for",
    CANCELLED: "not run: the turn was interrupted while the call waited for permission",
}
