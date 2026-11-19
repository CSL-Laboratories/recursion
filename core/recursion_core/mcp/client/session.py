"""The MCP client's protocol core over a pair of byte streams (stdio framing: one JSON-RPC message per line).

``McpClient(instream, outstream)`` reads the server's messages from ``instream`` (the server's stdout) and
writes its own to ``outstream`` (the server's stdin). It starts no process and ends none: whoever made the
streams owns them. Lines are read with this client's own bounded read and decoded with the Control MCP
server's ``framing.decode_line``; messages are written with its ``framing.FrameWriter`` and shaped and
sorted with its ``jsonrpc`` module, both by import.

``open``
    Sends ``initialize`` asking for ``LATEST_PROTOCOL`` (2026-07-28), with no client capabilities. A
    server that answers ``LEGACY_PROTOCOL`` (2025-06-18) is spoken to in 2025-06-18; a server that answers
    the request with a JSON-RPC error is asked once more, for 2025-06-18. Any other answer ends the session
    (``PROTOCOL_UNSUPPORTED``; ``INITIALIZE_REFUSED`` when the second request is refused too). Then sends
    ``notifications/initialized``.
``list_tools``
    ``tools/list`` page by page (``cursor``, ``nextCursor``) until a page has no ``nextCursor``; a cursor
    the server gives twice, or one that is not a string, is ``BAD_ANSWER``. On
    ``notifications/tools/list_changed`` the client lists again on a thread of its own (one at a time; a
    notice that comes during a listing makes one more), and hands each full list to ``on_tools``.
``begin_call``, ``call_tool``
    ``tools/call``. With ``on_progress`` the request carries a ``progressToken`` and every
    ``notifications/progress`` naming it is handed to the callback (on the reader's thread) until the call
    is over. A call abandoned (``ToolCall.abandon``, or its deadline passed) is answered at once with
    ``CALL_ABANDONED`` or ``CALL_TIMEOUT``, ``notifications/cancelled`` is sent for it, and its late answer,
    when it comes, is dropped.
Requests from the server
    ``ping`` is answered ``{}``. Every other request (``sampling/createMessage``, ``elicitation/create``,
    ``roots/list``, any unknown method) is refused with JSON-RPC error ``-32601`` and a client-side record
    ``{"record": SERVER_REQUEST_REFUSED, "method": ...}`` handed to ``on_record``. Reading goes on, so a call
    in progress still finishes.

BOUNDS. One line longer than ``MAX_MESSAGE_BYTES`` (2 MiB, its newline included) ends the session
(``MESSAGE_TOO_LARGE``): the client reads at most that many bytes plus one and never reads the rest. Each
request has a deadline (``CALL_DEADLINE_S``, 60 s, unless the caller gives a shorter one).

THREADS. Reading runs on a thread of its own and writing on another, so a server that is slow to answer,
or slow to read, never holds the caller past the deadline: a caller only puts a message in the outbox and
waits for its answer with a time limit. Every shared state is changed under one lock; nothing relies on a
global interpreter lock. A callback (``on_progress``, ``on_tools``, ``on_record``) that raises is ignored.

ENDING. The session ends once, for the first of: a line over the bound, end of the server's output
(``SERVER_GONE``), a write that fails (``OUTPUT_FAILED``), ``close`` (``CLIENT_SHUT``) or a refused
negotiation. Every call still waiting then raises ``McpSessionEnded`` with that ending as its ``code``, and
``ending`` keeps it. The writer sends what it already holds and then closes ``outstream``, so the server
reads end of input; ``instream`` is left to its owner.

Lines that are not UTF-8 or not JSON, and responses naming no request in flight, are dropped. A message
that is not valid JSON-RPC is answered as the Control MCP server answers it (``jsonrpc.classify``).
"""

from __future__ import annotations

import collections
import itertools
import threading
import time
from collections.abc import Callable
from typing import Any, BinaryIO

from recursion_core.control.mcp import jsonrpc
from recursion_core.control.mcp.framing import FrameWriter, decode_line, encode

from .limits import CALL_DEADLINE_S, MAX_MESSAGE_BYTES

LATEST_PROTOCOL = "2026-07-28"
LEGACY_PROTOCOL = "2025-06-18"
_ACCEPTED = {LATEST_PROTOCOL: (LATEST_PROTOCOL, LEGACY_PROTOCOL), LEGACY_PROTOCOL: (LEGACY_PROTOCOL,)}
_CLIENT_INFO = {"name": "recursion", "version": "0.0.0"}

SERVER_REQUEST_REFUSED = "server_request_refused"

# How a session ends (``McpClient.ending``, ``McpSessionEnded.code``).
MESSAGE_TOO_LARGE = "mcp_message_too_large"
SERVER_GONE = "mcp_server_gone"
OUTPUT_FAILED = "mcp_output_failed"
CLIENT_SHUT = "mcp_client_shut"
PROTOCOL_UNSUPPORTED = "mcp_protocol_unsupported"
INITIALIZE_REFUSED = "mcp_initialize_refused"

# How one request fails while the session goes on (``McpClientError.code``).
CALL_TIMEOUT = "mcp_call_timeout"
CALL_ABANDONED = "mcp_call_abandoned"
SERVER_ERROR = "mcp_server_error"
BAD_ANSWER = "mcp_bad_answer"


class McpClientError(Exception):
    """A request that did not get a usable answer; ``code`` says why."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class McpSessionEnded(McpClientError):
    """The session is over; ``code`` is its ending."""


class McpServerError(McpClientError):
    """The server answered the request with a JSON-RPC error, kept as ``error``."""

    def __init__(self, error: dict) -> None:
        super().__init__(SERVER_ERROR, str(error.get("message", ""))[:200])
        self.error = error


def _ignore(_value: Any) -> None:
    pass


class _Pending:
    __slots__ = ("done", "result", "failure", "on_progress")

    def __init__(self, on_progress: Callable[[dict], Any] | None) -> None:
        self.done = threading.Event()
        self.result: dict | None = None
        self.failure: McpClientError | None = None
        self.on_progress = on_progress


class ToolCall:
    """One request in flight: ``wait`` for its result, or ``abandon`` it."""

    def __init__(self, client: McpClient, request_id: int, pending: _Pending, deadline: float) -> None:
        self._client, self._id, self._pending, self._deadline = client, request_id, pending, deadline

    def wait(self) -> dict:
        """The result object; raises ``McpClientError`` (a subclass for a server error or an ended session)."""

        if not self._pending.done.wait(max(0.0, self._deadline - time.monotonic())):
            self._client._abandon(self._id, CALL_TIMEOUT)
        self._pending.done.wait()  # set by the answer, the abandonment or the session's end, whichever was first
        if self._pending.failure is not None:
            raise self._pending.failure
        return self._pending.result

    def abandon(self) -> None:
        """Answer the call with ``CALL_ABANDONED`` now, send ``notifications/cancelled`` and drop its answer."""

        self._client._abandon(self._id, CALL_ABANDONED)


class McpClient:
    def __init__(self, instream: BinaryIO, outstream: BinaryIO, *,
                 on_record: Callable[[dict], Any] = _ignore, on_tools: Callable[[list], Any] = _ignore) -> None:
        self._in, self._out = instream, outstream
        self._writer = FrameWriter(outstream)
        self._on_record, self._on_tools = on_record, on_tools
        self._cond = threading.Condition()
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._outbox: collections.deque[dict] = collections.deque()
        self._started = False
        self._relisting = self._relist_again = False
        self._tools: list = []
        self.ending: str | None = None
        self.protocol_version: str | None = None
        self.server_capabilities: dict = {}
        self.server_info: dict = {}

    # -- session ----------------------------------------------------------------------------------------
    def open(self, deadline_s: float = CALL_DEADLINE_S) -> dict:
        """Negotiate the session (see the module text); the server's ``initialize`` result."""

        with self._cond:
            if self._started:
                raise RuntimeError("open is called once")
            self._started = True
        threading.Thread(target=self._read_loop, name="mcp-client-read", daemon=True).start()
        threading.Thread(target=self._write_loop, name="mcp-client-write", daemon=True).start()
        asked = LATEST_PROTOCOL
        try:
            result = self._request("initialize", self._init_params(asked), None, deadline_s).wait()
        except McpServerError:
            asked = LEGACY_PROTOCOL
            try:
                result = self._request("initialize", self._init_params(asked), None, deadline_s).wait()
            except McpServerError as exc:
                self._end(INITIALIZE_REFUSED)
                raise McpSessionEnded(INITIALIZE_REFUSED, exc.detail) from exc
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in _ACCEPTED[asked]:
            self._end(PROTOCOL_UNSUPPORTED)
            raise McpSessionEnded(PROTOCOL_UNSUPPORTED, repr(version)[:80])
        caps, info = result.get("capabilities"), result.get("serverInfo")
        self.protocol_version = version
        self.server_capabilities = caps if isinstance(caps, dict) else {}
        self.server_info = info if isinstance(info, dict) else {}
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    @staticmethod
    def _init_params(version: str) -> dict:
        return {"protocolVersion": version, "capabilities": {}, "clientInfo": dict(_CLIENT_INFO)}

    def close(self) -> None:
        """End the session (``CLIENT_SHUT``) unless it has ended; never waits for the server."""

        self._end(CLIENT_SHUT)
        with self._cond:
            started = self._started
            self._started = True  # a later open is refused
        if not started:  # no writer thread: shut the server's input here (nothing was queued)
            try:
                self._out.close()
            except Exception:  # noqa: BLE001 - already shut, or the owner shut it
                pass

    def _end(self, ending: str) -> None:
        with self._cond:
            if self.ending is not None:
                return
            self.ending = ending
            waiting = list(self._pending.values())
            self._pending.clear()
            self._cond.notify_all()  # the writer sends what it holds, then shuts outstream
        for pending in waiting:
            pending.failure = McpSessionEnded(ending)
            pending.done.set()

    # -- tools -------------------------------------------------------------------------------------------
    @property
    def tools(self) -> list:
        """The last full list ``list_tools`` read."""

        with self._cond:
            return list(self._tools)

    def list_tools(self, deadline_s: float = CALL_DEADLINE_S) -> list:
        tools: list = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            result = self._request("tools/list", {} if cursor is None else {"cursor": cursor}, None, deadline_s).wait()
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpClientError(BAD_ANSWER, "tools/list: tools is not a list")
            tools.extend(page)
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise McpClientError(BAD_ANSWER, "tools/list: nextCursor is not a new string")
            seen.add(cursor)
        with self._cond:
            self._tools = list(tools)
        self._callback(self._on_tools, list(tools))
        return tools

    def begin_call(self, name: str, arguments: dict | None = None, *,
                   on_progress: Callable[[dict], Any] | None = None, deadline_s: float = CALL_DEADLINE_S) -> ToolCall:
        params: dict = {"name": name, "arguments": {} if arguments is None else arguments}
        return self._request("tools/call", params, on_progress, deadline_s)

    def call_tool(self, name: str, arguments: dict | None = None, *,
                  on_progress: Callable[[dict], Any] | None = None, deadline_s: float = CALL_DEADLINE_S) -> dict:
        return self.begin_call(name, arguments, on_progress=on_progress, deadline_s=deadline_s).wait()

    # -- requests ----------------------------------------------------------------------------------------
    def _request(self, method: str, params: dict, on_progress: Callable[[dict], Any] | None,
                 deadline_s: float) -> ToolCall:
        deadline = time.monotonic() + min(deadline_s, CALL_DEADLINE_S)
        pending = _Pending(on_progress)
        with self._cond:
            if self.ending is not None:
                raise McpSessionEnded(self.ending)
            request_id = next(self._ids)
            self._pending[request_id] = pending
        if on_progress is not None:
            params = {**params, "_meta": {"progressToken": request_id}}
        try:
            # Not taken only once the session has ended, and then _end has already answered this call.
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        except BaseException:  # a message JSON cannot hold: nothing was sent
            with self._cond:
                self._pending.pop(request_id, None)
            raise
        return ToolCall(self, request_id, pending, deadline)

    def _abandon(self, request_id: int, code: str) -> None:
        with self._cond:
            pending = self._pending.pop(request_id, None)
        if pending is None:  # answered, abandoned or ended already
            return
        pending.failure = McpClientError(code, f"request {request_id}")
        pending.done.set()
        self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                    "params": {"requestId": request_id, "reason": code}})

    def _send(self, message: dict) -> bool:
        encode(message)  # a message JSON cannot hold raises here, on the caller's thread
        with self._cond:
            if self.ending is not None:
                return False
            self._outbox.append(message)
            self._cond.notify_all()
            return True

    def _write_loop(self) -> None:
        while True:
            with self._cond:
                while not self._outbox and self.ending is None:
                    self._cond.wait()
                if not self._outbox:
                    break
                message = self._outbox.popleft()
            if not self._writer.write(message):
                self._end(OUTPUT_FAILED)
                break
        try:
            self._out.close()
        except Exception:  # noqa: BLE001 - a failed or already shut stream
            pass

    # -- reading -----------------------------------------------------------------------------------------
    def _read_loop(self) -> None:
        ending = SERVER_GONE
        try:
            while self.ending is None:
                line = self._in.readline(MAX_MESSAGE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_MESSAGE_BYTES:
                    ending = MESSAGE_TOO_LARGE
                    break
                frame = decode_line(line)
                if frame is not None and frame.error is None:
                    self._receive(frame.message)
        finally:  # end of input, a read that raised, or a line over the bound: every waiting call is answered
            self._end(ending)

    def _receive(self, message: Any) -> None:
        kind = jsonrpc.classify(message)
        if isinstance(kind, jsonrpc.Response):
            self._response(message)
        elif isinstance(kind, jsonrpc.Request):
            if kind.method == "ping":
                self._send(jsonrpc.result(kind.id, {}))
            else:
                self._send(jsonrpc.error(kind.id, jsonrpc.METHOD_NOT_FOUND, "Method not found"))
                self._callback(self._on_record, {"record": SERVER_REQUEST_REFUSED, "method": kind.method})
        elif isinstance(kind, jsonrpc.Notification):
            if kind.method == "notifications/progress":
                self._progress(kind.params)
            elif kind.method == "notifications/tools/list_changed":
                self._schedule_relist()
        else:
            self._send(jsonrpc.error(kind.id, kind.code, kind.message))

    def _response(self, message: dict) -> None:
        request_id = message.get("id")
        if not jsonrpc.valid_id(request_id):
            return
        with self._cond:
            pending = self._pending.pop(request_id, None)
        if pending is None:  # a late answer to an abandoned call, or one never asked for: dropped
            return
        if "error" in message:
            error = message["error"]
            pending.failure = McpServerError(error if isinstance(error, dict) else {})
        elif isinstance(message.get("result"), dict):
            pending.result = message["result"]
        else:
            pending.failure = McpClientError(BAD_ANSWER, "result is not an object")
        pending.done.set()

    def _progress(self, params: dict) -> None:
        token = params.get("progressToken")
        if not jsonrpc.valid_id(token):
            return
        with self._cond:
            pending = self._pending.get(token)
        if pending is not None and pending.on_progress is not None:
            self._callback(pending.on_progress, params)

    def _schedule_relist(self) -> None:
        with self._cond:
            if self.protocol_version is None or self.ending is not None:
                return
            if self._relisting:
                self._relist_again = True
                return
            self._relisting = True
        try:
            threading.Thread(target=self._relist_loop, name="mcp-client-relist", daemon=True).start()
        except RuntimeError:  # no thread: the next notice tries again
            with self._cond:
                self._relisting = False

    def _relist_loop(self) -> None:
        while True:
            try:
                self.list_tools()
            except McpClientError:
                pass
            with self._cond:
                if not self._relist_again or self.ending is not None:
                    self._relisting = False
                    return
                self._relist_again = False

    @staticmethod
    def _callback(callback: Callable[[Any], Any], value: Any) -> None:
        try:
            callback(value)
        except Exception:  # noqa: BLE001 - a caller's callback never stops the reader
            pass
