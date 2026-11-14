"""Driver contract version 2 (driver.v2): what an engine driver declares, takes and gives back.

The shapes are lane SPINE's ``schemas/control/v1/driver.v2.json`` (draft 1.0-draft.4 at #368's head
c77aa2e0) and the event payloads of ``event.v1.json``. This module is the core's code for them: every
engine lane's driver subclasses ``Driver`` and gets the same gate. ``RouteAdapter.run`` stays the one-turn
path that exists today; its declaration is ``one_turn(engine)``, every flag false.

A DRIVER DECLARES its engine, one true-or-false flag for each thing the engine can do (``FLAGS``), and
``input_ack``: whether the engine gives a typed acceptance of an input sent into a turn in progress. That
records a fact only; whether steering is offered on an engine without it is not decided here (the
Liaison's ruling of 3:42 AM Nov 13, not a decision of Lucas's).

THE CALLS, each checked by ``Driver`` before the subclass is asked, so a refused call writes no byte,
starts no process and changes no state:

    start(request, env) -> Handle         request is driver.v2's start record; env is never recorded
    resume(request, env) -> Handle        start with engine_session_id set: needs ``resume``
    send(handle, input)                   user_message | approval_answer | close
    interrupt(handle, turn_id)            needs ``interrupt``; only the turn in progress
    fork(engine_session_id, at) -> id     needs ``fork``
    parse(handle, line) -> [draft]        no flag; every draft is checked before it is returned

Which flag a call needs (driver.v2's description): a second user_message to the same worker needs
``multi_turn``; a user_message while a turn is in progress needs ``mid_turn_input``; an approval_answer
needs ``permission_bridge``. A call whose flag is false raises ``DriverRefused`` (code ``unsupported``, the
call and the flag). A malformed call raises ``DriverInputError``; a call the worker's state does not allow
(closed, an interrupt of a turn that is not in progress, a reused turn or delivery id) raises
``DriverStateError``. All three are raised before the subclass is asked.

DELIVERY. A user_message carries the delivery_id the core minted for it. ``parse`` returns an
``input.accepted`` draft only when it names a delivery this handle wrote and has not seen accepted, with
the same turn and the same ``how`` (``new_turn`` or ``mid_turn``) the write had: the driver correlates
acceptance with its own pending writes, never with an id read out of engine output. A draft that matches
nothing is dropped and counted (``Handle.ignored_acceptances``). The subclass makes an acceptance draft
only from the engine's own typed sign that it took the input; the base cannot see where a draft came from,
only that it matches a write. A write that raised leaves its delivery pending: the outcome is unknown, its
id is never reused, and a later typed acceptance of it still counts.

TURNS. A user_message that begins a turn makes it the turn in progress from the moment it is reserved,
before the write, so a second message sent meanwhile is a mid-turn input. The subclass calls
``turn_ended(handle, turn_id)`` when the engine's end-of-turn sign arrives.

Who owns a live handle between turns, how Stop and shutdown see it, and when it is released are this
lane's to define with lane STOP, in ``routes/workers.py`` (a later slice). Nothing here starts a thread or
holds a lock while calling the subclass.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, fields

CONTRACT = 2
FLAGS = ("multi_turn", "mid_turn_input", "interrupt", "resume", "fork", "permission_bridge", "typed_limits", "usage")
INPUT_KINDS = ("user_message", "approval_answer", "close")
UNSUPPORTED = "unsupported"
# The calls a flag can refuse, and the flags that refuse a call (driver.v2 $defs/refusal).
REFUSABLE_CALLS = ("send", "interrupt", "resume", "fork")
REFUSING_FLAGS = ("multi_turn", "mid_turn_input", "interrupt", "resume", "fork", "permission_bridge")

# common.v1's patterns. fullmatch, so a trailing newline is never accepted.
_ENGINE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_RUN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LOG = re.compile(r"ses-[A-Za-z0-9_-]{1,60}|run:[A-Za-z0-9_-]{1,64}")
_TURN = re.compile(r"turn-[A-Za-z0-9_-]{1,59}")
_DELIVERY = re.compile(r"dlv-[A-Za-z0-9_-]{1,60}")
_APPROVAL = re.compile(r"apr-[A-Za-z0-9_-]{1,60}")

# The event types a driver may draft, and the keys of each payload (event.v1; every key is required and no
# other is allowed). A driver never drafts an approval request, turn.started or turn.ended: the core writes
# those. The conformance fixtures check the payloads in full; this checks what every driver shares.
DRAFT_KEYS = {
    "engine.output": frozenset({"stream", "text", "offset_start", "offset_end"}),
    "tool.call": frozenset({"call_id", "name", "input_digest", "input"}),
    "tool.result": frozenset({"call_id", "status", "text", "output_ref", "truncated"}),
    "usage": frozenset({"connection_id", "model", "bucket", "span", "input_tokens", "output_tokens",
                        "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd"}),
    "limit": frozenset({"connection_id", "scope", "kind", "model", "bucket", "reset_at"}),
    "result": frozenset({"code", "detail"}),
    "input.accepted": frozenset({"delivery_id", "turn_id", "how"}),
}
DRAFT_TYPES = tuple(DRAFT_KEYS)
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens")


class DriverRefused(Exception):
    """A call whose flag is false. Nothing happened before it was raised."""

    code = UNSUPPORTED

    def __init__(self, call: str, flag: str) -> None:
        if call not in REFUSABLE_CALLS or flag not in REFUSING_FLAGS:
            raise ValueError((call, flag))
        super().__init__(f"{UNSUPPORTED}: {call} needs {flag}")
        self.call, self.flag = call, flag

    def as_record(self) -> dict:
        """driver.v2's refusal record."""
        return {"code": UNSUPPORTED, "call": self.call, "flag": self.flag}


class DriverInputError(ValueError):
    """A call whose arguments do not have driver.v2's shape. ``field`` names the first one that does not."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class DriverStateError(RuntimeError):
    """A call the worker's state does not allow. Nothing was written."""


@dataclass(frozen=True)
class Flags:
    multi_turn: bool = False
    mid_turn_input: bool = False
    interrupt: bool = False
    resume: bool = False
    fork: bool = False
    permission_bridge: bool = False
    typed_limits: bool = False
    usage: bool = False

    def __post_init__(self) -> None:
        for f in fields(self):
            if type(getattr(self, f.name)) is not bool:
                raise DriverInputError(f"flags.{f.name}")
        if self.mid_turn_input and not self.multi_turn:
            # Input in the middle of a turn needs a worker that keeps its input open.
            raise DriverInputError("flags.mid_turn_input")

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in FLAGS}


@dataclass(frozen=True)
class Declaration:
    """driver.v2's root record: the engine, the flags, and whether the engine acknowledges mid-turn input."""

    engine: str
    flags: Flags
    input_ack: bool

    def __post_init__(self) -> None:
        if not isinstance(self.engine, str) or not _ENGINE.fullmatch(self.engine):
            raise DriverInputError("engine")
        if not isinstance(self.flags, Flags):
            raise DriverInputError("flags")
        if type(self.input_ack) is not bool:
            raise DriverInputError("input_ack")

    def as_record(self) -> dict:
        return {"engine": self.engine, "driver": CONTRACT, "flags": self.flags.as_dict(), "input_ack": self.input_ack}

    @classmethod
    def from_record(cls, record: object) -> Declaration:
        if not isinstance(record, Mapping) or set(record) != {"engine", "driver", "flags", "input_ack"}:
            raise DriverInputError("declaration")
        if record["driver"] != CONTRACT or type(record["driver"]) is not int:
            raise DriverInputError("driver")
        flags = record["flags"]
        if not isinstance(flags, Mapping) or set(flags) != set(FLAGS):
            raise DriverInputError("flags")
        return cls(record["engine"], Flags(**flags), record["input_ack"])


def one_turn(engine: str) -> Declaration:
    """Today's path (``RouteAdapter.run``: one prompt, standard input closed, read until the result)."""
    return Declaration(engine, Flags(), input_ack=False)


class Handle:
    """What start and resume give back: the run the worker lives in and the engine's conversation id.

    It also carries the worker's state as the gate sees it. Only ``Driver`` changes that state, under the
    handle's own lock, and never while the subclass is being called.
    """

    def __init__(self, run_id: str, engine_session_id: str | None) -> None:
        self.run_id = run_id
        self._engine_session_id = engine_session_id
        self.ignored_acceptances = 0
        self._lock = threading.Lock()
        self._live_turn: str | None = None
        self._turns: list[str] = []          # every turn a user_message began, in order
        self._deliveries: set[str] = set()   # every delivery id written, accepted or not
        self._pending: dict[str, tuple[str, str]] = {}  # delivery id -> (turn id, how), until accepted
        self._closed = False

    @property
    def engine_session_id(self) -> str | None:
        return self._engine_session_id

    def set_engine_session_id(self, value: str) -> None:
        """The engine reported its conversation id. Once set it does not change."""
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise DriverInputError("engine_session_id")
        with self._lock:
            if self._engine_session_id not in (None, value):
                raise DriverStateError("the engine reported a second conversation id")
            self._engine_session_id = value

    @property
    def live_turn(self) -> str | None:
        return self._live_turn

    @property
    def closed(self) -> bool:
        return self._closed

    def as_record(self) -> dict:
        """driver.v2's handle record."""
        return {"run_id": self.run_id, "engine_session_id": self._engine_session_id}


class Driver:
    """The gate every engine driver shares. Subclasses set ``declaration`` and implement the ``_`` hooks."""

    declaration: Declaration

    # -- the calls ------------------------------------------------------------------------------------
    def start(self, request: object, env: object) -> Handle:
        req = check_start(request)
        if req["engine_session_id"] is not None:
            self._need("resume", "resume")
        child_env = check_env(env)
        handle = Handle(req["run_id"], req["engine_session_id"])
        self._start(handle, req, child_env)
        return handle

    def resume(self, request: object, env: object) -> Handle:
        self._need("resume", "resume")
        req = check_start(request)
        if req["engine_session_id"] is None:
            raise DriverInputError("engine_session_id")
        return self.start(req, env)

    def send(self, handle: Handle, item: object) -> None:
        item = check_input(item)
        kind = item["kind"]
        with handle._lock:
            if handle._closed:
                raise DriverStateError("the worker's input is closed")
            if kind == "approval_answer":
                self._need("send", "permission_bridge")
            elif kind == "close":
                handle._closed = True  # before the write: nothing is sent after a close
            else:
                self._reserve(handle, item)
        self._send(handle, item)

    def interrupt(self, handle: Handle, turn_id: object) -> None:
        self._need("interrupt", "interrupt")
        if not isinstance(turn_id, str) or not _TURN.fullmatch(turn_id):
            raise DriverInputError("turn_id")
        with handle._lock:
            if handle._live_turn != turn_id:
                raise DriverStateError(f"{turn_id} is not the turn in progress")
        self._interrupt(handle, turn_id)

    def fork(self, engine_session_id: object, at: object) -> str:
        self._need("fork", "fork")
        if not isinstance(engine_session_id, str) or not 1 <= len(engine_session_id) <= 256:
            raise DriverInputError("engine_session_id")
        if not isinstance(at, str) or not _TURN.fullmatch(at):
            raise DriverInputError("at")
        new = self._fork(engine_session_id, at)
        if not isinstance(new, str) or not 1 <= len(new) <= 256:
            raise DriverStateError("fork gave back no conversation id")
        return new

    def parse(self, handle: Handle, line: str) -> list[dict]:
        drafts = [check_draft(d) for d in self._parse(handle, line)]
        out = []
        with handle._lock:
            for draft in drafts:
                if draft["type"] == "input.accepted":
                    p = draft["payload"]
                    if handle._pending.get(p["delivery_id"]) != (p["turn_id"], p["how"]):
                        handle.ignored_acceptances += 1  # names no write of ours, or not as written
                        continue
                    del handle._pending[p["delivery_id"]]
                out.append(draft)
        return out

    def turn_ended(self, handle: Handle, turn_id: str) -> None:
        """Called by the subclass when the engine's end-of-turn sign for ``turn_id`` arrives."""
        with handle._lock:
            if handle._live_turn != turn_id:
                raise DriverStateError(f"{turn_id} is not the turn in progress")
            handle._live_turn = None

    # -- the gate ------------------------------------------------------------------------------------
    def _need(self, call: str, flag: str) -> None:
        if not getattr(self.declaration.flags, flag):
            raise DriverRefused(call, flag)

    def _reserve(self, handle: Handle, item: dict) -> None:
        """Check a user_message against the worker's state and reserve it. Caller holds the handle's lock."""
        turn, delivery = item["turn_id"], item["delivery_id"]
        if delivery in handle._deliveries:
            raise DriverStateError(f"{delivery} was already written")
        if handle._live_turn is not None:
            self._need("send", "mid_turn_input")
            if turn != handle._live_turn:
                raise DriverStateError(f"{turn} is not the turn in progress")
            how = "mid_turn"
        else:
            if handle._turns:
                self._need("send", "multi_turn")
            if turn in handle._turns:
                raise DriverStateError(f"{turn} already ended")
            handle._turns.append(turn)
            handle._live_turn = turn
            how = "new_turn"
        handle._deliveries.add(delivery)
        handle._pending[delivery] = (turn, how)

    # -- what a subclass implements -------------------------------------------------------------------
    def _start(self, handle: Handle, request: dict, env: dict[str, str]) -> None:
        raise NotImplementedError

    def _send(self, handle: Handle, item: dict) -> None:
        raise NotImplementedError

    def _interrupt(self, handle: Handle, turn_id: str) -> None:
        raise NotImplementedError

    def _fork(self, engine_session_id: str, at: str) -> str:
        raise NotImplementedError

    def _parse(self, handle: Handle, line: str) -> list[dict]:
        raise NotImplementedError


# -- shape checks --------------------------------------------------------------------------------------
def check_start(request: object) -> dict:
    """driver.v2's start record: session_id, run_id, workspace, route, engine_session_id. A copy."""
    keys = {"session_id", "run_id", "workspace", "route", "engine_session_id"}
    if not isinstance(request, Mapping) or set(request) != keys:
        raise DriverInputError("start")
    if not isinstance(request["session_id"], str) or not _LOG.fullmatch(request["session_id"]):
        raise DriverInputError("session_id")
    if not isinstance(request["run_id"], str) or not _RUN.fullmatch(request["run_id"]):
        raise DriverInputError("run_id")
    if request["workspace"] is not None and not isinstance(request["workspace"], Mapping):
        raise DriverInputError("workspace")
    if not isinstance(request["route"], Mapping):
        raise DriverInputError("route")
    esid = request["engine_session_id"]
    if esid is not None and (not isinstance(esid, str) or not 1 <= len(esid) <= 256):
        raise DriverInputError("engine_session_id")
    return dict(request)


def check_env(env: object) -> dict[str, str]:
    """The child environment, built by the child-environment policy: names and values are strings."""
    if not isinstance(env, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise DriverInputError("env")
    return dict(env)


def check_input(item: object) -> dict:
    """What send takes: a user_message, an approval_answer or a close, with exactly driver.v2's keys."""
    if not isinstance(item, Mapping) or item.get("kind") not in INPUT_KINDS:
        raise DriverInputError("kind")
    kind = item["kind"]
    expected = {"user_message": {"kind", "turn_id", "text", "delivery_id"},
                "approval_answer": {"kind", "approval_id", "decision"}, "close": {"kind"}}[kind]
    if set(item) != expected:
        raise DriverInputError(kind)
    if kind == "user_message":
        if not isinstance(item["turn_id"], str) or not _TURN.fullmatch(item["turn_id"]):
            raise DriverInputError("turn_id")
        if not isinstance(item["text"], str) or not item["text"]:
            raise DriverInputError("text")
        if not isinstance(item["delivery_id"], str) or not _DELIVERY.fullmatch(item["delivery_id"]):
            raise DriverInputError("delivery_id")
    elif kind == "approval_answer":
        if not isinstance(item["approval_id"], str) or not _APPROVAL.fullmatch(item["approval_id"]):
            raise DriverInputError("approval_id")
        if item["decision"] not in ("approve", "deny"):
            raise DriverInputError("decision")
    return dict(item)


def check_draft(draft: object) -> dict:
    """One event draft: a type a driver may draft and a payload with exactly that type's keys."""
    if not isinstance(draft, Mapping) or set(draft) != {"type", "payload"} or draft["type"] not in DRAFT_KEYS:
        raise DriverInputError("draft.type")
    payload = draft["payload"]
    if not isinstance(payload, Mapping) or set(payload) != DRAFT_KEYS[draft["type"]]:
        raise DriverInputError(f"draft.{draft['type']}")
    if draft["type"] == "input.accepted":
        if not isinstance(payload["delivery_id"], str) or not _DELIVERY.fullmatch(payload["delivery_id"]):
            raise DriverInputError("draft.input.accepted.delivery_id")
        if not isinstance(payload["turn_id"], str) or not _TURN.fullmatch(payload["turn_id"]):
            raise DriverInputError("draft.input.accepted.turn_id")
        if payload["how"] not in ("new_turn", "mid_turn"):
            raise DriverInputError("draft.input.accepted.how")
    elif draft["type"] == "usage":
        for name in _TOKEN_FIELDS:
            check_measurement(payload[name], integer=True, field=f"draft.usage.{name}")
        check_measurement(payload["cost_usd"], integer=False, field="draft.usage.cost_usd")
    return {"type": draft["type"], "payload": dict(payload)}


def check_measurement(value: object, *, integer: bool, field: str) -> None:
    """event.v1's token_count / dollar_amount: a value is null exactly when nothing is known, never 0 for it."""
    if not isinstance(value, Mapping) or set(value) != {"value", "quality", "provenance"}:
        raise DriverInputError(field)
    number, quality, provenance = value["value"], value["quality"], value["provenance"]
    if quality == "unknown":
        if number is not None or provenance != "unavailable":
            raise DriverInputError(field)
        return
    if quality not in ("known", "lower_bound") or provenance not in ("observed", "modelled", "inferred"):
        raise DriverInputError(field)
    kinds = (int,) if integer else (int, float)
    if type(number) is bool or not isinstance(number, kinds) or number < 0 or not math.isfinite(number):
        raise DriverInputError(field)
