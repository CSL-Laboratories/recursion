"""The Control MCP server, protocol revision 2025-06-18 only (the older of the two decided eras).

One ``LegacySession`` serves one client over one stdio pair (``serve``). What it answers:

``initialize``
    Answers ``protocolVersion: "2025-06-18"`` whatever the client asked for: 2025-06-18 lifecycle,
    "Version Negotiation": a server that supports the requested version answers it, otherwise it answers
    another version it supports, and this server supports exactly one. The client decides whether to go
    on. ``protocolVersion`` must be a non-empty string; ``capabilities`` and ``clientInfo``, when present,
    must be objects. A refused ``initialize`` leaves the session uninitialized; a second ``initialize``
    is refused. Capabilities: ``tools`` only, ``listChanged: false`` (the list never changes while the
    process lives).
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
    server never issues one) is ``-32602``. ``tools/call``: an unknown tool or arguments that break the
    tool's ``inputSchema`` are ``-32602`` and nothing reaches the daemon; otherwise the call runs on a
    worker thread and its answer is ``tools.forward``'s, which answers as ``control.mcp_tools.call_tool``
    does: the control API's answer as ``structuredContent``, the same JSON as text, ``isError`` true
    unless the code is a success code.
``notifications/initialized``
    Recorded; requests after the ``initialize`` answer are served whether or not it has arrived.
``notifications/cancelled``
    Names one in-flight request by its id (compared as a JSON value). That request's answer is not
    written. Nothing else happens: the call already sent to the daemon is not withdrawn, and no Stop is
    ever sent for a cancellation. A cancellation for an id that is not in flight is ignored.

The MCP face has no Stop path of its own: ``recursion_run_stop`` forwards to ``run.stop`` and answers
what it answered (``stopped`` only when cleanup is confirmed, otherwise ``pending``). A call that raises
answers ``control_api_unavailable`` with ``delivered: "unknown"``, never a stop.

Concurrency. At most ``max_in_flight`` calls run at once; when all are busy the reader waits for one to
finish before it reads on (back-pressure, not refusal). ``recursion_run_stop`` may also use one of
``reserved_stop`` extra slots, so a Stop is not kept waiting behind slow starts (the daemon keeps
reserved connections for Stop in the same way). A request whose id is still in flight is refused.

End of input. The client closing stdin ends the session: the reader stops, calls still in flight finish
and their answers are written while stdout stays open, and the process exits 0. It never stops a run:
the runs it started go on (detach, not stop). A write to stdout that fails ends reading in the same way:
nothing more is forwarded whose answer the client could not see.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, BinaryIO

from . import jsonrpc
from .framing import NOT_JSON, NOT_UTF8, TOO_LARGE, Frame, FrameWriter, read_frames
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

UNAVAILABLE = "control_api_unavailable"
_KNOWN_AFTER_INIT = frozenset({"tools/list", "tools/call"})

Log = Callable[[dict], None]


def _quiet(_event: dict) -> None:
    pass


def negotiate(requested: str) -> str:
    """The version this server answers to ``requested``: always the one it speaks."""

    return LEGACY_PROTOCOL


def _unknown_outcome(exc: BaseException) -> dict:
    """``tools/call`` answer for a call that raised: delivery unknown, nothing claimed."""

    answer = {"code": UNAVAILABLE, "delivered": "unknown", "detail": f"mcp:{type(exc).__name__}"}
    return {"content": [{"type": "text", "text": json.dumps(answer, sort_keys=True)}],
            "structuredContent": answer, "isError": True}


@dataclass(slots=True, eq=False)
class _Call:
    request_id: Any
    tool: Tool
    reserved: bool
    cancelled: bool = False


class LegacySession:
    """One client's session. ``api`` is anything with ``call(method, params) -> dict``; ``write`` puts one
    message on the wire."""

    def __init__(self, api: Any, write: Callable[[dict], Any], *, tools: Iterable[Tool] | None = None,
                 max_in_flight: int = MAX_IN_FLIGHT, reserved_stop: int = RESERVED_STOP,
                 log: Log | None = None) -> None:
        if max_in_flight < 1 or reserved_stop < 0:
            raise ValueError("max_in_flight must be positive and reserved_stop not negative")
        self._api = api
        self._write = write
        self._log = log or _quiet
        self._tools = {t.name: t for t in (tool_table() if tools is None else tools)}
        self._descriptors = [t.descriptor for t in self._tools.values()]
        self._max_in_flight, self._reserved_stop = max_in_flight, reserved_stop
        self._cond = threading.Condition()
        self._in_flight: dict[Any, _Call] = {}  # by request id (str or number)
        self._general = 0
        self._reserved = 0
        self.initialized = False
        self.initialized_notified = False
        self.client_info: dict | None = None
        self.requested_version: str | None = None

    # -- input --------------------------------------------------------------------------------------
    def receive(self, frame: Frame) -> None:
        if frame.error == TOO_LARGE:
            self._write(jsonrpc.error(None, jsonrpc.INVALID_REQUEST, "Invalid Request: message too large"))
            return
        if frame.error in (NOT_UTF8, NOT_JSON):
            detail = "not UTF-8" if frame.error == NOT_UTF8 else "not JSON"
            self._write(jsonrpc.error(None, jsonrpc.PARSE_ERROR, f"Parse error: {detail}"))
            return
        message = jsonrpc.classify(frame.message)
        if isinstance(message, jsonrpc.Invalid):
            self._write(jsonrpc.error(message.id, message.code, message.message))
        elif isinstance(message, jsonrpc.Response):
            self._log({"event": "ignored_response"})
        elif isinstance(message, jsonrpc.Notification):
            self._notification(message)
        else:
            self._request(message)

    def _notification(self, note: jsonrpc.Notification) -> None:
        if note.method == "notifications/initialized":
            if self.initialized:
                self.initialized_notified = True
            return
        if note.method == "notifications/cancelled":
            request_id = note.params.get("requestId")
            if not jsonrpc.valid_id(request_id):
                return
            with self._cond:
                call = self._in_flight.get(request_id)
                if call is not None:
                    call.cancelled = True
            self._log({"event": "cancelled", "in_flight": call is not None})
        # Any other notification is ignored.

    def _request(self, req: jsonrpc.Request) -> None:
        with self._cond:
            busy = req.id in self._in_flight
        if busy:
            self._write(jsonrpc.error(req.id, jsonrpc.INVALID_REQUEST,
                                      "Invalid Request: this id belongs to a request still in flight"))
            return
        if req.method == "initialize":
            self._initialize(req)
        elif req.method == "ping":
            self._write(jsonrpc.result(req.id, {}))
        elif req.method in _KNOWN_AFTER_INIT:
            if not self.initialized:
                self._write(jsonrpc.error(req.id, jsonrpc.INVALID_REQUEST,
                                          "Invalid Request: not initialized; send initialize first"))
            elif req.method == "tools/list":
                self._tools_list(req)
            else:
                self._tools_call(req)
        else:
            self._write(jsonrpc.error(req.id, jsonrpc.METHOD_NOT_FOUND, f"Method not found: {req.method}"))

    # -- lifecycle ------------------------------------------------------------------------------------
    def _initialize(self, req: jsonrpc.Request) -> None:
        if self.initialized:
            self._write(jsonrpc.error(req.id, jsonrpc.INVALID_REQUEST, "Invalid Request: already initialized"))
            return
        requested = req.params.get("protocolVersion")
        if not isinstance(requested, str) or not requested:
            self._write(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS,
                                      "Invalid params: protocolVersion must be a non-empty string"))
            return
        for name in ("capabilities", "clientInfo"):
            if name in req.params and not isinstance(req.params[name], dict):
                self._write(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS,
                                          f"Invalid params: {name} must be an object"))
                return
        answered = negotiate(requested)
        self.initialized = True
        self.requested_version = requested
        self.client_info = req.params.get("clientInfo")
        self._log({"event": "initialize", "requested": requested, "answered": answered,
                   "client": (self.client_info or {}).get("name")})
        self._write(jsonrpc.result(req.id, {
            "protocolVersion": answered,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": SERVER_TITLE, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }))

    # -- tools ------------------------------------------------------------------------------------------
    def _tools_list(self, req: jsonrpc.Request) -> None:
        if "cursor" in req.params:
            self._write(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS, "Invalid params: unknown cursor"))
            return
        self._write(jsonrpc.result(req.id, {"tools": self._descriptors}))

    def _tools_call(self, req: jsonrpc.Request) -> None:
        name = req.params.get("name")
        if not isinstance(name, str):
            self._write(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS, "Invalid params: name must be a string"))
            return
        tool = self._tools.get(name)
        if tool is None:
            self._write(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS, f"Unknown tool: {name}"))
            return
        arguments = req.params.get("arguments", {})
        if arguments is None:
            arguments = {}
        problem = check_arguments(tool.input_schema, arguments)
        if problem is not None:
            self._write(jsonrpc.error(req.id, jsonrpc.INVALID_PARAMS, f"Invalid params: {problem}",
                                      {"tool": name}))
            return
        call = self._admit(req.id, tool)
        worker = threading.Thread(target=self._run, args=(call, arguments), name="mcp-tools-call", daemon=True)
        try:
            worker.start()
        except RuntimeError:  # no thread could be started: nothing was sent to the daemon
            self._release(call)
            self._forget(call)
            self._write(jsonrpc.error(req.id, jsonrpc.INTERNAL_ERROR, "Internal error: no worker for this call"))

    def _admit(self, request_id: Any, tool: Tool) -> _Call:
        """Take a slot (waiting for one if every slot is busy) and record the call as in flight."""

        with self._cond:
            while True:
                if self._general < self._max_in_flight:
                    self._general += 1
                    reserved = False
                    break
                if tool.is_stop and self._reserved < self._reserved_stop:
                    self._reserved += 1
                    reserved = True
                    break
                self._cond.wait()
            call = _Call(request_id, tool, reserved)
            self._in_flight[request_id] = call
            return call

    def _release(self, call: _Call) -> bool:
        """Give the call's slot back; ``True`` when its answer is still wanted (it was not cancelled)."""

        with self._cond:
            if call.reserved:
                self._reserved -= 1
            else:
                self._general -= 1
            self._cond.notify_all()
            return not call.cancelled

    def _forget(self, call: _Call) -> None:
        """The call is over and its answer, if wanted, is written: its id may be used again."""

        with self._cond:
            self._in_flight.pop(call.request_id, None)
            self._cond.notify_all()

    def _run(self, call: _Call, arguments: dict) -> None:
        try:
            try:
                outcome = forward(self._api, call.tool, arguments)
            except Exception as exc:  # noqa: BLE001 - an answer is owed; the call's effect is unknown
                outcome = _unknown_outcome(exc)
            if self._release(call):
                try:
                    self._write(jsonrpc.result(call.request_id, outcome))
                except (TypeError, ValueError):  # an answer JSON cannot hold; it claims nothing either way
                    self._write(jsonrpc.error(call.request_id, jsonrpc.INTERNAL_ERROR,
                                              "Internal error: the answer is not JSON"))
        finally:
            self._forget(call)

    # -- end of input -----------------------------------------------------------------------------------
    def in_flight(self) -> int:
        with self._cond:
            return len(self._in_flight)

    def close(self) -> None:
        """Wait until every call in flight has finished and written its answer (if wanted)."""

        with self._cond:
            while self._in_flight:
                self._cond.wait()


def serve(api: Any, instream: BinaryIO, outstream: BinaryIO, *, log: Log | None = None,
          tools: Iterable[Tool] | None = None, max_in_flight: int = MAX_IN_FLIGHT,
          reserved_stop: int = RESERVED_STOP) -> int:
    """Serve one client until end of input; returns the process exit status (0). ``tools`` defaults to the
    legacy four."""

    writer = FrameWriter(outstream)
    log = log or _quiet
    session = LegacySession(api, writer.write, tools=tools, log=log, max_in_flight=max_in_flight,
                            reserved_stop=reserved_stop)
    for frame in read_frames(instream):
        session.receive(frame)
        if writer.broken:  # the client stopped reading: forward nothing more whose answer it cannot see
            break
    session.close()
    log({"event": "end_of_input", "stdout_broken": writer.broken})
    return 0
