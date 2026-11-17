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

    start(session, workspace, env) -> Handle                 PROVISIONAL form; env is never recorded
    resume(engine_session_id, session, workspace, env) -> Handle   needs ``resume``; PROVISIONAL form
    send(handle, input)                   user_message | approval_answer | close
    interrupt(handle, turn_id)            needs ``interrupt``; only the turn in progress
    fork(engine_session_id, at) -> id     needs ``fork``
    parse(handle, line) -> [draft]        no flag; every draft is checked before it is returned

Which flag a call needs (driver.v2's description): a second user_message to the same worker needs
``multi_turn``; a user_message while a turn is in progress needs ``mid_turn_input``; an approval_answer
needs ``permission_bridge``. A call whose flag is false raises ``DriverRefused``. The refusal record's code
is ``unsupported``, the only code driver.v2 publishes, including a mid-turn send (``DriverBusy``, still a
``DriverRefused``, so a caller can tell that case by type). PROVISIONAL, the start and resume forms above:
which of the brief's wording and driver.v2's stands is open with Lucas (lane question Q7).
PORT WITHDRAW (the Liaison's ruling of Nov 15, 7:07 PM, for #402's round 5, P2; corrected after #432's first reads).
A driver that puts a request to the approvals port first takes a ticket, ``admit_port(handle)``, and hands it to the
port with the request. The port makes the approval visible ONLY through ``ticket.publish(reveal)``: ``reveal`` is the
port's own step that makes the approval visible (the commit of its row, say), and the ticket runs it under the
ticket's lock, only while the ticket is admitted and the worker's port is not sealed. ``withdraw_port(handle)``, which a
``close`` input also runs, seals the worker's port first (from that instant no ticket of it can begin a reveal) and
then ends every ticket admitted and not yet published, waiting only for a reveal that is already running, and for
at most ``wait`` seconds in all. The guarantee, when ``withdraw_port`` RETURNS: every ticket of the worker is
published or withdrawn, every approval that is visible became visible before it returned, and no approval of that
worker becomes visible afterwards. "Delivered" in the ruling's words means accepted by the port, that is revealed:
the driver does not make the port call, so a caller that still holds a ticket can call the port after the withdraw,
and the port refuses it there (``publish`` answers False and the reveal never runs); no new ticket is admitted. If a
reveal runs past the wait, or a step raises, ``withdraw_port`` RAISES instead of returning (``PortWithdrawIncomplete``,
or the interruption itself), the port stays sealed, and a later ``withdraw_port`` finishes the job. The port's side
(refuse a withdrawn request with a typed reason, keep ``reveal`` short) is lane APPROVALS'.

A malformed call raises ``DriverInputError``; a call the worker's state does not allow (closed input, an
interrupt of a turn that is not in progress, a reused turn or delivery id) raises ``DriverStateError``. All
of these are raised before the subclass is asked.

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

ORDER AND LOCKS. A handle has two locks. Its STATE lock guards the gate's bookkeeping and is never held
while the subclass runs, so a reader thread in ``parse`` can always record an acceptance or a turn's end.
Its WRITE lock is held by ``send`` and ``interrupt`` from the state check through the subclass's write, so
a handle's inputs reach the engine in the order the gate reserved them: a message reserved first is
written first, and nothing is written after a close. A subclass therefore never needs an ordering lock of
its own, and must not call ``send`` or ``interrupt`` from inside ``_send`` or ``_interrupt`` (the write
lock is not reentrant). ``parse`` never takes the write lock.

SHAPES. ``start`` checks the whole start record, the nested route and workspace included (common.v1), and
``parse`` checks every draft's payload values and their relations (event.v1), all against lane SPINE's
schemas as pinned at #368 ``c77aa2e0``, in plain code (the core has no schema validator).

Who owns a live handle between turns, how Stop and shutdown see it, and when it is released are this
lane's to define with lane STOP, in ``routes/workers.py`` (a later slice). Nothing here starts a thread or
holds the state lock while calling the subclass.
"""

from __future__ import annotations

import datetime
import hashlib
import math
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields

from recursion_core.control.methods import NoCanonicalText, canonical_text

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
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_CODE_WORD = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CONNECTION_ID = re.compile(r"(subscription|api_key|passthrough):.+", re.S)
_TIMESTAMP = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
                        r"\.[0-9]{3}Z")

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
        return {"code": self.code, "call": self.call, "flag": self.flag}


class DriverBusy(DriverRefused):
    """A user_message sent while a turn is in progress, to a driver without ``mid_turn_input``.

    driver.v2's refusal record has one code, ``unsupported``, and this record uses it, with call
    ``send`` and flag ``mid_turn_input``. The class stays a ``DriverRefused`` so a caller that
    catches refusals sees it, and so this case can still be told by type.
    """

    def __init__(self) -> None:
        super().__init__("send", "mid_turn_input")


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
        self._lock = threading.Lock()         # the state lock: never held while the subclass runs
        self._write_lock = threading.Lock()   # orders this handle's writes; held across check and write
        self._live_turn: str | None = None
        self._turns: list[str] = []          # every turn a user_message began, in order
        self._deliveries: set[str] = set()   # every delivery id written, accepted or not
        self._pending: dict[str, tuple[str, str]] = {}  # delivery id -> (turn id, how), until accepted
        self._closed = False
        self._port_seal = _PortSeal()          # shared with every ticket of this handle (PORT WITHDRAW)
        # Tickets admitted to the port that withdraw_port has not yet seen ended. A published or withdrawn ticket
        # stays here until the next admit_port or withdraw_port drops it; nothing reads it but those two.
        self._tickets: list[PortTicket] = []

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

    @property
    def port_sealed(self) -> bool:
        """True once withdraw_port (or a ``close`` input) began for this handle: no ticket of it can open again."""
        return self._port_seal.sealed

    def as_record(self) -> dict:
        """driver.v2's handle record."""
        return {"run_id": self.run_id, "engine_session_id": self._engine_session_id}


ADMITTED, OPENING, PUBLISHED, WITHDRAWN = "admitted", "opening", "published", "withdrawn"
# How long withdraw_port waits, in all, for reveals that are already running. A reveal is the port's one step that
# makes an approval visible and must be short (no wait on a person, the network, or anything that calls the driver).
REVEAL_WAIT_S = 5.0


class PortWithdrawIncomplete(DriverStateError):
    """withdraw_port could not end every ticket: a reveal ran past the wait. ``tickets`` are the ones not ended.

    The worker's port is sealed all the same, so no other ticket can begin a reveal; a ticket named here may still
    finish the reveal it began. Calling withdraw_port again waits for them again.
    """

    def __init__(self, tickets: list[PortTicket]) -> None:
        super().__init__(f"{len(tickets)} reveal(s) of the approvals port still running after the wait")
        self.tickets = list(tickets)


class _PortSeal:
    """One per handle, shared by its tickets: once ``sealed`` is True no ticket of the handle can begin a reveal."""

    __slots__ = ("sealed",)

    def __init__(self) -> None:
        self.sealed = False


class PortTicket:
    """One request admitted to the approvals port: a latch. It ends once, published or withdrawn.

    - ``publish(reveal)``: the port calls it ONCE, with ``reveal``, its own step that makes the approval visible.
      Under the ticket's lock, and only while the ticket is admitted and its worker's port is not sealed, the ticket
      runs ``reveal()`` and answers True: the approval is visible, and it became so before any withdraw of this ticket
      could return. Otherwise ``reveal`` is never run and the answer is False: the port opens nothing (the request
      was withdrawn, or this ticket was already used). If ``reveal`` raises, the ticket ends withdrawn and the
      exception leaves ``publish``: the port must then have made nothing visible (its commit failed and rolled back).
    - ``withdraw(wait)``: ends the ticket withdrawn if it is still admitted, waiting at most ``wait`` seconds for a
      reveal that is already running (``PortWithdrawIncomplete`` past that). ``Driver.withdraw_port`` does this for
      every ticket of a worker; True means this call withdrew it.

    ``state`` is a snapshot for tests and reports: ``admitted``, ``opening`` (a reveal is running, or was cut off by an
    interruption with its outcome unknown), ``published`` or ``withdrawn`` (an admitted ticket of a sealed worker reads
    withdrawn: it can never open). Any code can make a ticket; only one that ``Driver.admit_port`` gave is known to a
    worker and ended by its withdraw.
    """

    def __init__(self, seal: _PortSeal | None = None) -> None:
        self._lock = threading.Lock()
        self._state = ADMITTED
        self._seal = seal if seal is not None else _PortSeal()
        self._revealer: int | None = None  # the thread running reveal, while it runs

    @property
    def state(self) -> str:
        state = self._state
        return WITHDRAWN if state == ADMITTED and self._seal.sealed else state

    def publish(self, reveal: object) -> bool:
        if not callable(reveal):
            raise DriverInputError("reveal")
        with self._lock:
            if self._state != ADMITTED:
                return False
            if self._seal.sealed:
                self._state = WITHDRAWN
                return False
            self._state = OPENING
            self._revealer = threading.get_ident()
            try:
                reveal()
            except BaseException:
                self._revealer = None
                self._state = WITHDRAWN  # the port made nothing visible (its side of the contract)
                raise
            self._revealer = None
            self._state = PUBLISHED
            return True

    def withdraw(self, wait: float = REVEAL_WAIT_S) -> bool:
        ended = self._end(_check_wait(wait))
        if ended is None:
            raise PortWithdrawIncomplete([self])
        return ended

    def _end(self, wait: float) -> bool | None:
        """End the ticket if it is admitted, waiting at most ``wait`` for a running reveal. None: the wait ran out;
        True: this call withdrew it; False: it had already ended (and no reveal of it is running)."""
        if self._revealer == threading.get_ident():
            raise DriverStateError("withdraw called from inside this ticket's own reveal")
        if not self._lock.acquire(timeout=wait):
            return None
        try:
            if self._state != ADMITTED:
                return False
            self._state = WITHDRAWN
            return True
        finally:
            self._lock.release()


def _check_wait(wait: object) -> float:
    if type(wait) not in (int, float) or not math.isfinite(wait) or wait < 0:
        raise DriverInputError("wait")
    return float(wait)


def _check_handle(handle: object) -> Handle:
    if not isinstance(handle, Handle):
        raise DriverInputError("handle")
    return handle


def _end_tickets(handle: Handle, wait: float) -> None:
    """The second half of a withdraw, after the seal, NOT under the handle's lock: end every ticket of the handle,
    waiting at most ``wait`` seconds in all for reveals already running. Every ticket is tried even when one step
    raises; the first exception then leaves (an interruption is kept), else ``PortWithdrawIncomplete`` if a reveal
    outlasted the wait. Tickets not seen ended stay on the handle, so a later withdraw waits for them again."""
    with handle._lock:
        tickets = list(handle._tickets)
    deadline = time.monotonic() + wait
    unended: list[PortTicket] = []
    first: BaseException | None = None
    for ticket in tickets:
        try:
            if ticket._end(max(0.0, deadline - time.monotonic())) is None:
                unended.append(ticket)
        except BaseException as exc:  # noqa: B036 -- kept and raised below, after every ticket was tried
            unended.append(ticket)
            if first is None:
                first = exc
    with handle._lock:
        handle._tickets = [t for t in handle._tickets if t not in tickets or t in unended]
    if first is not None:
        raise first
    if unended:
        raise PortWithdrawIncomplete(unended)


class Driver:
    """The gate every engine driver shares. Subclasses set ``declaration`` and implement the ``_`` hooks."""

    declaration: Declaration

    # -- the calls ------------------------------------------------------------------------------------
    def start(self, session: object, workspace: object, env: object) -> Handle:
        """Begin a new conversation. PROVISIONAL signature: the frozen brief's ``start(session, workspace, env)``.

        ``session`` is ``{session_id, run_id, route}`` and ``workspace`` is common.v1's workspace or None;
        together they are driver.v2's start record with ``engine_session_id`` null, which is what the subclass's
        ``_start`` receives. Whether the calls take the brief's arguments or driver.v2's one start record is
        open with Lucas (lane questions Q7); consumers must not rely on either form yet.
        """
        return self._begin(check_start(_record(session, workspace, None)), env)

    def resume(self, engine_session_id: object, session: object, workspace: object, env: object) -> Handle:
        """A new run that continues ``engine_session_id`` (needs ``resume``). PROVISIONAL, as ``start``: the brief
        writes ``resume(engine_session_id)``; a resume launches a new run, so it also takes that run's session
        and workspace and the child environment. The flag is checked before anything else."""
        self._need("resume", "resume")
        if engine_session_id is None:
            raise DriverInputError("engine_session_id")
        return self._begin(check_start(_record(session, workspace, engine_session_id)), env)

    def _begin(self, req: dict, env: object) -> Handle:
        child_env = check_env(env)
        handle = Handle(req["run_id"], req["engine_session_id"])
        self._start(handle, req, child_env)
        return handle

    def send(self, handle: Handle, item: object) -> None:
        item = check_input(item)
        kind = item["kind"]
        with handle._write_lock:  # the write happens in the order of the reservation (ORDER AND LOCKS)
            with handle._lock:
                first_close = kind == "close" and not handle._closed
                if kind == "close":
                    handle._port_seal.sealed = True  # first: no ticket of the worker can begin a reveal (PORT WITHDRAW)
                if handle._closed:
                    if kind != "close":
                        raise DriverStateError("the worker's input is closed")
                elif kind == "approval_answer":
                    self._need("send", "permission_bridge")
                elif kind == "close":
                    handle._closed = True  # before the write: nothing is sent after a close
                else:
                    self._reserve(handle, item)
            if kind != "close":
                self._send(handle, item)
                return
            self._close(handle, item, first_close)

    def _close(self, handle: Handle, item: dict, first_close: bool) -> None:
        """The close input, under the write lock and after the seal: the write (once), then the tickets' end. Both
        run even if the other raises; the first exception leaves. A repeated close writes nothing, finishes the
        withdraw an earlier close left unfinished, and is then refused as before."""
        failure: BaseException | None = None
        if first_close:
            try:
                self._send(handle, item)
            except BaseException as exc:  # noqa: B036 -- kept and raised below, after the tickets were ended
                failure = exc
        try:
            _end_tickets(handle, REVEAL_WAIT_S)
        except BaseException as exc:  # noqa: B036
            if failure is None:
                raise
            failure.add_note(f"the approvals port's withdraw also failed: {exc!r}")
        if failure is not None:
            raise failure
        if not first_close:
            raise DriverStateError("the worker's input is closed")

    # -- the approvals port (PORT WITHDRAW) ----------------------------------------------------------
    def admit_port(self, handle: Handle) -> PortTicket:
        """Admit one request to the approvals port for ``handle``: the ticket the port publishes with.

        Raises ``DriverStateError`` once the worker's port is sealed (``withdraw_port`` or a ``close`` input began)
        or its input is closed, so no new request is admitted for that worker. The caller hands the ticket to the
        port with the request; the port makes the approval visible only through ``ticket.publish(reveal)``.
        """
        _check_handle(handle)
        with handle._lock:
            if handle._port_seal.sealed or handle._closed:
                raise DriverStateError("the worker's approvals port is sealed")
            handle._tickets = [t for t in handle._tickets if t._state in (ADMITTED, OPENING)]
            ticket = PortTicket(handle._port_seal)
            handle._tickets.append(ticket)
            return ticket

    def withdraw_port(self, handle: Handle, wait: float = REVEAL_WAIT_S) -> None:
        """Seal the worker's approvals port, then end every ticket admitted for it and not yet published.

        The seal comes first and is one assignment: from then on no ticket of the worker can begin a reveal, so an
        interruption anywhere after it leaves every unpublished ticket unable to open. Then each ticket is ended,
        waiting only for a reveal that is already running, at most ``wait`` seconds in all; nothing else the port,
        the engine or the subclass does is waited for, and no lock of the handle is held while waiting.

        When it RETURNS: every ticket is published or withdrawn, every visible approval of the worker became visible
        before it returned, and none becomes visible after. It RAISES instead if a reveal outlasted the wait
        (``PortWithdrawIncomplete``) or a step raised (that exception, after every ticket was tried); the port stays
        sealed and a later call finishes. A ``close`` input runs the same steps. Calling it again is harmless.
        Raises ``DriverStateError`` if called from inside one of the worker's own reveals.
        """
        _check_handle(handle)
        wait = _check_wait(wait)
        with handle._lock:
            handle._port_seal.sealed = True
        _end_tickets(handle, wait)

    def interrupt(self, handle: Handle, turn_id: object) -> None:
        self._need("interrupt", "interrupt")
        if not isinstance(turn_id, str) or not _TURN.fullmatch(turn_id):
            raise DriverInputError("turn_id")
        with handle._write_lock:
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
            if not self.declaration.flags.mid_turn_input:
                raise DriverBusy()
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
# Each check follows lane SPINE's schemas as pinned at #368 c77aa2e0 (driver.v2, event.v1, common.v1). The
# field names in DriverInputError are dotted paths, for example "route.connection.id".

def _text(value: object, field: str, *, lo: int = 1, hi: int | None = None, no_newline: bool = False) -> str:
    """A string of lo..hi characters; with no_newline, one that holds no CR or LF (common.v1's ``not``)."""
    if not isinstance(value, str) or len(value) < lo or (hi is not None and len(value) > hi):
        raise DriverInputError(field)
    if no_newline and ("\r" in value or "\n" in value):
        raise DriverInputError(field)
    return value


def _matches(value: object, pattern: re.Pattern, field: str) -> str:
    """A string the pattern matches whole, with no CR or LF anywhere (every common.v1 id says so)."""
    if not isinstance(value, str) or "\r" in value or "\n" in value or not pattern.fullmatch(value):
        raise DriverInputError(field)
    return value


def _count(value: object, field: str) -> int:
    """A JSON integer of 0 or more; a bool is not one. No ceiling: event.v1 sets none."""
    if type(value) is bool or not isinstance(value, int) or value < 0:
        raise DriverInputError(field)
    return value


def _timestamp(value: object, field: str) -> str:
    """common.v1's timestamp: the millisecond RFC 3339 spelling with Z, and a real calendar instant."""
    _matches(value, _TIMESTAMP, field)
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise DriverInputError(field) from None
    return value


def check_route_record(route: object, field: str = "route") -> dict:
    """common.v1's route: engine, model (null for the engine's default) and connection {kind, id}. A copy."""
    if not isinstance(route, Mapping) or set(route) != {"engine", "model", "connection"}:
        raise DriverInputError(field)
    _matches(route["engine"], _ENGINE, f"{field}.engine")
    if route["model"] is not None:
        _text(route["model"], f"{field}.model")
    conn = route["connection"]
    if not isinstance(conn, Mapping) or set(conn) != {"kind", "id"}:
        raise DriverInputError(f"{field}.connection")
    if conn["kind"] not in ("subscription", "api_key", "passthrough"):
        raise DriverInputError(f"{field}.connection.kind")
    _text(conn["id"], f"{field}.connection.id", hi=200, no_newline=True)
    return {"engine": route["engine"], "model": route["model"], "connection": dict(conn)}


def check_workspace(workspace: object, field: str = "workspace") -> dict | None:
    """common.v1's workspace, or None. direct: worktree_path is null; worktree: base_ref and worktree_path set."""
    if workspace is None:
        return None
    keys = {"path", "mode", "base_ref", "worktree_path"}
    if not isinstance(workspace, Mapping) or set(workspace) != keys:
        raise DriverInputError(field)
    _text(workspace["path"], f"{field}.path")
    mode = workspace["mode"]
    if mode not in ("direct", "worktree"):
        raise DriverInputError(f"{field}.mode")
    for name in ("base_ref", "worktree_path"):
        if workspace[name] is not None:
            _text(workspace[name], f"{field}.{name}")
        elif mode == "worktree":
            raise DriverInputError(f"{field}.{name}")
    if mode == "direct" and workspace["worktree_path"] is not None:
        raise DriverInputError(f"{field}.worktree_path")
    return dict(workspace)


def _record(session: object, workspace: object, engine_session_id: object) -> dict:
    """driver.v2's start record from the brief's arguments (PROVISIONAL): session is {session_id, run_id, route}."""
    if not isinstance(session, Mapping) or set(session) != {"session_id", "run_id", "route"}:
        raise DriverInputError("session")
    return {"session_id": session["session_id"], "run_id": session["run_id"], "route": session["route"],
            "workspace": workspace, "engine_session_id": engine_session_id}


def check_start(request: object) -> dict:
    """driver.v2's start record: session_id, run_id, workspace, route, engine_session_id. A copy, nested too."""
    keys = {"session_id", "run_id", "workspace", "route", "engine_session_id"}
    if not isinstance(request, Mapping) or set(request) != keys:
        raise DriverInputError("start")
    _matches(request["session_id"], _LOG, "session_id")
    _matches(request["run_id"], _RUN, "run_id")
    workspace = check_workspace(request["workspace"])
    route = check_route_record(request["route"])
    esid = request["engine_session_id"]
    if esid is not None:
        _text(esid, "engine_session_id", hi=256)
    return {"session_id": request["session_id"], "run_id": request["run_id"], "workspace": workspace,
            "route": route, "engine_session_id": esid}


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
        _matches(item["turn_id"], _TURN, "turn_id")
        _text(item["text"], "text")
        _matches(item["delivery_id"], _DELIVERY, "delivery_id")
    elif kind == "approval_answer":
        _matches(item["approval_id"], _APPROVAL, "approval_id")
        if item["decision"] not in ("approve", "deny"):
            raise DriverInputError("decision")
    return dict(item)


def _check_engine_output(p: Mapping, f: str) -> None:
    if p["stream"] not in ("stdout", "stderr"):
        raise DriverInputError(f"{f}.stream")
    if not isinstance(p["text"], str):
        raise DriverInputError(f"{f}.text")
    _count(p["offset_start"], f"{f}.offset_start")
    _count(p["offset_end"], f"{f}.offset_end")


def _check_tool_call(p: Mapping, f: str) -> None:
    _text(p["call_id"], f"{f}.call_id", hi=128, no_newline=True)
    _text(p["name"], f"{f}.name", hi=256, no_newline=True)
    _matches(p["input_digest"], _SHA256, f"{f}.input_digest")
    carried = p["input"]
    if carried is None:
        return  # not carried: the digest is of an input this record does not hold
    if not isinstance(carried, Mapping):
        raise DriverInputError(f"{f}.input")
    try:
        # event.v1: input_digest is the SHA-256 of the input as RFC 8785 writes it. A carried input
        # whose digest is some other hash is refused, not kept.
        text = canonical_text(carried if type(carried) is dict else dict(carried))
    except (NoCanonicalText, TypeError, RecursionError, RuntimeError):
        raise DriverInputError(f"{f}.input") from None
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != p["input_digest"]:
        raise DriverInputError(f"{f}.input_digest")


def _check_tool_result(p: Mapping, f: str) -> None:
    _text(p["call_id"], f"{f}.call_id", hi=128, no_newline=True)
    if p["status"] not in ("ok", "error", "denied", "cancelled"):
        raise DriverInputError(f"{f}.status")
    if p["text"] is not None and not isinstance(p["text"], str):
        raise DriverInputError(f"{f}.text")
    if p["output_ref"] is not None:
        _matches(p["output_ref"], _TOKEN, f"{f}.output_ref")
    if (p["text"] is None) == (p["output_ref"] is None):  # exactly one of the two carries the output
        raise DriverInputError(f"{f}.output")
    if type(p["truncated"]) is not bool:
        raise DriverInputError(f"{f}.truncated")


def _connection_id(value: object, field: str) -> None:
    _matches(value, _CONNECTION_ID, field)
    if len(value) > 256:
        raise DriverInputError(field)


def _optional_text(value: object, field: str) -> None:
    if value is not None:
        _text(value, field)


def _check_usage(p: Mapping, f: str) -> None:
    _connection_id(p["connection_id"], f"{f}.connection_id")
    _optional_text(p["model"], f"{f}.model")
    _optional_text(p["bucket"], f"{f}.bucket")
    if p["span"] not in ("call", "turn", "session_total"):
        raise DriverInputError(f"{f}.span")
    for name in _TOKEN_FIELDS:
        check_measurement(p[name], integer=True, field=f"{f}.{name}")
    check_measurement(p["cost_usd"], integer=False, field=f"{f}.cost_usd")


def _check_limit(p: Mapping, f: str) -> None:
    _connection_id(p["connection_id"], f"{f}.connection_id")
    if p["scope"] not in ("connection", "model"):
        raise DriverInputError(f"{f}.scope")
    if p["kind"] not in ("plan_limit", "short_fault", "spend_cap"):
        raise DriverInputError(f"{f}.kind")
    _optional_text(p["model"], f"{f}.model")
    if (p["scope"] == "model") != (p["model"] is not None):  # model is a string exactly when scope is model
        raise DriverInputError(f"{f}.model")
    _optional_text(p["bucket"], f"{f}.bucket")
    if p["reset_at"] is not None:
        _timestamp(p["reset_at"], f"{f}.reset_at")


def _check_result(p: Mapping, f: str) -> None:
    if p["code"] is not None:
        _matches(p["code"], _CODE_WORD, f"{f}.code")
    if not isinstance(p["detail"], str):
        raise DriverInputError(f"{f}.detail")


def _check_input_accepted(p: Mapping, f: str) -> None:
    _matches(p["delivery_id"], _DELIVERY, f"{f}.delivery_id")
    _matches(p["turn_id"], _TURN, f"{f}.turn_id")
    if p["how"] not in ("new_turn", "mid_turn"):
        raise DriverInputError(f"{f}.how")


_PAYLOAD_CHECKS = {"engine.output": _check_engine_output, "tool.call": _check_tool_call,
                   "tool.result": _check_tool_result, "usage": _check_usage, "limit": _check_limit,
                   "result": _check_result, "input.accepted": _check_input_accepted}


def check_draft(draft: object) -> dict:
    """One event draft: a type a driver may draft, and a payload that is that type's event.v1 payload."""
    if not isinstance(draft, Mapping) or set(draft) != {"type", "payload"} or draft["type"] not in DRAFT_KEYS:
        raise DriverInputError("draft.type")
    payload = draft["payload"]
    if not isinstance(payload, Mapping) or set(payload) != DRAFT_KEYS[draft["type"]]:
        raise DriverInputError(f"draft.{draft['type']}")
    _PAYLOAD_CHECKS[draft["type"]](payload, f"draft.{draft['type']}")
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
    if type(number) is bool or not isinstance(number, kinds) or number < 0:
        raise DriverInputError(field)
    if isinstance(number, float) and not math.isfinite(number):  # an int is never converted: no overflow
        raise DriverInputError(field)
