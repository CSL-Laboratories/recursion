# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""The shared ACP client: Agent Client Protocol version 1 over an engine's stdin and stdout. Engine-neutral.

WHERE THIS FILE LIVES. It is written to move to ``routes/acp.py`` unchanged when the map opens that path
(kept by NONE until wave2; ENGINE-E5's from then). Until then it sits in ENGINE-E5's own fixtures folder,
because that is the only place the lane may write. It imports nothing from that folder and nothing of
Antigravity's: only ``routes.contract`` (typed codes) and ``routes.launch`` (the process interface).

THE PIN. Every method name, stop reason, permission kind and error code used here was read from the ACP
schema at one release (``SCHEMA_PIN``): ``schema/v1/meta.json`` lists the methods and ``schema/v1/schema.json``
the shapes. Tests check the names in this module against the pinned method list kept beside it
(``acp_meta_v1.json``, byte for byte) and against values copied from the pinned schema
(``acp_schema_excerpt.json``), so a name that drifts from the pin fails a test, not a run.

TWO LAYERS.

``AcpConnection`` is the protocol with no I/O: it builds the lines to write and reads the lines it is fed.
A driver in the driver.v2 world (the core tails the engine's stdout file and calls parse with each line)
uses it directly. It never blocks, never sleeps and starts no thread. Its lock is never held while it
calls out: not the clock, not the engine's hook.

``AcpSession`` drives one connection over an ``EngineProcess`` (``routes.launch``: send, close_stdin,
read_line, fence) for callers that block until a turn ends: today's one-turn path and the tests. It pumps
lines on the CALLER's thread; ``cancel`` and ``answer_permission`` may be called from another thread
while a turn is being pumped. It starts no thread either. ONE ORDERING LOCK spans each change of the
connection's state that writes (a prompt, a cancel, an answer) and the write itself, so the wire carries
them in the order the state changed: a cancel can never reach the agent before the prompt it cancels, and
a cancel of one turn is always on the wire before the next turn's prompt. The connection's own lock is
never held while writing.

WHAT THE CLIENT DOES, in the order of a run:

1. ``initialize`` with protocol version 1 and client capabilities ``fs`` and ``terminal`` OFF. They stay
   off: there is no switch to turn them on in this file (a lane that needs one asks for it). A request
   the agent makes for ``fs/*``, ``terminal/*`` or ``elicitation/*`` is answered ``-32601`` (method not
   found), and nothing is read or written for it.
2. ``authenticate`` with exactly ONE method id, the route's. A method the agent did not advertise in its
   ``initialize`` answer is refused before ``authenticate`` is sent (``AcpRefused``, code
   ``launch_refused``, detail ``acp_method_not_advertised:<id>``). A failed ``authenticate`` ends the open
   (``auth_failed``). No other method is ever tried: there is no fallback between methods.
3. ``session/new`` with the workspace folder as ``cwd`` and no MCP servers, or ``session/load`` when the
   caller resumes and the agent advertised ``loadSession``. When it did not, the load is refused before a
   byte is written (``AcpUnsupported``, code ``launch_refused``). A load the agent refuses can be tried again.
4. ``session/prompt``: one turn at a time. A prompt while another prompt is unanswered is refused
   (``AcpStateError``), so a late update of one turn can never be filed under the next. A prompt whose
   deadline has already passed is refused before anything is written (``deadline_exceeded``).
5. ``session/cancel`` (a notification). Asking to cancel is NOT proof the cancel won: the turn ends
   ``cancelled`` with ``cancel_confirmed`` true only when the agent answers the prompt with stop reason
   ``cancelled``. If the agent answers anything else after the cancel was sent (it finished first), the
   turn still ends ``cancelled`` (the caller asked to stop it, so its result is never shown as a success)
   with ``cancel_confirmed`` false and the agent's own answer kept in ``late_stop_reason``. Every
   permission request still open for the turn is answered ``cancelled`` at once, as ACP requires.
6. ``session/request_permission`` from the agent becomes a ``PermissionAsk`` with the client's own token
   (never the agent's id). It is answered EXACTLY ONCE, through one gate: the caller's answer
   (``approve`` or ``deny``), the deadline (``deny``), or the turn's cancel (``cancelled``), whichever
   comes first; a second answer changes nothing and is counted. An answer that arrives at or after the
   ask's deadline is the deadline's ``deny``, whether or not ``expire`` has run yet. ``approve`` selects
   the agent's ``allow_once`` option only; ``deny`` selects its ``reject_once`` option. ``allow_always``
   and ``reject_always`` are never selected (they make the agent remember a rule beyond this one call).
   If the option needed is not offered the answer is the ``cancelled`` outcome, which runs nothing. This
   client keeps no approval queue: a driver hands each ask to the approval queue, which answers through
   ``answer_permission``. Asks of earlier turns are dropped when a new prompt starts; an answer to one of
   them is counted as late.
7. ``session/update`` notifications go to the caller as they are read. An update read while no prompt is
   unanswered, or after the turn's deadline, is counted as stray and given to nobody.
8. Responses are matched to requests by the JSON-RPC id this client minted; a response to an id it is
   not waiting for (a late one, or one never sent) is counted and dropped. A prompt's request is dropped
   once it is answered.
9. An error answer TO A PROMPT is mapped by the ENGINE's hook first (``EngineHooks.limit``): only the
   engine knows which of its codes is a typed plan limit, so this file maps no numeric code to a limit.
   The hook is called with no lock held, and a hook that raises counts as "no limit" (``hook_failures``).
   Then ``-32000`` is ``auth_failed``; a message that only sounds like a quota
   (``contract.sounds_like_quota``) is a ``short_fault`` (grill Q23: free text outside the model's output
   never makes a plan limit); anything else is ``engine_error``. Errors while opening (initialize,
   authenticate, session/new, session/load) do not reach the hook: they raise ``AcpError``.
10. The agent going away ends the run however it is noticed: the end of its stdout while a request is
    unanswered, or a write to its stdin that fails. A turn in progress ends ``engine_exited``; an open in
    progress raises ``AcpError(engine_exited)``. A line that is not a JSON-RPC 2.0 object (including one
    too deeply nested to read) is a protocol error.
11. Recursion's deadline is checked before each write that starts work and again after each read: an
    answer read after the deadline never makes a success.

CODES. ``TurnOutcome.code`` is None (succeeded), a route contract code, or ``cancelled``, which is not one
of the route contract's CODES: a caller on the route contract maps it (the one-shot adapter reports
``engine_error``). Every ``AcpError`` raised carries a route contract code.

WHAT IT DOES NOT DO (yet): map ``tool_call`` updates to tool.call drafts (their ``rawInput`` arrives late
and differs by agent), read usage (stable ACP has only ``usage_update``: context size, and an optional
running cost), or read a rate limit (stable ACP has none). The driver.v2 flags of an ACP engine say so.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from recursion_core.routes import contract as c
from recursion_core.routes.launch import EngineProcess, ReadTimeout

# -- the pin ---------------------------------------------------------------------------------------------
PROTOCOL_VERSION = 1
SCHEMA_PIN = {
    "repository": "agentclientprotocol/agent-client-protocol",
    "tag": "v1.18.0",
    "commit": "13b765828f961812f0c78ae577220e994209fa2f",
    "schema_file": "schema/v1/schema.json",
    "schema_version": "1.32.0",
    "schema_sha256": "e747a7bec3c02708b70885bf3cccac5c72fa421390dd7e85758fd61e33b1ae21",
    "meta_file": "schema/v1/meta.json",
    "meta_sha256": "061edb6efa8fb2aa2792459a86ec7268de5fe665bba48b2ffe7939df01481f88",
}

# Agent methods (meta.json "agentMethods") this client sends.
INITIALIZE = "initialize"
AUTHENTICATE = "authenticate"
SESSION_NEW = "session/new"
SESSION_LOAD = "session/load"
SESSION_PROMPT = "session/prompt"
SESSION_CANCEL = "session/cancel"
# Client methods (meta.json "clientMethods") the agent may call on this client.
SESSION_UPDATE = "session/update"
REQUEST_PERMISSION = "session/request_permission"
REFUSED_CLIENT_METHODS = (
    "fs/read_text_file", "fs/write_text_file", "terminal/create", "terminal/output", "terminal/release",
    "terminal/wait_for_exit", "terminal/kill", "elicitation/create", "elicitation/complete",
)
SENT_METHODS = (INITIALIZE, AUTHENTICATE, SESSION_NEW, SESSION_LOAD, SESSION_PROMPT, SESSION_CANCEL)
ANSWERED_METHODS = (SESSION_UPDATE, REQUEST_PERMISSION) + REFUSED_CLIENT_METHODS

# schema.json $defs/StopReason, PermissionOptionKind, RequestPermissionOutcome, ErrorCode.
STOP_REASONS = ("end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled")
ALLOW_ONCE, ALLOW_ALWAYS, REJECT_ONCE, REJECT_ALWAYS = "allow_once", "allow_always", "reject_once", "reject_always"
OPTION_KINDS = (ALLOW_ONCE, ALLOW_ALWAYS, REJECT_ONCE, REJECT_ALWAYS)
METHOD_NOT_FOUND = -32601
AUTH_REQUIRED = -32000

# What a turn's result code is, for each way it can end (see CODES in the docstring).
CANCELLED = "cancelled"
STOP_REASON_CODES = {"end_turn": None, "cancelled": CANCELLED, "max_tokens": c.ENGINE_ERROR,
                     "max_turn_requests": c.ENGINE_ERROR, "refusal": c.ENGINE_ERROR}

APPROVE, DENY = "approve", "deny"
_OPEN_METHODS = (INITIALIZE, AUTHENTICATE, SESSION_NEW, SESSION_LOAD)


# -- errors ----------------------------------------------------------------------------------------------
class AcpError(Exception):
    """A typed failure of the client. ``code`` is a route contract code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class AcpRefused(AcpError):
    """Refused before the step it names wrote anything (a method not advertised, for example)."""


class AcpUnsupported(AcpRefused):
    """The agent did not advertise what the call needs. Nothing was written. Code ``launch_refused``."""

    def __init__(self, detail: str) -> None:
        super().__init__(c.LAUNCH_REFUSED, detail)


class AcpStateError(RuntimeError):
    """A call the connection's state does not allow. Nothing was written."""


class AcpProtocolError(AcpError):
    """A line from the agent that is not ACP over JSON-RPC 2.0."""

    def __init__(self, detail: str) -> None:
        super().__init__(c.PROTOCOL_ERROR, detail)


@dataclass(frozen=True)
class RpcError:
    """A JSON-RPC error object as the agent sent it. ``message`` is the agent's text: untrusted."""

    code: int
    message: str
    data: object = None


# -- the per-engine hook ---------------------------------------------------------------------------------
class EngineHooks:
    """What only an engine knows. The default knows nothing, so it maps no error to a limit.

    ``limit`` is asked about an error answer to a prompt only, with no lock of the connection held: it may
    call the connection's methods."""

    def limit(self, method: str, error: RpcError) -> dict | None:
        """A typed limit signal (``contract.limit_signal``) for this engine's error, or None."""
        return None


NO_HOOKS = EngineHooks()


# -- records the connection hands out --------------------------------------------------------------------
@dataclass
class PermissionAsk:
    """One permission request of the agent, as the client holds it until it is answered once."""

    token: str                    # the client's own id for the ask; never the agent's request id
    turn: int                     # the turn it came in
    tool_call: dict               # the agent's ToolCallUpdate, untrusted
    options: tuple                # the agent's PermissionOption list, untrusted
    deadline_at: float | None
    _rpc_id: object = field(repr=False, default=None)
    answer: str | None = None     # approve | deny | cancelled, once answered
    answered_by: str | None = None
    option_id: str | None = None  # the option sent, or None for the cancelled outcome


@dataclass
class TurnOutcome:
    """How one prompt turn ended."""

    turn: int
    code: str | None              # None (succeeded), a route contract code, or "cancelled"
    stop_reason: str | None       # the agent's own stop reason, when it answered the prompt
    cancel_requested: bool
    cancel_confirmed: bool        # True only when the agent answered "cancelled" after the request
    late_stop_reason: str | None  # what the agent answered after a cancel it did not confirm
    error: RpcError | None
    limit: dict | None
    detail: str
    text: str                     # agent_message_chunk text of the turn; empty when code is not None


@dataclass
class _Pending:
    method: str
    turn: int | None = None
    done: bool = False
    result: object = None
    error: RpcError | None = None


@dataclass
class _Turn:
    number: int
    rpc_id: int
    deadline_at: float
    cancel_requested: bool = False
    text: list = field(default_factory=list)
    outcome: TurnOutcome | None = None


@dataclass
class _HookNeeded:
    """A prompt's error answer that the engine's hook must see before the turn's outcome is set."""

    turn: _Turn
    error: RpcError


@dataclass
class Fed:
    """What one fed line gave: the items for the caller and the lines to write back to the agent."""

    updates: list = field(default_factory=list)      # session/update params, for the turn in progress
    asks: list = field(default_factory=list)         # new PermissionAsk records
    replies: list = field(default_factory=list)      # lines to write, in order
    ended: TurnOutcome | None = None                 # a turn this line ended


# -- the protocol, no I/O --------------------------------------------------------------------------------
class AcpConnection:
    """ACP version 1 as data in and lines out. Thread-safe: one lock, never held while calling out."""

    def __init__(self, *, hooks: EngineHooks = NO_HOOKS, clock: Callable[[], float] | None = None,
                 permission_timeout_s: float | None = None) -> None:
        if permission_timeout_s is not None and (
                isinstance(permission_timeout_s, bool) or not isinstance(permission_timeout_s, (int, float))
                or not math.isfinite(permission_timeout_s) or not permission_timeout_s > 0):
            raise ValueError("permission_timeout_s must be a finite number of seconds above zero")
        self._hooks = hooks
        self._clock = clock
        self._permission_timeout_s = permission_timeout_s
        self._lock = threading.Lock()
        self._next_id = 0
        self._next_ask = 0
        self._pending: dict[int, _Pending] = {}
        self._asks: dict[str, PermissionAsk] = {}
        self._turns = 0
        self._turn: _Turn | None = None
        self.agent_capabilities: dict | None = None
        self.auth_methods: tuple = ()
        self.session_id: str | None = None
        self._loading: str | None = None     # the session id a session/load in flight names
        self.method_id: str | None = None
        self.stray_responses = 0
        self.stray_updates = 0
        self.late_answers = 0
        self.hook_failures = 0
        self.closed = False

    def _now(self) -> float | None:
        """The clock, read with no lock held."""
        return self._clock() if self._clock is not None else None

    # -- requests ---------------------------------------------------------------------------------------
    def _request(self, method: str, params: dict, turn: int | None = None) -> tuple[int, str]:
        """Caller holds the lock."""
        self._next_id += 1
        rpc_id = self._next_id
        self._pending[rpc_id] = _Pending(method, turn)
        return rpc_id, _line({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})

    def initialize(self) -> tuple[int, str]:
        with self._lock:
            self._open_check()
            if self.agent_capabilities is not None:
                raise AcpStateError("initialize was already answered")
            return self._request(INITIALIZE, {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            })

    def authenticate(self, method_id: str) -> tuple[int, str]:
        """The route's ONE method. Refused before writing if the agent did not advertise it."""
        if not isinstance(method_id, str) or not method_id:
            raise ValueError("method_id")
        with self._lock:
            self._open_check()
            if self.agent_capabilities is None:
                raise AcpStateError("authenticate before initialize was answered")
            if self.method_id is not None:
                raise AcpStateError("a method was already chosen for this connection")
            if method_id not in self.auth_methods:
                raise AcpRefused(c.LAUNCH_REFUSED, f"acp_method_not_advertised:{method_id}")
            self.method_id = method_id
            return self._request(AUTHENTICATE, {"methodId": method_id})

    def new_session(self, cwd: str) -> tuple[int, str]:
        self._check_cwd(cwd)
        with self._lock:
            self._session_check()
            return self._request(SESSION_NEW, {"cwd": cwd, "mcpServers": []})

    def load_session(self, session_id: str, cwd: str) -> tuple[int, str]:
        self._check_cwd(cwd)
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id")
        with self._lock:
            self._session_check()
            if self.agent_capabilities.get("loadSession") is not True:
                raise AcpUnsupported("acp_load_not_advertised: the agent did not advertise loadSession")
            if self._loading is not None:
                raise AcpStateError("a session/load is already in flight")
            self._loading = session_id
            return self._request(SESSION_LOAD, {"sessionId": session_id, "cwd": cwd, "mcpServers": []})

    def prompt(self, text: str, deadline_at: float) -> tuple[int, str]:
        if not isinstance(text, str) or not text:
            raise ValueError("text")
        with self._lock:
            self._open_check()
            if self.session_id is None:
                raise AcpStateError("prompt before a session was opened")
            if self._turn is not None:
                raise AcpStateError("a prompt is still unanswered")
            self._turns += 1
            # Every ask of an earlier turn was answered when that turn ended: drop them.
            self._asks = {t: a for t, a in self._asks.items() if a.turn >= self._turns}
            rpc_id, line = self._request(SESSION_PROMPT, {
                "sessionId": self.session_id, "prompt": [{"type": "text", "text": text}],
            }, turn=self._turns)
            self._turn = _Turn(self._turns, rpc_id, deadline_at)
            return rpc_id, line

    def cancel(self) -> list[str]:
        """The cancel notification, then a ``cancelled`` answer to every ask still open for the turn.

        Returns the lines to write, in order; empty when no turn is in progress. Calling it twice sends
        the notification once."""
        with self._lock:
            turn = self._turn
            if turn is None or self.closed:
                return []
            lines = []
            if not turn.cancel_requested:
                turn.cancel_requested = True
                lines.append(_line({"jsonrpc": "2.0", "method": SESSION_CANCEL,
                                    "params": {"sessionId": self.session_id}}))
            for ask in self._open_asks(turn.number):
                lines.append(self._settle(ask, "cancelled", "cancel"))
            return lines

    def release(self, rpc_id: int) -> None:
        """Forget a request the caller has read the answer of (an open step's)."""
        with self._lock:
            pending = self._pending.get(rpc_id)
            if pending is not None and pending.done:
                del self._pending[rpc_id]

    # -- permission answers ------------------------------------------------------------------------------
    def answer_permission(self, token: str, decision: str) -> str | None:
        """The caller's answer: the line to write, or None when the ask was already answered (counted late).

        At or after the ask's deadline the answer is the deadline's ``deny``, whatever the caller said: the
        line returned is that deny, and ``ask.answered_by`` is ``deadline``."""
        if decision not in (APPROVE, DENY):
            raise ValueError("decision")
        now = self._now()
        with self._lock:
            ask = self._asks.get(token)
            if ask is None:
                if self._issued(token):   # an ask of an earlier turn, dropped when that turn ended
                    self.late_answers += 1
                    return None
                raise KeyError(token)
            if ask.answer is not None or self.closed:
                self.late_answers += 1
                return None
            if ask.deadline_at is not None and now is not None and now >= ask.deadline_at:
                return self._settle(ask, DENY, "deadline")
            return self._settle(ask, decision, "caller")

    def expire(self, now: float) -> list[str]:
        """Deny every open ask whose deadline has passed. The lines to write."""
        with self._lock:
            if self.closed:
                return []
            return [self._settle(ask, DENY, "deadline") for ask in list(self._asks.values())
                    if ask.answer is None and ask.deadline_at is not None and now >= ask.deadline_at]

    def next_deadline(self) -> float | None:
        """The earliest deadline of an open ask, or None."""
        with self._lock:
            open_ = [a.deadline_at for a in self._asks.values() if a.answer is None and a.deadline_at is not None]
            return min(open_) if open_ else None

    def ask(self, token: str) -> PermissionAsk:
        with self._lock:
            return self._asks[token]

    # -- reading ----------------------------------------------------------------------------------------
    def feed(self, line: str) -> Fed:
        """One line of the agent's stdout. Raises ``AcpProtocolError`` for a line that is not JSON-RPC 2.0."""
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            raise AcpProtocolError("stdout line is not JSON (or is nested too deeply to read)") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise AcpProtocolError("stdout line is not a JSON-RPC 2.0 object")
        now = self._now()
        fed = Fed()
        hook_needed = None
        with self._lock:
            if self.closed:
                return fed
            if "method" in message:
                if not isinstance(message["method"], str):
                    raise AcpProtocolError("method is not a string")
                if "id" in message:
                    self._agent_request(message, fed, now)
                else:
                    self._notification(message, fed)
            elif "id" in message and ("result" in message or "error" in message):
                hook_needed = self._response(message, fed)
            else:
                raise AcpProtocolError("neither a request, a notification nor a response")
        if hook_needed is not None:
            fed.ended = self._ended_by_error(hook_needed)
        return fed

    def end_of_output(self) -> TurnOutcome | None:
        """The agent is gone. Every unanswered request fails; a turn in progress ends engine_exited."""
        with self._lock:
            self.closed = True
            for pending in self._pending.values():
                if not pending.done:
                    pending.done = True
                    pending.error = RpcError(0, "the agent's output ended")
            for ask in self._open_asks(None):
                ask.answer, ask.answered_by = "cancelled", "cancel"
            turn, self._turn = self._turn, None
            if turn is None or turn.outcome is not None:
                return None
            return self._end(turn, code=c.ENGINE_EXITED, detail="the agent went away before the turn ended")

    def deadline(self) -> TurnOutcome | None:
        """Recursion's own deadline for the turn in progress passed: the turn ends deadline_exceeded here.

        The prompt request stays pending as ABANDONED: a late answer to it is dropped, never filed under a
        later turn, and no new prompt is accepted until it has come. Open asks stay open until ``cancel``
        (which ``AcpSession`` sends at once) answers them ``cancelled``, or the output ends."""
        with self._lock:
            turn = self._turn
            if turn is None or turn.outcome is not None:
                return None
            turn.outcome = self._outcome(turn, code=c.DEADLINE_EXCEEDED, detail="recursion deadline")
            return turn.outcome

    def pending_result(self, rpc_id: int) -> _Pending:
        with self._lock:
            return self._pending[rpc_id]

    @property
    def turn_in_progress(self) -> int | None:
        with self._lock:
            return self._turn.number if self._turn is not None else None

    # -- internals (caller holds the lock, unless said otherwise) ----------------------------------------
    def _open_check(self) -> None:
        if self.closed:
            raise AcpStateError("the agent's output has ended")

    def _session_check(self) -> None:
        self._open_check()
        if self.agent_capabilities is None:
            raise AcpStateError("a session before initialize was answered")
        if self.session_id is not None:
            raise AcpStateError("a session is already open on this connection")

    @staticmethod
    def _check_cwd(cwd: object) -> None:
        if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
            raise ValueError("cwd must be an absolute path")

    def _issued(self, token: object) -> bool:
        return (isinstance(token, str) and token.startswith("ask-") and token[4:].isdigit()
                and 1 <= int(token[4:]) <= self._next_ask)

    def _open_asks(self, turn: int | None) -> list:
        return [a for a in self._asks.values() if a.answer is None and (turn is None or a.turn == turn)]

    def _settle(self, ask: PermissionAsk, answer: str, by: str) -> str:
        """Answer ``ask`` (it is open). The line to write."""
        kind = {APPROVE: ALLOW_ONCE, DENY: REJECT_ONCE}.get(answer)
        option = next((o for o in ask.options if isinstance(o, Mapping) and o.get("kind") == kind
                       and isinstance(o.get("optionId"), str) and o["optionId"]), None) if kind else None
        ask.answer, ask.answered_by = answer, by
        if option is None:
            ask.option_id = None
            outcome = {"outcome": "cancelled"}
        else:
            ask.option_id = option["optionId"]
            outcome = {"outcome": "selected", "optionId": option["optionId"]}
        return _line({"jsonrpc": "2.0", "id": ask._rpc_id, "result": {"outcome": outcome}})

    def _agent_request(self, message: dict, fed: Fed, now: float | None) -> None:
        method, rpc_id = message["method"], message["id"]
        if method == REQUEST_PERMISSION:
            params = message.get("params")
            turn = self._turn
            if (turn is None or turn.outcome is not None or not isinstance(params, Mapping)
                    or params.get("sessionId") != self.session_id):
                # No turn of ours is waiting for it: refuse it, run nothing.
                fed.replies.append(_line({"jsonrpc": "2.0", "id": rpc_id,
                                          "result": {"outcome": {"outcome": "cancelled"}}}))
                return
            self._next_ask += 1
            token = f"ask-{self._next_ask}"
            deadline_at = None
            if self._permission_timeout_s is not None and now is not None:
                deadline_at = now + self._permission_timeout_s
            tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), Mapping) else {}
            options = tuple(params.get("options")) if isinstance(params.get("options"), list) else ()
            ask = PermissionAsk(token, turn.number, dict(tool_call), options, deadline_at, _rpc_id=rpc_id)
            self._asks[token] = ask
            if turn.cancel_requested:
                fed.replies.append(self._settle(ask, "cancelled", "cancel"))
            else:
                fed.asks.append(ask)
            return
        # fs/*, terminal/*, elicitation/* and anything else: this client offers none of them.
        fed.replies.append(_line({"jsonrpc": "2.0", "id": rpc_id,
                                  "error": {"code": METHOD_NOT_FOUND, "message": f"{method} is not offered"}}))

    def _notification(self, message: dict, fed: Fed) -> None:
        if message["method"] != SESSION_UPDATE:
            return  # an extension notification: nothing of ours reads it
        params = message.get("params")
        turn = self._turn
        if (turn is None or turn.outcome is not None or not isinstance(params, Mapping)
                or params.get("sessionId") != self.session_id or not isinstance(params.get("update"), Mapping)):
            self.stray_updates += 1
            return
        update = params["update"]
        if update.get("sessionUpdate") == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, Mapping) and content.get("type") == "text" and isinstance(content.get("text"), str):
                turn.text.append(content["text"])
        fed.updates.append(dict(params))

    def _response(self, message: dict, fed: Fed) -> _HookNeeded | None:
        rpc_id = message["id"]
        pending = self._pending.get(rpc_id) if type(rpc_id) is int else None
        if pending is None or pending.done:
            self.stray_responses += 1
            return None
        pending.done = True
        if "error" in message:
            err = message["error"]
            if (not isinstance(err, Mapping) or type(err.get("code")) is not int
                    or not isinstance(err.get("message"), str)):
                raise AcpProtocolError("error object without an integer code and a message")
            pending.error = RpcError(err["code"], err["message"], err.get("data"))
        else:
            pending.result = message["result"]
        if pending.method == INITIALIZE and pending.error is None:
            self._initialized(pending.result)
        elif pending.method in (SESSION_NEW, SESSION_LOAD):
            if pending.error is None:
                self._session_opened(pending)
            elif pending.method == SESSION_LOAD:
                self._loading = None       # a refused load leaves the connection able to load again
        elif pending.method == SESSION_PROMPT:
            del self._pending[rpc_id]      # nobody reads a prompt's request after its answer
            answered = self._prompt_answered(pending)
            if isinstance(answered, _HookNeeded):
                return answered
            fed.ended = answered
        return None

    def _initialized(self, result: object) -> None:
        if not isinstance(result, Mapping) or result.get("protocolVersion") != PROTOCOL_VERSION:
            raise AcpProtocolError("initialize answered another protocol version")
        caps = result.get("agentCapabilities")
        self.agent_capabilities = dict(caps) if isinstance(caps, Mapping) else {}
        methods = result.get("authMethods")
        ids = []
        for m in methods if isinstance(methods, list) else ():
            if isinstance(m, Mapping) and isinstance(m.get("id"), str) and m["id"]:
                ids.append(m["id"])
        self.auth_methods = tuple(ids)

    def _session_opened(self, pending: _Pending) -> None:
        if pending.method == SESSION_NEW:
            result = pending.result
            if (not isinstance(result, Mapping) or not isinstance(result.get("sessionId"), str)
                    or not result["sessionId"]):
                raise AcpProtocolError("session/new answered without a sessionId")
            self.session_id = result["sessionId"]
        else:
            self.session_id = self._loading
        self._loading = None

    def _prompt_answered(self, pending: _Pending):
        turn = self._turn
        if turn is None or turn.rpc_id is None or turn.number != pending.turn:
            return None
        self._turn = None
        for ask in self._open_asks(turn.number):
            ask.answer, ask.answered_by = "cancelled", "cancel"
        if turn.outcome is not None:
            return None  # the turn had already ended (its deadline passed); this answer is late and dropped
        if pending.error is not None:
            if turn.cancel_requested:
                detail = f"cancel requested; then acp error {pending.error.code}: {pending.error.message[:200]}"
                return self._end(turn, code=CANCELLED, error=pending.error, detail=detail)
            return _HookNeeded(turn, pending.error)
        result = pending.result
        reason = result.get("stopReason") if isinstance(result, Mapping) else None
        if reason not in STOP_REASONS:
            raise AcpProtocolError("session/prompt answered without a known stopReason")
        if turn.cancel_requested:
            if reason == "cancelled":
                return self._end(turn, code=CANCELLED, stop_reason=reason, confirmed=True,
                                 detail="the agent confirmed the cancel")
            return self._end(turn, code=CANCELLED, stop_reason=reason, late=reason,
                             detail=f"cancel requested; the agent then answered {reason}; that result is not shown")
        return self._end(turn, code=STOP_REASON_CODES[reason], stop_reason=reason,
                         detail="" if reason == "end_turn" else f"stop_reason {reason}")

    def _ended_by_error(self, needed: _HookNeeded) -> TurnOutcome:
        """Called with NO lock held: the engine's hook, then the outcome under the lock."""
        turn, error = needed.turn, needed.error
        try:
            limit = self._hooks.limit(SESSION_PROMPT, error)
        except Exception:
            limit = None
            with self._lock:
                self.hook_failures += 1
        detail = f"acp error {error.code}: {error.message[:200]}"
        with self._lock:
            if limit is not None:
                return self._end(turn, code=c.PLAN_LIMIT, error=error, limit=limit, detail=detail)
            if error.code == AUTH_REQUIRED:
                return self._end(turn, code=c.AUTH_FAILED, error=error, detail=detail)
            if c.sounds_like_quota(error.message):
                return self._end(turn, code=c.SHORT_FAULT, error=error, detail=detail)
            return self._end(turn, code=c.ENGINE_ERROR, error=error, detail=detail)

    def _outcome(self, turn: _Turn, *, code, stop_reason=None, confirmed=False, late=None, error=None,
                 limit=None, detail="") -> TurnOutcome:
        # A turn that did not succeed carries no text: a cancelled or failed turn's words are never handed
        # on as if they were its answer (they stay in the raw lines the caller was given with on_line).
        text = "".join(turn.text) if code is None else ""
        return TurnOutcome(turn.number, code, stop_reason, turn.cancel_requested, confirmed, late, error, limit,
                           detail, text)

    def _end(self, turn: _Turn, **kwargs) -> TurnOutcome:
        turn.outcome = self._outcome(turn, **kwargs)
        return turn.outcome


def _line(message: dict) -> str:
    # ASCII only: a character the agent sent that cannot be written as UTF-8 (a lone surrogate) is escaped,
    # never left to fail the write.
    return json.dumps(message, separators=(",", ":"), ensure_ascii=True)


# -- the blocking runner --------------------------------------------------------------------------------
class AcpSession:
    """One ACP connection over one process, pumped on the caller's thread.

    ``on_line(line)`` is called with every line read, before it is parsed (a driver drafts engine.output
    from it). ``on_update(params)`` gets each session/update of the turn; ``on_ask(ask)`` each new permission
    ask (it must not block: the answer comes back through ``answer_permission``, from any thread).
    """

    def __init__(self, process: EngineProcess, clock: Callable[[], float], *, hooks: EngineHooks = NO_HOOKS,
                 permission_timeout_s: float | None = None,
                 on_line: Callable[[str], object] | None = None,
                 on_update: Callable[[dict], object] | None = None,
                 on_ask: Callable[[PermissionAsk], object] | None = None) -> None:
        self.process = process
        self._clock = clock
        self.connection = AcpConnection(hooks=hooks, clock=clock, permission_timeout_s=permission_timeout_s)
        # The ordering lock: held across a state change that writes and the write itself. Re-entrant, so a
        # step holding it can call _write. The connection's own lock is never held while it is taken.
        self._order = threading.RLock()
        self.input_lost = False
        self._on_line, self._on_update, self._on_ask = on_line, on_update, on_ask

    # -- writing ------------------------------------------------------------------------------------------
    def _write(self, lines) -> bool:
        """Write ``lines`` in order. False when the agent's input is gone (it is never raised)."""
        with self._order:
            for line in lines:
                try:
                    self.process.send(line)
                except OSError:
                    self.input_lost = True
                    return False
            return True

    def _step(self, make) -> tuple[int, bool]:
        """``make()`` changes the state and gives (rpc_id, line); the line is written before anyone else's."""
        with self._order:
            rpc_id, line = make()
            return rpc_id, self._write([line])

    # -- opening ------------------------------------------------------------------------------------------
    def open(self, method_id: str, cwd: str, deadline_at: float, *, load_session_id: str | None = None) -> str:
        """initialize, authenticate with ``method_id``, then session/new (or session/load). The session id."""
        conn = self.connection
        self._before(deadline_at, "before initialize was sent")
        self._open_step(conn.initialize, INITIALIZE, c.ENGINE_ERROR, deadline_at)
        self._before(deadline_at, "before authenticate was sent")
        # Refuses an unadvertised method before writing.
        self._open_step(lambda: conn.authenticate(method_id), AUTHENTICATE, c.AUTH_FAILED, deadline_at)
        self._before(deadline_at, "before the session was opened")
        if load_session_id is None:
            self._open_step(lambda: conn.new_session(cwd), SESSION_NEW, c.ENGINE_ERROR, deadline_at)
        else:
            # Refuses before writing if the agent did not advertise loadSession.
            self._open_step(lambda: conn.load_session(load_session_id, cwd), SESSION_LOAD, c.ENGINE_ERROR,
                            deadline_at)
        return conn.session_id

    def _before(self, deadline_at: float, what: str) -> None:
        if self._clock() >= deadline_at:
            raise AcpError(c.DEADLINE_EXCEEDED, f"the deadline passed {what}")

    def _open_step(self, make, method: str, code: str, deadline_at: float) -> None:
        rpc_id, written = self._step(make)
        if not written:
            self.connection.end_of_output()
            raise AcpError(c.ENGINE_EXITED, f"{method}: the agent's input is closed")
        self._await(rpc_id, deadline_at)
        pending = self.connection.pending_result(rpc_id)
        self.connection.release(rpc_id)
        if pending.error is not None:
            err = pending.error
            if err.code == 0 and self.connection.closed:
                raise AcpError(c.ENGINE_EXITED, f"{method}: the agent went away")
            if code != c.AUTH_FAILED and err.code == AUTH_REQUIRED:
                code = c.AUTH_FAILED
            raise AcpError(code, f"{method}: acp error {err.code}: {err.message[:200]}")

    def _await(self, rpc_id: int, deadline_at: float) -> None:
        """Pump until ``rpc_id`` is answered. Raises AcpError(deadline_exceeded | engine_exited)."""
        while not self.connection.pending_result(rpc_id).done:
            if not self._pump_one(deadline_at):
                break
        if not self.connection.pending_result(rpc_id).done:
            raise AcpError(c.ENGINE_EXITED, "the agent's output ended")

    # -- a turn -------------------------------------------------------------------------------------------
    def prompt(self, text: str, deadline_at: float) -> TurnOutcome:
        """One turn: send the prompt and pump until the agent answers it, the agent goes away or the deadline.

        A deadline that has already passed is refused before anything is written (``AcpError``)."""
        self._before(deadline_at, "before the prompt was sent")
        _rpc_id, written = self._step(lambda: self.connection.prompt(text, deadline_at))
        if not written:
            return self.connection.end_of_output()
        while True:
            outcome = self._pump_one(deadline_at)
            if isinstance(outcome, TurnOutcome):
                return outcome
            if outcome is False:  # the output ended with no turn in progress: cannot happen mid-prompt
                raise AcpStateError("the output ended but no turn outcome was recorded")

    def cancel(self) -> None:
        """Ask the agent to cancel the turn in progress. Safe from any thread; never raises for a gone agent."""
        with self._order:
            self._write(self.connection.cancel())

    def answer_permission(self, token: str, decision: str) -> bool:
        """The caller's answer to an ask. True only when it is the one the agent was sent: False when the ask
        was already answered (deadline, cancel, or an earlier answer), when its deadline had passed (the
        deadline's deny is sent instead), or when the agent's input is gone."""
        with self._order:
            line = self.connection.answer_permission(token, decision)
            if line is None:
                return False
            written = self._write([line])
            return written and self.connection.ask(token).answered_by == "caller"

    def close(self) -> None:
        """End the agent's input. The agent finishes and exits."""
        self.process.close_stdin()

    # -- the pump -----------------------------------------------------------------------------------------
    def _pump_one(self, deadline_at: float):
        """Read and handle one line. Returns a TurnOutcome when a turn ended (by an answer, the agent going
        away or the deadline), False when the output ended with no turn, True otherwise."""
        conn = self.connection
        now = self._clock()
        expired = conn.expire(now)
        if expired:
            self._write(expired)
        if now >= deadline_at:
            outcome = conn.deadline()
            if outcome is not None:
                # The turn is abandoned here; the agent is asked to stop it, and its open asks are answered
                # cancelled, so the session can take another turn once the agent has answered the prompt.
                self.cancel()
                return outcome
            raise AcpError(c.DEADLINE_EXCEEDED, "recursion deadline")
        wait_until = deadline_at
        ask_deadline = conn.next_deadline()
        if ask_deadline is not None:
            wait_until = min(wait_until, ask_deadline)
        try:
            line = self.process.read_line(max(wait_until - now, 0.0))
        except ReadTimeout:
            return True  # the next round expires asks or ends the turn at its deadline
        if line is None:
            outcome = conn.end_of_output()
            return outcome if outcome is not None else False
        # The read may have taken past the deadline: then nothing read now can make a success.
        abandoned = None
        late = self._clock() >= deadline_at
        if late:
            abandoned = conn.deadline()
            if abandoned is not None:
                self.cancel()
        if self._on_line is not None:
            self._on_line(line)
        fed = conn.feed(line)
        if fed.replies:
            self._write(fed.replies)
        for update in fed.updates:
            if self._on_update is not None:
                self._on_update(update)
        for ask in fed.asks:
            if self._on_ask is not None:
                self._on_ask(ask)
        if abandoned is not None:
            return abandoned
        if late and fed.ended is None:
            raise AcpError(c.DEADLINE_EXCEEDED, "recursion deadline (an answer read after it)")
        if fed.ended is not None:
            return fed.ended
        return True
