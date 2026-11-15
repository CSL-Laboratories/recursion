"""The Codex session driver on driver contract version 2 (lane DRIVER-CX). See routes/driver.py for the gate.

One ``codex app-server`` worker per session, kept across turns (CONTRACT 807-808, "The core keeps a
persistent streaming worker per engine wherever the engine supports one", DECIDED). Every method and
field used here is in the schema the installed ``codex-cli 0.160.0`` generates for itself
(``fixtures/codex/app-server-0.160.0.json``; ``routes/tests/test_codex_protocol.py`` and
``test_codex_driver.py`` hold this module to it).

START runs ``initialize`` / ``initialized`` and then ``thread/start``, or ``thread/resume`` with the stored
thread id when the start record names one, and reads their answers itself. The thread id the server
reports becomes the handle's ``engine_session_id``. Nothing names an approval policy or a sandbox in
driver.v2's start record, so neither is sent: Codex applies its own (Lucas, grill Q20: "We shouldn't
restrict the CLIs more than the provider's harness restricts them"), and the effective values the server
reports back are kept on the session (``effective``). A resume the server refuses (an unknown thread, or
one another process is writing) is ``request_invalid`` and nothing else is written.

SEND. A user_message that begins a turn is ``turn/start`` on the open thread, carrying the delivery id as
``clientUserMessageId``; one sent while a turn is in progress is ``turn/steer`` with the engine's id of
that turn as ``expectedTurnId`` (held until the server has reported that id, then written). ``close``
closes standard input. Standard input stays open between turns.

ACCEPTANCE. The server's own typed answer to that request is the acceptance: a ``turn/start`` result
naming a turn gives ``input.accepted`` with ``how: new_turn``; a ``turn/steer`` result whose ``turnId``
is the turn in progress gives ``how: mid_turn``. Nothing read out of a notification or out of text is
taken as an acceptance, and an error answer gives none. So this driver declares ``input_ack: true`` and
``mid_turn_input: true``: a fact about the protocol (the Liaison's ruling of 3:42 AM Nov 13), recorded
from the schema and to be confirmed by Lucas's attended capture.

INTERRUPT is the protocol's own ``turn/interrupt`` (held, like a steer, until the turn's id is known).

A TURN ENDS at ``turn/completed``: the driver calls ``turn_ended`` and drafts, in this order, one
``usage`` (the thread's running total from the last ``thread/tokenUsage/updated`` of that turn, span
``session_total``, every count ``observed``; a count the server did not report, and the cost, which it
never reports, are null with quality ``unknown``; with no usage notice in the turn every count is
unknown), a ``limit`` when the turn failed on a typed usage limit, and a ``result`` whose code is null
for ``completed``. No engine text reaches a ``detail``. A ``limit`` is also drafted when
``account/rateLimits/updated`` reports a window at 100 percent: scope ``connection`` with the vendor's
bucket named (R3 finding 10, the option the brief recommends and Lucas has not yet chosen: a named
bucket fences the whole connection until a bucket-to-model mapping is recorded). ``error``
notifications are not outcomes: Codex sends them for retries too (``willRetry``), and the outcome is the
turn's own status.

APPROVALS (CONTRACT 3430-3431, "forward provider approvals"; 919, "The core draws every approval").
Given an approvals port (``approvals=``: APPROVALS' ``request(effect, binding) -> approval_id``, a fake
until that lane's interface lands), the driver declares ``permission_bridge: true`` and turns each
command or file-change approval request of the server into ONE approval: an approval.v1 ``engine_tool``
effect (the engine, the session and run, the tool, its cwd, and its input as the engine gave it, the file
changes included from the item the server announced) and the private binding without its id
(``run_id``, ``engine``, ``engine_request_id``, ``bound_at``), which the port completes with the
approval id it mints. The JSON-RPC request id, and the thread, turn and item it names, stay in this
driver's private table. Only driver.v2's ``approval_answer {approval_id, decision}`` reaches the engine:
``approve`` is the protocol's ``accept`` (that call, once; never ``acceptForSession``), ``deny`` is
``decline``. It is written on the request id of that same pending call, once, and only while the server
still waits for it: an answer after the server resolved the request itself (``serverRequest/resolved``)
or after the turn ended writes nothing and is refused. A port that raises, or none, means the typed
``decline`` at once, so no tool runs unapproved and the turn is not left hanging. The deadline is
APPROVALS' (it writes the deny at ``expires_at`` and sends it as ``approval_answer``). No approval request
is ever refused -32601. Every other server request is answered with JSON-RPC error -32601.

THE UPDATER. The worker is ``codex -c check_for_update_on_startup=false app-server`` (routes/codex.py's
override) and this module starts no other process.

Not here yet: ``fork`` (false until SESSIONS asks), APPROVALS' real port, ``tool.call`` and
``tool.result`` drafts (their input digest is RFC 8785, taken by the core), who owns a live handle
between turns (DRIVER-CC's S9 drive owner, ``routes/workers.py``).
"""

from __future__ import annotations

import datetime
import json
import threading
import time
from collections.abc import Callable

from recursion_core.routes import contract as c
from recursion_core.routes.codex import _AUTH_ERRORS, _LIMIT_ERRORS, NO_UPDATE_OVERRIDE, _error_info
from recursion_core.routes.driver import Declaration, Driver, DriverStateError, Flags, Handle
from recursion_core.routes.launch import ReadTimeout

ENGINE = "codex"
DEFAULT_EXECUTABLE = "codex"
DECLARATION = Declaration(ENGINE, Flags(multi_turn=True, mid_turn_input=True, interrupt=True, resume=True,
                                        fork=False, permission_bridge=False, typed_limits=True, usage=True),
                          input_ack=True)
BRIDGED = Declaration(ENGINE, Flags(**{**DECLARATION.flags.as_dict(), "permission_bridge": True}), input_ack=True)
CLIENT_INFO = {"name": "recursion", "version": "0"}
# server request method -> the tool an approval names
APPROVAL_REQUESTS = {"item/commandExecution/requestApproval": "commandExecution",
                     "item/fileChange/requestApproval": "fileChange"}
DECLINE = {"decision": "decline"}  # CommandExecutionApprovalDecision / FileChangeApprovalDecision, 0.160.0
ACCEPT = {"decision": "accept"}
_DECISIONS = {"approve": ACCEPT, "deny": DECLINE}
_UNKNOWN = {"value": None, "quality": "unknown", "provenance": "unavailable"}
# event.v1 usage field <- ThreadTokenUsage breakdown field (0.160.0). cacheWriteInputTokens is optional there.
_USAGE_FIELDS = (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                 ("reasoning_tokens", "reasoningOutputTokens"), ("cache_read_tokens", "cachedInputTokens"),
                 ("cache_write_tokens", "cacheWriteInputTokens"))


class _Session:
    """What the driver keeps for one live worker. Its lock is never held while writing to the engine."""

    def __init__(self, process, connection_id: str) -> None:
        self.process = process
        self.connection_id = connection_id
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.next_id = 1
        self.pending: dict[int, tuple] = {}      # our request id -> what it was
        self.thread_id: str | None = None
        self.engine_turn: dict[str, str] = {}    # core turn id -> the engine's turn id
        self.core_turn: dict[str, str] = {}      # the engine's turn id -> core turn id
        self.held: dict[str, list] = {}          # core turn id -> writes waiting for the engine's turn id
        self.usage: dict[str, dict] = {}         # the engine's turn id -> its last ThreadTokenUsage
        self.bucket_limit: dict | None = None    # the last exhausted window seen: {"bucket", "reset_at"}
        self.model: str | None = None            # the served model, only as the protocol reports it
        self.effective: dict = {}                # approvalPolicy and sandbox the server applied
        self.early: list[str] = []               # notifications read during start, parsed with the next line
        self.offset = 0                          # bytes of standard output read so far
        self.items: dict[str, dict] = {}         # item id -> the command or file-change item the server announced
        self.bindings: dict[str, dict] = {}      # approval id -> the pending engine request it answers (PRIVATE)
        self.session_id: str | None = None
        self.run_id: str | None = None

    def reserve(self, record: tuple) -> int:
        with self.lock:
            rid, self.next_id = self.next_id, self.next_id + 1
            self.pending[rid] = record
            return rid

    def write(self, message: dict) -> None:
        with self.write_lock:
            self.process.send(json.dumps(message, separators=(",", ":")))


class CodexDriver(Driver):
    """driver.v2 for ``codex app-server``. ``launcher`` is routes.launch's: ``launcher(argv, env=, shell=False)``."""

    declaration = DECLARATION

    def __init__(self, launcher, clock: Callable[[], float], *, binary: str = DEFAULT_EXECUTABLE,
                 start_timeout_s: float = 60.0, wall_clock: Callable[[], float] | None = None,
                 approvals=None) -> None:
        self._launcher, self._clock, self._binary = launcher, clock, binary
        self._start_timeout_s = start_timeout_s
        self._wall = wall_clock or time.time
        self._approvals = approvals
        if approvals is not None:
            self.declaration = BRIDGED

    def argv(self) -> list[str]:
        return [self._binary, *NO_UPDATE_OVERRIDE, "app-server"]

    # -- start and resume -----------------------------------------------------------------------------
    def _start(self, handle: Handle, request: dict, env: dict[str, str]) -> None:
        route = request["route"]
        conn = route.get("connection") if isinstance(route.get("connection"), dict) else {}
        connection_id = f"{conn.get('kind')}:{conn.get('id')}"
        process = self._launcher(self.argv(), env=env, shell=False)
        session = _Session(process, connection_id)
        session.session_id, session.run_id = request["session_id"], request["run_id"]
        handle._codex = session
        deadline = self._clock() + self._start_timeout_s
        try:
            self._call(session, "initialize", {"clientInfo": dict(CLIENT_INFO)}, deadline)
            session.write({"method": "initialized"})
            params: dict = {}
            if route.get("model") is not None:
                params["model"] = route["model"]
            esid = request["engine_session_id"]
            if esid is None:
                answer = self._call(session, "thread/start", params, deadline)
            else:
                try:
                    answer = self._call(session, "thread/resume", {"threadId": esid, **params}, deadline)
                except _RpcError:
                    raise c.RouteError(c.REQUEST_INVALID, "thread/resume refused: no such engine session, or "
                                       "another worker is writing it") from None
            thread = answer.get("thread") if isinstance(answer, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str) or not thread_id or (esid is not None and thread_id != esid):
                raise c.RouteError(c.PROTOCOL_ERROR, "the thread answer names no thread, or another thread")
            session.thread_id = thread_id
            if isinstance(answer.get("model"), str) and answer["model"]:
                session.model = answer["model"]
            session.effective = {k: answer.get(k) for k in ("approvalPolicy", "sandbox")}
            handle.set_engine_session_id(thread_id)
        except _RpcError as err:
            self._abandon(session, "start_refused")
            raise c.RouteError(_code_of(err.error), "the engine refused the start") from None
        except c.RouteError:
            self._abandon(session, "start_refused")
            raise
        except BaseException:
            self._abandon(session, "start_failed")
            raise

    def _call(self, session: _Session, method: str, params: dict, deadline: float) -> object:
        """One request during start, answered before anything else is done. Notifications are kept."""

        rid = session.reserve(("start", method))
        session.write({"id": rid, "method": method, "params": params})
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise c.RouteError(c.DEADLINE_EXCEEDED, f"no answer to {method} before the start deadline")
            try:
                line = session.process.read_line(remaining)
            except ReadTimeout:
                raise c.RouteError(c.DEADLINE_EXCEEDED, f"no answer to {method} before the start deadline") from None
            if line is None:
                raise c.RouteError(c.ENGINE_EXITED, f"the engine closed its output before answering {method}")
            msg = _message(line)
            session.offset += len(line.encode("utf-8")) + 1
            if msg.get("id") == rid and "method" not in msg:
                with session.lock:
                    session.pending.pop(rid, None)
                if "error" in msg and msg["error"] is not None:
                    if not isinstance(msg["error"], dict):
                        raise c.RouteError(c.PROTOCOL_ERROR, "JSON-RPC error must be an object")
                    raise _RpcError(msg["error"])
                if "result" not in msg:
                    raise c.RouteError(c.PROTOCOL_ERROR, f"the answer to {method} has no result")
                return msg["result"]
            if "id" in msg and "method" in msg:
                self._answer_server_request(session, msg)
            else:
                session.early.append(line)

    def _abandon(self, session: _Session, reason: str) -> None:
        try:
            session.process.close_stdin()
        except OSError:
            pass
        session.process.fence(reason)

    # -- send, interrupt ------------------------------------------------------------------------------
    def _send(self, handle: Handle, item: dict) -> None:
        session = _session(handle)
        if item["kind"] == "close":
            session.process.close_stdin()
            return
        if item["kind"] == "approval_answer":
            self._answer_approval(session, item["approval_id"], item["decision"])
            return
        turn, delivery = item["turn_id"], item["delivery_id"]
        text = [{"type": "text", "text": item["text"]}]
        with session.lock:
            engine_turn = session.engine_turn.get(turn)
            started = turn in session.held or engine_turn is not None
            if not started:  # the gate made this the turn in progress: it begins a turn
                session.held[turn] = []
        if not started:
            rid = session.reserve(("turn/start", delivery, turn))
            session.write({"id": rid, "method": "turn/start", "params": {
                "threadId": session.thread_id, "input": text, "clientUserMessageId": delivery}})
            return
        params = {"threadId": session.thread_id, "input": text, "clientUserMessageId": delivery}
        self._write_for_turn(session, turn, ("turn/steer", delivery, turn), "turn/steer", params, "expectedTurnId")

    def _interrupt(self, handle: Handle, turn_id: str) -> None:
        session = _session(handle)
        params = {"threadId": session.thread_id}
        self._write_for_turn(session, turn_id, ("turn/interrupt", turn_id), "turn/interrupt", params, "turnId")

    def _write_for_turn(self, session: _Session, turn: str, record: tuple, method: str, params: dict,
                        key: str) -> None:
        """Write a request that names the engine's id of ``turn``; hold it until that id is known."""

        with session.lock:
            engine_turn = session.engine_turn.get(turn)
            if engine_turn is None:
                if turn not in session.held:
                    raise DriverStateError(f"{turn} is not a turn this worker began")
                session.held[turn].append((record, method, params, key))
                return
        rid = session.reserve(record)
        session.write({"id": rid, "method": method, "params": {**params, key: engine_turn}})

    def _release_held(self, session: _Session, turn: str) -> None:
        with session.lock:
            held, session.held[turn] = session.held.get(turn, []), []
            engine_turn = session.engine_turn[turn]
        for record, method, params, key in held:
            rid = session.reserve(record)
            session.write({"id": rid, "method": method, "params": {**params, key: engine_turn}})

    def _fork(self, engine_session_id: str, at: str) -> str:  # refused by the gate: fork is false
        raise DriverStateError("fork is not offered by this driver")

    # -- parse ----------------------------------------------------------------------------------------
    def pump(self, handle: Handle, timeout_s: float) -> list[dict] | None:
        """Read one line of the worker's output and parse it; None at EOF. For the drive owner and tests."""

        line = _session(handle).process.read_line(timeout_s)
        return None if line is None else self.parse(handle, line)

    def _parse(self, handle: Handle, line: str) -> list[dict]:
        session = _session(handle)
        with session.lock:
            early, session.early = session.early, []
        drafts: list[dict] = []
        for kept in early:
            drafts += self._one(handle, session, _message(kept), None)
        start = session.offset
        session.offset += len(line.encode("utf-8")) + 1
        drafts += self._one(handle, session, _message(line), (start, session.offset, line))
        return drafts

    def _one(self, handle: Handle, session: _Session, msg: dict, span: tuple | None) -> list[dict]:
        method = msg.get("method")
        if method is None:
            return self._answer(handle, session, msg)
        if "id" in msg:
            self._answer_server_request(session, msg)
            return []
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if "threadId" in params and params["threadId"] != session.thread_id:
            return []  # a notification about another thread: not this worker's conversation
        if method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            return self._adopt_turn(session, turn.get("id"), None)
        if method == "item/started":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if item.get("type") in ("commandExecution", "fileChange") and isinstance(item.get("id"), str):
                with session.lock:
                    session.items[item["id"]] = item
            return []
        if method == "serverRequest/resolved":
            with session.lock:
                for approval_id, bound in list(session.bindings.items()):
                    if bound["rpc_id"] == params.get("requestId"):
                        del session.bindings[approval_id]  # the server stopped waiting: no answer is written
            return []
        if method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str) and span is not None:
                return [{"type": "engine.output", "payload": {"stream": "stdout", "text": item["text"],
                                                               "offset_start": span[0], "offset_end": span[1]}}]
            return []
        if method == "thread/tokenUsage/updated":
            if isinstance(params.get("turnId"), str) and isinstance(params.get("tokenUsage"), dict):
                with session.lock:
                    session.usage[params["turnId"]] = params["tokenUsage"]
            return []
        if method == "account/rateLimits/updated":
            return self._rate_limits(session, params.get("rateLimits"))
        if method == "model/rerouted":
            if isinstance(params.get("toModel"), str) and params["toModel"]:
                session.model = params["toModel"]
            return []
        if method == "turn/completed":
            return self._turn_completed(handle, session, params)
        return []

    def _answer(self, handle: Handle, session: _Session, msg: dict) -> list[dict]:
        """The server's answer to one of our requests. Only an answer we are waiting for counts."""

        rid = msg.get("id")
        with session.lock:
            record = session.pending.pop(rid, None) if isinstance(rid, int) else None
        if record is None:
            return []
        if "error" in msg and msg["error"] is not None:
            if record[0] == "turn/start":
                return self._turn_refused(handle, session, record[2], msg["error"])
            return []  # a refused steer or interrupt: no acceptance, and the turn goes on
        result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
        if record[0] == "turn/start":
            turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
            if not isinstance(turn.get("id"), str) or not turn["id"]:
                return []
            drafts = self._adopt_turn(session, turn["id"], record[2])
            return [_accepted(record[1], record[2], "new_turn"), *drafts]
        if record[0] == "turn/steer":
            with session.lock:
                current = session.engine_turn.get(record[2])
            if isinstance(result.get("turnId"), str) and result["turnId"] == current:
                return [_accepted(record[1], record[2], "mid_turn")]
        return []

    def _adopt_turn(self, session: _Session, engine_turn: object, core_turn: str | None) -> list[dict]:
        """Learn the engine's id of the turn this worker began, then write what waited for it."""

        if not isinstance(engine_turn, str) or not engine_turn:
            return []
        with session.lock:
            if core_turn is None:  # turn/started: the turn whose start is still unanswered, if exactly one
                waiting = [t for t in session.held if t not in session.engine_turn]
                if len(waiting) != 1:
                    return []
                core_turn = waiting[0]
            known = session.engine_turn.get(core_turn)
            if known is not None:
                if known != engine_turn:
                    raise c.RouteError(c.PROTOCOL_ERROR, "the engine reported two ids for one turn")
                return []
            session.engine_turn[core_turn] = engine_turn
            session.core_turn[engine_turn] = core_turn
        self._release_held(session, core_turn)
        return []

    def _turn_refused(self, handle: Handle, session: _Session, turn: str, error: object) -> list[dict]:
        error = error if isinstance(error, dict) else {}
        with session.lock:
            session.held.pop(turn, None)
        self.turn_ended(handle, turn)
        code = _code_of(error)
        drafts = []
        if code == c.PLAN_LIMIT:
            drafts.append(self._limit_draft(session))
        drafts.append({"type": "result", "payload": {"code": code, "detail": "the engine refused the turn"}})
        return drafts

    def _turn_completed(self, handle: Handle, session: _Session, params: dict) -> list[dict]:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        engine_turn = turn.get("id")
        with session.lock:
            core_turn = session.core_turn.get(engine_turn) if isinstance(engine_turn, str) else None
            if core_turn is None:
                return []  # not a turn this worker began (or one it already ended)
            del session.core_turn[engine_turn]
            session.held.pop(core_turn, None)
            usage = session.usage.pop(engine_turn, None)
            seen, session.bucket_limit = session.bucket_limit, None  # a window seen in this turn
            for approval_id, bound in list(session.bindings.items()):
                if bound["turn"] == engine_turn:
                    del session.bindings[approval_id]  # the turn is over: its pending requests are gone
        self.turn_ended(handle, core_turn)
        drafts = [self._usage_draft(session, usage)]
        status = turn.get("status")
        if status == "completed":
            drafts.append({"type": "result", "payload": {"code": None, "detail": ""}})
            return drafts
        if status == "interrupted":
            drafts.append({"type": "result", "payload": {"code": "interrupted", "detail": "the turn was interrupted"}})
            return drafts
        error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
        code = _code_of(error)
        if code in (c.ENGINE_ERROR, c.SHORT_FAULT) and seen is not None:
            code = c.PLAN_LIMIT  # an exhausted window was reported in this turn: the failure is that limit
        if code == c.PLAN_LIMIT:
            drafts.append(self._limit_draft(session, seen))
        drafts.append({"type": "result", "payload": {"code": code, "detail": "the turn failed"}})
        return drafts

    def _usage_draft(self, session: _Session, usage: dict | None) -> dict:
        total = usage.get("total") if isinstance(usage, dict) else None
        payload: dict = {"connection_id": session.connection_id, "model": session.model, "bucket": None,
                         "span": "session_total" if isinstance(total, dict) else "turn"}
        for ours, theirs in _USAGE_FIELDS:
            value = total.get(theirs) if isinstance(total, dict) else None
            if type(value) is int and value >= 0:
                payload[ours] = {"value": value, "quality": "known", "provenance": "observed"}
            else:
                payload[ours] = dict(_UNKNOWN)
        payload["cost_usd"] = dict(_UNKNOWN)  # Codex reports no cost
        return {"type": "usage", "payload": payload}

    def _rate_limits(self, session: _Session, snapshot: object) -> list[dict]:
        if not isinstance(snapshot, dict):
            return []
        hit = [w for w in (snapshot.get("primary"), snapshot.get("secondary"))
               if isinstance(w, dict) and type(w.get("usedPercent")) in (int, float) and w["usedPercent"] >= 100]
        if not hit:
            return []
        resets = [w["resetsAt"] for w in hit if type(w.get("resetsAt")) is int]
        bucket = snapshot.get("limitId") if isinstance(snapshot.get("limitId"), str) and snapshot["limitId"] else None
        with session.lock:
            session.bucket_limit = {"bucket": bucket, "reset_at": _timestamp(max(resets)) if resets else None}
        return [self._limit_draft(session)]

    def _limit_draft(self, session: _Session, seen: dict | None = None) -> dict:
        seen = seen or session.bucket_limit or {"bucket": None, "reset_at": None}
        return {"type": "limit", "payload": {"connection_id": session.connection_id, "scope": "connection",
                                             "kind": "plan_limit", "model": None, "bucket": seen["bucket"],
                                             "reset_at": seen["reset_at"]}}

    def _answer_server_request(self, session: _Session, msg: dict) -> None:
        tool = APPROVAL_REQUESTS.get(msg.get("method"))
        if tool is None:
            session.write({"id": msg["id"], "error": {"code": -32601, "message": "unsupported"}})
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        approval_id = self._ask(session, msg["id"], tool, params) if self._approvals is not None else None
        if approval_id is None:
            session.write({"id": msg["id"], "result": dict(DECLINE)})

    def _ask(self, session: _Session, rpc_id: object, tool: str, params: dict) -> str | None:
        """One approval through the port; None when there is none to wait for (then the caller declines)."""

        thread, turn, item_id = params.get("threadId"), params.get("turnId"), params.get("itemId")
        if thread != session.thread_id or not isinstance(turn, str) or not isinstance(item_id, str):
            return None
        with session.lock:
            item = dict(session.items.get(item_id) or {})
        tool_input = {k: v for k, v in params.items() if k not in ("threadId", "turnId", "itemId", "startedAtMs")}
        if tool == "fileChange" and "changes" in item:
            tool_input["changes"] = item["changes"]
        if tool == "commandExecution" and "command" not in tool_input and isinstance(item.get("command"), str):
            tool_input["command"] = item["command"]
        cwd = params.get("cwd") if isinstance(params.get("cwd"), str) and params.get("cwd") else None
        effect = {"kind": "engine_tool", "engine": ENGINE, "session_id": session.session_id,
                  "run_id": session.run_id, "tool": tool, "cwd": cwd, "input": tool_input}
        binding = {"run_id": session.run_id, "engine": ENGINE, "engine_request_id": json.dumps(rpc_id),
                   "bound_at": _timestamp_ms(self._wall())}
        try:
            approval_id = self._approvals.request(effect, binding)
        except Exception:  # noqa: BLE001 - any failure of the port: deny now, never leave the engine waiting
            return None
        if not isinstance(approval_id, str) or not approval_id:
            return None
        with session.lock:
            if approval_id in session.bindings:
                return None
            session.bindings[approval_id] = {"rpc_id": rpc_id, "thread": thread, "turn": turn, "item": item_id}
        return approval_id

    def _answer_approval(self, session: _Session, approval_id: str, decision: str) -> None:
        """Write the queue's answer on the request id of that same pending call, once."""

        with session.lock:
            bound = session.bindings.pop(approval_id, None)
        if bound is None:  # unknown, answered already, resolved by the server, or of a turn that ended
            raise DriverStateError(f"{approval_id} names no request this worker is waiting on")
        session.write({"id": bound["rpc_id"], "result": dict(_DECISIONS[decision])})


class _RpcError(Exception):
    def __init__(self, error: dict) -> None:
        super().__init__(str(error.get("code")))
        self.error = error


def _session(handle: Handle) -> _Session:
    session = getattr(handle, "_codex", None)
    if not isinstance(session, _Session):
        raise DriverStateError("this handle has no Codex worker")
    return session


def _message(line: str) -> dict:
    try:
        msg = json.loads(line)
    except ValueError:
        raise c.RouteError(c.PROTOCOL_ERROR, "stdout line is not JSON") from None
    if not isinstance(msg, dict):
        raise c.RouteError(c.PROTOCOL_ERROR, "stdout line is not a JSON object")
    return msg


def _code_of(error: dict) -> str:
    info = _error_info(error)
    if info in _AUTH_ERRORS:
        return c.AUTH_FAILED
    if info in _LIMIT_ERRORS:
        return c.PLAN_LIMIT
    if info is None and c.sounds_like_quota(error.get("message")):
        return c.SHORT_FAULT
    return c.ENGINE_ERROR


def _accepted(delivery: str, turn: str, how: str) -> dict:
    return {"type": "input.accepted", "payload": {"delivery_id": delivery, "turn_id": turn, "how": how}}


def _timestamp_ms(epoch: float) -> str:
    moment = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _timestamp(epoch: int) -> str | None:
    """Codex's resetsAt (Unix seconds) as common.v1's timestamp: RFC 3339, three decimals, Z."""

    try:
        moment = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# -- registration (not called by anything yet) ------------------------------------------------------------
def factory(launcher, clock, credentials, environ, **options):
    """The engine table's factory shape (SPINE's S3). It returns today's one-turn ``CodexAdapter`` unchanged,
    with this module's session driver beside it as ``.session``, so a registration changes nothing for the
    one-turn path. Both hand the launcher, as argv[0], the binary option when composition gave one and
    ``DEFAULT_EXECUTABLE`` otherwise, and nothing else."""

    from recursion_core.routes.codex import CodexAdapter

    binary = options.get("binary") or DEFAULT_EXECUTABLE
    adapter = CodexAdapter(launcher, clock, credentials, environ, binary=binary)
    adapter.session = CodexDriver(launcher, clock, binary=binary)
    return adapter


def register(hooks) -> None:
    """Replace the seeded ``codex`` factory. NOT CALLED YET: the Liaison's rulings of Nov 14 hold it until
    SPINE's follow-up to #370 adds ``default_executable`` to the engine table, and no registrant module of
    this lane is in SPINE's list. When it lands, the one edit is to add ``default_executable=DEFAULT_EXECUTABLE``
    to this call."""

    hooks.engines.register(ENGINE, factory, replace=True)
