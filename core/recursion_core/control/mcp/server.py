"""The Control MCP server, protocol revision 2025-06-18 only (the older of the two decided eras).

One ``LegacySession`` serves one client over one stdio pair (``serve``). What it answers:

``initialize``
    Answers ``protocolVersion: "2025-06-18"`` whatever the client asked for: 2025-06-18 lifecycle,
    "Version Negotiation": a server that supports the requested version answers it, otherwise it answers
    another version it supports, and this server supports exactly one. The client decides whether to go
    on. As the 2025-06-18 schema requires, ``protocolVersion`` is a non-empty string, ``capabilities`` an
    object and ``clientInfo`` an object with a string ``name`` and ``version``; anything else is
    ``-32602``. A refused ``initialize`` leaves the session uninitialized; a second one is refused.
    Capabilities: ``tools`` only, ``listChanged: false`` (the list never changes while the process lives).
``server/discover`` and every other unknown method
    ``-32601`` Method not found, before and after ``initialize``, and the process keeps serving. A
    dual-era client probes a stdio server with ``server/discover`` and falls back to ``initialize`` on
    any error that is not a recognised modern error (2026-07-28 basic/versioning, "Backward Compatibility
    with Initialization-Based Versions"), so the probe must neither be answered as modern nor end the
    process.
``ping``
    ``{}``, in any state.
``tools/list``, ``tools/call``
    ``-32600`` until ``initialize`` has been answered. ``tools/list`` has one page; a ``cursor`` (this
    server never issues one) is ``-32602``. ``tools/call``: an unknown tool, or ``arguments`` that break
    the tool's ``inputSchema`` (``arguments`` may be left out, which is ``{}``; when present it is checked
    as it is, so ``null`` is refused), are ``-32602`` and nothing reaches the daemon. Otherwise the call is
    admitted (below) and its answer is ``tools.forward``'s, which answers as ``control.mcp_tools.call_tool``
    does: the control API's answer as ``structuredContent``, the same JSON as text, ``isError`` true unless
    the code is a success code.
``notifications/initialized``
    Accepted; requests after the ``initialize`` answer are served whether or not it has arrived.
``notifications/cancelled``
    Names one request by its id (a string or an integer; ``"1"`` and ``1`` are different requests).
    A call still WAITING for a slot is dropped and never sent. A call ADMITTED (it holds a slot, its worker
    has not sent it yet) is never sent either: the worker decides to send under the session's lock and
    finds it cancelled. A call SENT to the daemon has its answer dropped; the call is not withdrawn. A call
    whose answer is already being written is not affected: the answer was committed before the
    cancellation arrived, as for a call that has finished (the specification lets a receiver ignore a
    cancellation that comes too late). Which of these happened is decided under the session's lock, so a
    cancellation and a send, or a cancellation and an answer, never both win, and the logged outcome
    (``never_sent``, ``answer_dropped``, ``answer_already_written``) is what the daemon saw. No Stop is ever
    sent for a cancellation, and a cancellation for an id that is not in flight is ignored. A cancellation
    names an id, not a request: the 2025-06-18 basic protocol says a request id MUST NOT have been used
    before in the same session, and a client that reuses a finished id anyway, while a late cancellation of
    the earlier request is on its way, cancels the newer call (logged as that call's outcome).

The MCP face has no Stop path of its own: ``recursion_run_stop`` forwards to ``run.stop`` and answers
what it answered (``stopped`` only when cleanup is confirmed, otherwise ``pending``). A call that raises,
or whose answer is not an object, answers ``control_api_unavailable`` with ``delivered: "unknown"``.

ADMISSION AND OVERLOAD. The reader never waits for capacity, nor for stdout (REPLIES), so a Stop or a
cancellation behind a busy moment is still read. A call takes one of ``max_in_flight`` ordinary slots; the
stop tool may also take one of ``reserved_stop`` extra slots (the daemon keeps reserved connections for
Stop in the same way). A slot is held until the call's answer has been written (or dropped), so the
number of worker threads and of answers waiting for stdout is bounded by the slots. A call that finds no
slot waits in a queue:
ordinary calls in one of ``max_waiting`` places, stop calls in one of ``max_waiting_stops`` places of their
own, which are served first. A call that finds its queue full is refused at once with ``-32000`` "Server
busy" and ``data.delivered: false``: nothing was sent, so it is safe to send again. A request whose id is
still in flight (waiting, running or being answered) is refused.

END OF INPUT. The client ending stdin ends reading: calls already admitted are run (waiting ones as slots
free) and answered while stdout stays open, then ``serve`` returns 0. An answer is never dropped, so a
client that ends stdin and stops reading stdout without shutting it holds ``serve`` until it reads again
or stdout fails. It never stops a run: the runs it started go on (detach, not stop).

REPLIES. The reader never writes to stdout. The answers it owes (every refusal, ``Server busy`` included,
``ping``, ``initialize`` and ``tools/list``) go to ``reply``, which takes an answer without waiting and
says whether it took it; ``serve`` gives it a ``replies.ReplyQueue``, written by a thread of its own. So
however slow stdout is, the reader goes on reading: a Stop behind a busy answer is admitted to its
reserved slot and sent, and a cancellation is applied. A call's answer is written by its worker, which
holds the call's slot until then. The reply queue is bounded; an answer it cannot take (only a client that
has stopped reading stdout while it goes on writing fills it) is dropped and its request treated as never
received: each of those requests sent nothing to the daemon, and a dropped ``initialize`` answer leaves
the session uninitialized (``replies.py``, EXHAUSTION). An in-process session given no ``reply`` writes
those answers with ``write``.

DIAGNOSTICS. Nor does any thread of ``serve`` wait for stderr while it serves. Every event for ``log`` (a
cancellation, a response the client sent, ``initialize``, a stretch of dropped answers, ``output_lost``,
the last line) goes to a ``diagnostics.DiagnosticQueue``, which takes it without waiting and delivers it on
a thread of its own; it is bounded, and an event that does not fit is dropped and counted. So a client
that captures stderr and stops reading it never stops the reader either. At its end ``serve`` waits for
the events still queued, its last line included, for at most ``max_diagnostic_drain_s`` (2 s) and then
returns, so a client that never reads stderr does not keep the process alive after end of input; what
stderr has not taken by then is lost. An in-process session calls its ``log`` directly.

OUTPUT FAILURE. When a write to stdout fails, the session stops forwarding: no frame read afterwards and no
waiting call is sent, a call that has been admitted but not yet sent is not sent, the reader's answers not
yet written are discarded, calls already sent are left to finish at the daemon (no Stop is sent in their
place), and ``serve`` returns without waiting for more input. Reading runs on its own thread for that
reason. The failure is known at the first write that fails: a call read before then is sent if its worker
starts before then. A ``write`` that raises instead of failing quietly (an in-process session's) counts
as a failed output too: ``{"event": "write_failed", "detail": ...}`` is logged and nothing more is sent.
"""

from __future__ import annotations

import collections
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, BinaryIO

from . import jsonrpc
from .framing import MAX_INT_DIGITS, NOT_JSON, NOT_UTF8, NUMBER_TOO_LONG, TOO_LARGE, Frame, FrameWriter, read_frames
from .diagnostics import DIAGNOSTIC_DRAIN_S, MAX_PENDING_DIAGNOSTIC_BYTES, DiagnosticQueue, check_seconds
from .replies import MAX_PENDING_REPLY_BYTES, ReplyQueue
from .tools import Tool, check_arguments, forward, tool_table

LEGACY_PROTOCOL = "2025-06-18"
SERVER_NAME = "recursion-control"
SERVER_TITLE = "Recursion control"
SERVER_VERSION = "0.0.0"  # the repository carries no package version yet
INSTRUCTIONS = (
    "Starts, reads and stops agent runs on this machine's Recursion daemon. recursion_run_stop answers "
    "\"stopped\" only when cleanup is confirmed and \"pending\" otherwise; read recursion_run_status "
    "before retrying a stop or a start."
)

MAX_IN_FLIGHT = 8
RESERVED_STOP = 2
MAX_WAITING = 16
MAX_WAITING_STOPS = 8
SERVER_BUSY = -32000

UNAVAILABLE = "control_api_unavailable"
_KNOWN_AFTER_INIT = frozenset({"tools/list", "tools/call"})
# A call's states: WAITING (no slot yet), ADMITTED (holds a slot, not sent), SENT (sent to the daemon),
# ANSWERING (its answer is being written). Each change is made under the session's lock.
WAITING, ADMITTED, SENT, ANSWERING = "waiting", "admitted", "sent", "answering"

Log = Callable[[dict], None]


def _quiet(_event: dict) -> None:
    pass


def _always_open() -> bool:
    return True


def negotiate(requested: str) -> str:
    """The version this server answers to ``requested``: always the one it speaks."""

    return LEGACY_PROTOCOL


def _unknown_outcome(exc: BaseException) -> dict:
    """``tools/call`` answer for a call that raised: delivery unknown, nothing claimed."""

    answer = {"code": UNAVAILABLE, "delivered": "unknown", "detail": f"mcp:{type(exc).__name__}"}
    return {"content": [{"type": "text", "text": json.dumps(answer, sort_keys=True)}],
            "structuredContent": answer, "isError": True}


class _Busy(Exception):
    pass


class _OutputLost(Exception):
    pass


@dataclass(slots=True, eq=False)
class _Call:
    request_id: Any
    tool: Tool
    state: str = WAITING
    holds_slot: bool = False
    reserved: bool = False
    cancelled: bool = False
    ready: bool = False  # its arguments are attached: it may be started
    arguments: Any = None


class LegacySession:
    """One client's session. ``api`` is anything with ``call(method, params) -> dict``; ``write`` puts one
    call's answer on the wire (from the call's worker); ``reply`` takes one answer the reader owes without
    waiting and returns whether it took it (REPLIES; ``write`` when left out); ``output_open()`` says
    whether stdout still takes answers."""

    def __init__(self, api: Any, write: Callable[[dict], Any], *, reply: Callable[[dict], bool] | None = None,
                 tools: Iterable[Tool] | None = None,
                 max_in_flight: int = MAX_IN_FLIGHT, reserved_stop: int = RESERVED_STOP,
                 max_waiting: int = MAX_WAITING, max_waiting_stops: int = MAX_WAITING_STOPS,
                 log: Log | None = None, output_open: Callable[[], bool] = _always_open) -> None:
        if max_in_flight < 1 or min(reserved_stop, max_waiting, max_waiting_stops) < 0:
            raise ValueError("max_in_flight must be positive and the other limits not negative")
        self._api = api
        self._write = write
        self._reply = reply if reply is not None else self._write_now
        self._log = log or _quiet
        self._output_open = output_open
        self._tools = {t.name: t for t in (tool_table() if tools is None else tools)}
        self._descriptors = [t.descriptor for t in self._tools.values()]
        self._max_in_flight, self._reserved_stop = max_in_flight, reserved_stop
        self._max_waiting, self._max_waiting_stops = max_waiting, max_waiting_stops
        self._cond = threading.Condition()
        self._in_flight: dict[Any, _Call] = {}
        self._waiting: collections.deque[_Call] = collections.deque()
        self._waiting_stops: collections.deque[_Call] = collections.deque()
        self._general = 0
        self._reserved = 0
        self._output_lost = False
        self.initialized = False

    def _write_now(self, message: dict) -> bool:
        """``reply`` of an in-process session: the answer is written at once, by the reader."""

        self._write(message)
        return True

    # -- input --------------------------------------------------------------------------------------
    def receive(self, frame: Frame) -> None:
        if frame.error == TOO_LARGE:
            self._reply(jsonrpc.error(None, jsonrpc.INVALID_REQUEST, "Invalid Request: message too large"))
            return
        if frame.error == NUMBER_TOO_LONG:
            self._reply(jsonrpc.error(None, jsonrpc.INVALID_REQUEST,
                                      f"Invalid Request: an integer longer than {MAX_INT_DIGITS} digits"))
            return
        if frame.error in (NOT_UTF8, NOT_JSON):
            detail = "not UTF-8" if frame.error == NOT_UTF8 else "not JSON"
            self._reply(jsonrpc.error(None, jsonrpc.PARSE_ERROR, f"Parse error: {detail}"))
            return
        message = jsonrpc.classify(frame.message)
        if isinstance(message, jsonrpc.Invalid):
            self._reply(jsonrpc.error(message.id, message.code, message.message))
        elif isinstance(message, jsonrpc.Response):
            self._log({"event": "ignored_response"})
        elif isinstance(message, jsonrpc.Notification):
            self._notification(message)
        else:
            self._request(message)

    def _notification(self, note: jsonrpc.Notification) -> None:
        if note.method == "notifications/cancelled":
            request_id = note.params.get("requestId")
            if jsonrpc.valid_id(request_id):
                self._cancel(request_id)
        # notifications/initialized and every other notification need nothing.

    def _cancel(self, request_id: Any) -> None:
        with self._cond:
            call = self._in_flight.get(request_id)
            if call is None:
                outcome = "not_in_flight"
            elif call.state == WAITING:
                (self._waiting_stops if call.tool.is_stop else self._waiting).remove(call)
                del self._in_flight[request_id]
                self._cond.notify_all()
                outcome = "never_sent"
            elif call.state == ADMITTED:  # its worker finds it cancelled before sending (_run)
                call.cancelled = True
                outcome = "never_sent"
            elif call.state == SENT:
                call.cancelled = True
                outcome = "answer_dropped"
            else:
                outcome = "answer_already_written"
        self._log({"event": "cancelled", "outcome": outcome})

    def _request(self, req: jsonrpc.Request) -> None:
        with self._cond:
            busy = req.id in self._in_flight
        if busy:
            self._reply(jsonrpc.error(req.id, jsonrpc.INVALID_REQUEST,
                                      "Invalid Request: this id belongs to a request still in flight"))
            return
        if req.method == "initialize":
            self._initialize(req)
        elif req.method == "ping":
            self._reply(jsonrpc.result(req.id, {}))
        elif req.method in _KNOWN_AFTER_INIT:
            if not self.initialized:
                self._reply(jsonrpc.error(req.id, jsonrpc.INVALID_REQUEST,
                                          "Invalid Request: not initialized; send initialize first"))
            elif req.method == "tools/list":
                self._tools_list(req)
            else:
                self._tools_call(req)
        else:
            self._reply(jsonrpc.error(req.id, jsonrpc.METHOD_NOT_FOUND, f"Method not found: {req.method}"))

    # -- lifecycle ------------------------------------------------------------------------------------
    def _initialize(self, req: jsonrpc.Request) -> None:
        if self.initialized:
            self._reply(jsonrpc.error(req.id, jsonrpc.INVALID_REQUEST, "Invalid Request: already initialized"))
            return
        params = req.params
        requested, info = params.get("protocolVersion"), params.get("clientInfo")
        problem = None
        if not isinstance(requested, str) or not requested:
            problem = "protocolVersion must be a non-empty string"
        elif not isinstance(params.get("capabilities"), dict):
            problem = "capabilities must be an object"
        elif not (isinstance(info, dict) and isinstance(info.get("name"), str) and isinstance(info.get("version"), str)):
            problem = "clientInfo must be an object with a string name and version"
        if problem is not None:
            self._reply(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS, f"Invalid params: {problem}"))
            return
        answered = negotiate(requested)
        taken = self._reply(jsonrpc.result(req.id, {
            "protocolVersion": answered,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": SERVER_TITLE, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }))
        if not taken:  # the answer was dropped (REPLIES): the client never sees it, so nothing changes
            return
        self.initialized = True
        self._log({"event": "initialize", "requested": requested, "answered": answered, "client": info["name"]})

    # -- tools ------------------------------------------------------------------------------------------
    def _tools_list(self, req: jsonrpc.Request) -> None:
        if "cursor" in req.params:
            self._reply(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS, "Invalid params: unknown cursor"))
            return
        self._reply(jsonrpc.result(req.id, {"tools": self._descriptors}))

    def _tools_call(self, req: jsonrpc.Request) -> None:
        name = req.params.get("name")
        if not isinstance(name, str):
            self._reply(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS, "Invalid params: name must be a string"))
            return
        tool = self._tools.get(name)
        if tool is None:
            self._reply(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS, f"Unknown tool: {name}"))
            return
        arguments = req.params["arguments"] if "arguments" in req.params else {}
        try:
            problem = check_arguments(tool.input_schema, arguments)
        except Exception:  # noqa: BLE001 - a schema the checker cannot apply: refuse, send nothing, keep reading
            self._reply(jsonrpc.error(req.id, jsonrpc.INTERNAL_ERROR,
                                      "Internal error: the tool's input schema could not be applied", {"tool": name}))
            return
        if problem is not None:
            self._reply(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS, f"Invalid params: {problem}",
                                      {"tool": name}))
            return
        try:
            call = self._admit(req.id, tool)
        except _OutputLost:  # no answer could be seen: nothing is sent and nothing is written
            return
        except _Busy:
            self._reply(jsonrpc.error(req.id, SERVER_BUSY, "Server busy: this call was not sent; it may be sent again",
                                      {"delivered": False}))
            return
        with self._cond:
            call.arguments, call.ready = arguments, True
            start = [call] if call.state == ADMITTED else self._startable()
        for each in start:
            self._start(each)

    def _admit(self, request_id: Any, tool: Tool) -> _Call:
        """Record the call as in flight, holding a slot (``admitted``) or a place in its queue (``waiting``);
        never waits. ``_Busy`` when its queue is full, ``_OutputLost`` once stdout has failed."""

        with self._cond:
            if self._output_lost or not self._output_open():
                raise _OutputLost()
            call = _Call(request_id, tool)
            if self._take_slot(call):
                call.state = ADMITTED
            elif tool.is_stop and len(self._waiting_stops) < self._max_waiting_stops:
                self._waiting_stops.append(call)
            elif not tool.is_stop and len(self._waiting) < self._max_waiting:
                self._waiting.append(call)
            else:
                raise _Busy()
            self._in_flight[request_id] = call
            return call

    def _take_slot(self, call: _Call) -> bool:
        """Under the lock: an ordinary slot, else (for the stop tool) a reserved one."""

        if self._general < self._max_in_flight:
            self._general += 1
        elif call.tool.is_stop and self._reserved < self._reserved_stop:
            self._reserved += 1
            call.reserved = True
        else:
            return False
        call.holds_slot = True
        return True

    def _startable(self) -> list[_Call]:
        """Under the lock: waiting calls that can now take a slot, stop calls first; each is moved to admitted."""

        started: list[_Call] = []
        if self._output_lost:
            return started
        for queue in (self._waiting_stops, self._waiting):
            for call in list(queue):
                if not call.ready or not self._take_slot(call):
                    continue
                queue.remove(call)
                call.state = ADMITTED
                started.append(call)
        return started

    def _start(self, call: _Call) -> None:
        worker = threading.Thread(target=self._run, args=(call,), name="mcp-tools-call", daemon=True)
        try:
            worker.start()
        except RuntimeError:  # no thread could be started: nothing was sent to the daemon
            self._reply(jsonrpc.error(call.request_id, jsonrpc.INTERNAL_ERROR, "Internal error: no worker for this call"))
            self._finish(call)

    def _run(self, call: _Call) -> None:
        try:
            with self._cond:  # the send is decided here: a cancellation before this point means never sent
                send = not call.cancelled and not self._output_lost and self._output_open()
                if send:
                    call.state = SENT
            if send:
                try:
                    outcome = forward(self._api, call.tool, call.arguments)
                except BaseException as exc:  # noqa: BLE001 - an answer is owed; the call's effect is unknown
                    outcome = _unknown_outcome(exc)
                with self._cond:
                    answer = not call.cancelled
                    if answer:
                        call.state = ANSWERING
                if answer:
                    self._answer(call, outcome)
            # Cancelled before the send, or output already lost: the call is not sent at all.
        finally:
            self._finish(call)

    def _answer(self, call: _Call, outcome: dict) -> None:
        try:
            try:
                self._write(jsonrpc.result(call.request_id, outcome))
            except (TypeError, ValueError):  # an answer JSON cannot hold; it claims nothing either way
                self._write(jsonrpc.error(call.request_id, jsonrpc.INTERNAL_ERROR,
                                          "Internal error: the answer is not JSON"))
        except BaseException as exc:  # noqa: BLE001 - a write that raises anything is an output that failed
            self._log({"event": "write_failed", "detail": type(exc).__name__})
            self.output_lost()

    def _finish(self, call: _Call) -> None:
        """The call is over and its answer, if wanted, written: give its slot and id back, start what waits."""

        with self._cond:
            if call.holds_slot:
                if call.reserved:
                    self._reserved -= 1
                else:
                    self._general -= 1
                call.holds_slot = False
            self._in_flight.pop(call.request_id, None)
            start = self._startable()
            self._cond.notify_all()
        for each in start:
            self._start(each)

    # -- output failure and end of input ------------------------------------------------------------------
    def output_lost(self) -> None:
        """Stdout failed: send nothing more. Waiting calls are dropped unsent; running ones finish."""

        with self._cond:
            if self._output_lost:
                return
            self._output_lost = True
            dropped = list(self._waiting_stops) + list(self._waiting)
            self._waiting_stops.clear()
            self._waiting.clear()
            for call in dropped:
                self._in_flight.pop(call.request_id, None)
            self._cond.notify_all()
        self._log({"event": "output_lost", "dropped_unsent": len(dropped)})

    def in_flight(self) -> int:
        with self._cond:
            return len(self._in_flight)

    def close(self) -> None:
        """Wait until every admitted call has been run (or dropped) and its answer written (if wanted)."""

        with self._cond:
            while self._in_flight:
                self._cond.wait()


def serve(api: Any, instream: BinaryIO, outstream: BinaryIO, *, log: Log | None = None,
          tools: Iterable[Tool] | None = None, max_in_flight: int = MAX_IN_FLIGHT, reserved_stop: int = RESERVED_STOP,
          max_waiting: int = MAX_WAITING, max_waiting_stops: int = MAX_WAITING_STOPS,
          max_pending_reply_bytes: int = MAX_PENDING_REPLY_BYTES,
          max_pending_diagnostic_bytes: int = MAX_PENDING_DIAGNOSTIC_BYTES,
          max_diagnostic_drain_s: float = DIAGNOSTIC_DRAIN_S) -> int:
    """Serve one client until end of input or until stdout fails; returns the process exit status (0).

    Reading runs on a thread of its own, so that a failure of stdout ends the session even while the
    reader waits for input; the reader's answers are written by the reply queue's thread (REPLIES), and
    every event for ``log`` is delivered by a diagnostics thread (DIAGNOSTICS). An exception raised while
    reading (an interrupt included) is raised here at once; diagnostics not yet delivered then may be lost.
    ``max_diagnostic_drain_s`` is ``None`` for no limit or a number of seconds from 0 to a day
    (``diagnostics.check_seconds``); anything else is a ``ValueError`` before anything is read. The queues'
    bounds and the session's limits are refused before anything is read too: a ``ValueError`` for a number out
    of range, a ``TypeError`` for a value that is not a number.
    """

    check_seconds(max_diagnostic_drain_s, "max_diagnostic_drain_s")  # refused before anything is read
    diagnostics = DiagnosticQueue(log or _quiet, max_bytes=max_pending_diagnostic_bytes)
    log = diagnostics.offer
    done = threading.Event()
    failure: list[BaseException] = []
    writer = FrameWriter(outstream)
    replies = ReplyQueue(writer, max_bytes=max_pending_reply_bytes, log=log)
    session = LegacySession(api, writer.write, reply=replies.offer, tools=tools, log=log,
                            max_in_flight=max_in_flight, reserved_stop=reserved_stop, max_waiting=max_waiting,
                            max_waiting_stops=max_waiting_stops, output_open=lambda: not writer.broken)

    def output_failed() -> None:
        session.output_lost()  # the reply queue sees the failure at its next write (replies.py)
        done.set()

    writer.on_broken = output_failed
    diagnostics.start()
    replies.start()

    def read() -> None:
        try:
            for frame in read_frames(instream):  # after an output failure a frame is still read, and refused
                session.receive(frame)           # at admission: nothing is sent (LegacySession._admit)
        except BaseException as exc:  # noqa: BLE001 - carried to the serving thread and raised there
            failure.append(exc)
        finally:
            done.set()

    threading.Thread(target=read, name="mcp-stdin", daemon=True).start()
    done.wait()
    if failure:
        raise failure[0]
    session.close()
    replies.close()
    diagnostics.close(last={"event": "end_of_input" if not writer.broken else "output_failed",
                            "stdout_broken": writer.broken}, timeout=max_diagnostic_drain_s)
    return 0
