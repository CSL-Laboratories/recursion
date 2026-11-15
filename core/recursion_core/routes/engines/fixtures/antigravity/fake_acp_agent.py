# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""A fake ACP agent for the shared client's tests. SYNTHETIC: written by hand from the pinned ACP schema
(``acp.SCHEMA_PIN``) and the agent behaviour the vault records describe, not captured from any agent.

It is an ``EngineProcess`` (``routes.launch``): the client writes lines to it with ``send`` and reads its
lines with ``read_line``. It runs in the test's own process and starts no thread. It answers
``initialize``, ``authenticate``, ``session/new`` and ``session/load`` itself, and runs one SCRIPT per
``session/prompt``, a list of steps:

    ("chunk", text)                 a session/update agent_message_chunk
    ("update", update)              any session/update (the ``update`` object as given)
    ("ask", tool_call, options)     a session/request_permission; the script waits for the client's answer
    ("wait_cancel",)                the script waits until session/cancel arrives
    ("end", stop_reason)            answer the prompt
    ("error", code, message)        answer the prompt with a JSON-RPC error
    ("die",)                        stdout ends (after what is already queued)
    ("raw", line)                   write ``line`` as it is
    ("stale_response",)             a response to an id the client never sent
    ("fs_read", path)               ask the client for fs/read_text_file (it must refuse)

Steps run as soon as the prompt arrives, until a step that waits. So a script ``chunk, end`` has queued
its answer before the client has read the chunk: a cancel sent after reading the chunk races an answer
that is already on its way, which is the race the client's cancel rule is about.

With ``blocking=False`` (the default) a ``read_line`` that finds nothing queued, while the agent waits for
the client, advances the fake clock by the timeout and raises ``ReadTimeout``: time passes only when the
client waits, and no test sleeps. With ``blocking=True`` it waits on a condition for a line written by
another thread; ``watchdog_s`` only turns a hang into a failing test (the line never came).
"""

from __future__ import annotations

import json
import threading
from collections import deque

from recursion_core.routes.launch import ReadTimeout

# The method ids Antigravity's "Other editors (ACP)" page lists (read Nov 14, 2026, through a fetch tool
# that summarises; T3 Code 09e8de9c names the same four in apps/server/src/provider/antigravityAuthSupport.ts).
# terminal-login is offered only to clients that advertise terminal auth, which this client never does.
ANTIGRAVITY_METHODS = ("oauth-personal", "oauth-business", "gemini-api-key", "agent-platform")


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class FakeAcpAgent:
    def __init__(self, scripts=(), *, clock: FakeClock | None = None, methods=ANTIGRAVITY_METHODS,
                 accept_methods=None, load_session: bool = False, replay=(), protocol_version: int = 1,
                 session_id: str = "fake-session-1", blocking: bool = False, watchdog_s: float = 10.0) -> None:
        self.clock = clock if clock is not None else FakeClock()
        self._scripts = deque(list(s) for s in scripts)
        self._methods = tuple(methods)
        self._accept = set(self._methods if accept_methods is None else accept_methods)
        self._load_session = load_session
        self._replay = tuple(replay)
        self._protocol_version = protocol_version
        self._session_id = session_id
        self._blocking, self._watchdog_s = blocking, watchdog_s
        self._cond = threading.Condition()
        self._out: deque = deque()
        self._dead = False
        self._steps: deque = deque()
        self._prompt_id = None
        self._waiting = None        # ("permission", id) | ("cancel",) | None
        self._next_ask = 0
        self._idle_reads = 0
        # What the client did, for the tests to read.
        self.received: list = []           # every message the client wrote, parsed
        self.authenticated: list = []      # every methodId the client asked to authenticate with
        self.cancels = 0
        self.permission_answers: dict = {}  # agent request id -> the client's result (or error)
        self.refused: list = []            # agent requests the client answered with an error
        self.stdin_closed = False
        self.fenced: list = []

    # -- what the client writes ---------------------------------------------------------------------------
    def send(self, line: str) -> None:
        with self._cond:
            if self._dead or self.stdin_closed:
                raise BrokenPipeError("the fake agent's input is closed")
            line.encode("utf-8")   # as a real pipe in text mode: a line that is not UTF-8 cannot be written
            message = json.loads(line)
            self.received.append(message)
            self._handle(message)
            self._cond.notify_all()

    def close_stdin(self) -> None:
        with self._cond:
            self.stdin_closed = True
            self._dead = True      # the fake exits on EOF, after what it already queued
            self._cond.notify_all()

    def fence(self, reason: str) -> None:
        with self._cond:
            self.fenced.append(reason)
            self._dead = True
            self._out.clear()
            self._cond.notify_all()

    # -- what the client reads ----------------------------------------------------------------------------
    def read_line(self, timeout_s: float):
        with self._cond:
            if not self._out and not self._dead and self._blocking:
                self._cond.wait_for(lambda: self._out or self._dead, timeout=self._watchdog_s)
                if not self._out and not self._dead:
                    self._dead = True   # the watchdog: the line the test waited for never came
                    raise AssertionError("fake agent watchdog: nothing to read")
            if self._out:
                self._idle_reads = 0
                return self._out.popleft()
            if self._dead:
                return None
            # A client that keeps asking with no time left, while nothing can arrive, would spin for ever:
            # fail it instead of hanging the test.
            self._idle_reads = self._idle_reads + 1 if timeout_s <= 0 else 0
            if self._idle_reads > 1000:
                self._dead = True
                raise AssertionError("fake agent: the client spins on reads with no time left")
            self.clock.advance(timeout_s)
            raise ReadTimeout()

    # -- the agent ----------------------------------------------------------------------------------------
    def _emit(self, message: dict) -> None:
        self._out.append(json.dumps(message, separators=(",", ":")))

    def _answer(self, rpc_id, result) -> None:
        self._emit({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def _error(self, rpc_id, code: int, message: str) -> None:
        self._emit({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})

    def _update(self, update: dict) -> None:
        self._emit({"jsonrpc": "2.0", "method": "session/update",
                    "params": {"sessionId": self._session_id, "update": update}})

    def _handle(self, message: dict) -> None:
        method, rpc_id = message.get("method"), message.get("id")
        if method is None:
            # A response to one of the agent's own requests.
            if self._waiting is not None and self._waiting[0] == "permission" and rpc_id == self._waiting[1]:
                self.permission_answers[rpc_id] = message.get("result")
                self._waiting = None
                self._run()
            elif "error" in message:
                self.refused.append(message)
            else:
                self.permission_answers[rpc_id] = message.get("result")
            return
        params = message.get("params") or {}
        if method == "initialize":
            self._answer(rpc_id, {
                "protocolVersion": self._protocol_version,
                "agentCapabilities": {"loadSession": self._load_session},
                "authMethods": [{"id": m, "name": m} for m in self._methods],
                "agentInfo": {"name": "fake ACP agent (synthetic)", "version": "0"},
            })
        elif method == "authenticate":
            self.authenticated.append(params.get("methodId"))
            if params.get("methodId") in self._accept:
                self._answer(rpc_id, {})
            else:
                self._error(rpc_id, -32000, "Authentication required")
        elif method == "session/new":
            self._answer(rpc_id, {"sessionId": self._session_id})
        elif method == "session/load":
            if not self._load_session:
                self._error(rpc_id, -32601, "session/load is not offered")
                return
            self._session_id = params.get("sessionId")   # the loaded session is the one the agent speaks for
            for update in self._replay:
                self._update(update)
            self._answer(rpc_id, {})
        elif method == "session/prompt":
            self._prompt_id = rpc_id
            self._steps = deque(self._scripts.popleft() if self._scripts else [("end", "end_turn")])
            self._run()
        elif method == "session/cancel":
            self.cancels += 1
            if self._waiting == ("cancel",):
                self._waiting = None
                self._run()
        elif rpc_id is not None:
            self._error(rpc_id, -32601, f"{method} is not offered by the fake")

    def _run(self) -> None:
        while self._steps and self._waiting is None and not self._dead:
            step = self._steps.popleft()
            kind = step[0]
            if kind == "chunk":
                self._update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": step[1]}})
            elif kind == "update":
                self._update(step[1])
            elif kind == "ask":
                self._next_ask += 1
                ask_id = f"perm-{self._next_ask}"
                self._emit({"jsonrpc": "2.0", "id": ask_id, "method": "session/request_permission",
                            "params": {"sessionId": self._session_id, "toolCall": step[1], "options": step[2]}})
                self._waiting = ("permission", ask_id)
            elif kind == "wait_cancel":
                self._waiting = ("cancel",)
            elif kind == "end":
                self._answer(self._prompt_id, {"stopReason": step[1]})
            elif kind == "error":
                self._error(self._prompt_id, step[1], step[2])
            elif kind == "die":
                self._dead = True
            elif kind == "raw":
                self._out.append(step[1])
            elif kind == "stale_response":
                self._answer(9_999, {"stopReason": "end_turn"})
            elif kind == "fs_read":
                self._next_ask += 1
                self._emit({"jsonrpc": "2.0", "id": f"fs-{self._next_ask}", "method": "fs/read_text_file",
                            "params": {"sessionId": self._session_id, "path": step[1]}})
            else:
                raise ValueError(f"unknown fake step {kind!r}")


# Permission options as agents send them (kinds from the pinned schema; ids made up).
OPTIONS_ALL = [
    {"optionId": "proceed_always", "name": "Allow always", "kind": "allow_always"},
    {"optionId": "proceed_once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
]
OPTIONS_ALWAYS_ONLY = [
    {"optionId": "proceed_always", "name": "Allow always", "kind": "allow_always"},
    {"optionId": "reject_always", "name": "Never", "kind": "reject_always"},
]
TOOL_CALL = {"toolCallId": "call-1", "title": "Run echo hi", "kind": "execute", "status": "pending",
             "rawInput": {"command": "echo hi"}}
