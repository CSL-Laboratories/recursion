"""The MCP client's protocol core over a pair of byte streams (stdio framing: one JSON-RPC message per line).

``McpClient(instream, outstream)`` reads the server's messages from ``instream`` (the server's stdout) and
writes its own to ``outstream`` (the server's stdin). It starts no process and ends none: whoever made the
streams owns them. Lines are read with this client's own bounded read and decoded with the Control MCP
server's ``framing.decode_line``; each message is encoded once with its ``framing.encode``, when it is made, and
those bytes are what is written; messages are shaped and sorted with its ``jsonrpc`` module, both by import.

``open``
    Sends ``initialize`` asking for ``LATEST_PROTOCOL`` (2026-07-28), with no client capabilities. A
    server that answers ``LEGACY_PROTOCOL`` (2025-06-18) is spoken to in 2025-06-18; a server that answers
    the request with a JSON-RPC error is asked once more, for 2025-06-18. Every other way ``open`` fails
    ends the session, once, with the code the caller is given (``McpSessionEnded``): no answer within the
    deadline (``CALL_TIMEOUT``), a result that is not an object (``BAD_ANSWER``), a revision the client does
    not speak (``PROTOCOL_UNSUPPORTED``), the second request refused too (``INITIALIZE_REFUSED``), or the
    session's own ending. ``ending`` keeps that code, a later call raises ``McpSessionEnded`` with it, and the
    server reads end of input. ``initialize`` is never cancelled: its late answer, if one comes, is dropped.
    On success, sends ``notifications/initialized``.
``list_tools``
    ``tools/list`` page by page (``cursor``, ``nextCursor``) until a page has no ``nextCursor``; a cursor
    the server gives twice, or one that is not a string, is ``BAD_ANSWER``. On
    ``notifications/tools/list_changed`` the client lists again on a thread of its own (one at a time; a
    notice that comes during a listing makes one more), and hands each full list to ``on_tools``.
``begin_call``, ``call_tool``
    ``tools/call``. Every such request carries a ``progressToken`` of its own (its id: new for each request,
    never used twice in a session), so a server may report progress whatever the caller asked to see; with
    ``on_progress`` every ``notifications/progress`` naming it is handed to the callback (on the reader's
    thread) until the call is over. A call abandoned (``ToolCall.abandon``, or its deadline passed, whether
    or not anyone waits on it) is answered at once with ``CALL_ABANDONED`` or ``CALL_TIMEOUT``,
    ``notifications/cancelled`` is sent for it once (never for ``initialize``), and its late answer, when it
    comes, is dropped. A request whose deadline passed before its bytes were written is taken out of the
    outbox and never sent, and no ``notifications/cancelled`` goes out for it: the server never saw it.
Before ``open``
    A request made before ``open`` has finished (``list_tools``, ``begin_call``, ``call_tool``) is refused
    with ``McpClientError`` and the code ``SESSION_NOT_OPEN``; nothing goes on the wire before the
    ``initialize`` exchange. A request made after the session ended raises ``McpSessionEnded``.
Requests from the server
    ``ping`` is answered ``{}``. Every other request (``sampling/createMessage``, ``elicitation/create``,
    ``roots/list``, any unknown method) is refused with JSON-RPC error ``-32601`` and a client-side record
    ``{"record": SERVER_REQUEST_REFUSED, "method": ...}`` handed to ``on_record``. A method that is longer
    than ``MAX_SERVER_TEXT_CHARS`` or holds a character that is not printable is kept as its excerpt (SERVER
    TEXT below) and the record gains one key, ``method_excerpt``: ``{"length": <the server's length in
    characters>, "cut": <whether it was cut>}``. The id echoed in the refusal is the server's own, unchanged.
    Reading goes on, so a call in progress still finishes.

LIMITS. An outside server is untrusted input; every limit is one named constant in ``limits.py`` (first
values, none measured), read here when it is checked. A limit that fires is a typed result the caller
receives and a record ``{"record": <code>, ...}`` handed to ``on_record``; it touches only this session.

* start: ``initialize`` is answered within ``INITIALIZE_DEADLINE_S`` (5 s, both requests together), else
  the session ends ``SERVER_START_TIMEOUT``; no ``notifications/cancelled`` is sent for ``initialize``;
* each call: a deadline (``CALL_DEADLINE_S``, 60 s, unless the caller gives a shorter one), held by the
  session: past it the call is abandoned (``notifications/cancelled``) and answers ``CALL_TIMEOUT`` even when
  nobody waits on it. A ``notifications/progress`` naming a ``tools/call`` in flight gives that call its
  deadline again (the caller's shorter one if it gave one) from the moment the notice is read, never past
  ``CALL_CEILING_S`` (30 min) from the moment the call was made; a call that meets that ceiling answers
  ``CALL_TIMEOUT`` and its record carries ``"reason": CALL_CUT_CEILING``. Only ``tools/call`` is given more
  time; a notice naming no call in flight changes nothing. A notice naming a call whose deadline has already
  passed (the writer, the session's timer, was held in a write) gives it no time: the reader ends that call
  ``CALL_TIMEOUT`` there, as it does for an answer read after the deadline, and its late answer is dropped;
* one message: a line longer than ``MAX_MESSAGE_BYTES`` (2 MiB, its newline included) ends the session
  (``MESSAGE_TOO_LARGE``): the client reads at most that many bytes plus one and never reads the rest;
* one tool result: everything a model reads of it shares ``MAX_TOOL_RESULT_TEXT_BYTES`` (256 KiB). Its text
  comes first (every string ``text`` of a content item, whatever its ``type``, the string ``text`` of an
  item's ``resource``, or a ``content`` that is one string; UTF-8): it is cut on a whole character where the
  budget ends, and an item whose text finds the budget spent is left out. Then ``structuredContent``, the
  result's annotations (those of its items and of their resources, as one part) and the result's ``_meta``
  are each measured as the UTF-8 bytes of their compact JSON and, in that order, left out whole when what is
  left of the budget does not hold them (a server's ``_meta`` left out keeps only the client's mark). The
  result's ``_meta`` then holds ``{RESULT_CUT: {"limit_bytes": ..., "text_bytes": <the text's whole size>}}``,
  with ``"left_out": [<the parts>]`` when a part was left out; the mark is never left out and is not
  counted. Images, audio and other binary data (an item's ``data``, a resource's ``blob``) are bound only by
  the message limit. A ``content`` that is neither a list nor a string fails the call ``BAD_ANSWER``;
* the tool list: listed to ``MAX_TOOLS`` (512); no page is asked for past it; ``list_tools`` returns a
  ``ToolList`` whose ``cut`` is ``TOOL_LIST_CUT`` (``None`` for a whole list), and ``on_tools`` and ``tools``
  hand over a ``ToolList`` that keeps the same ``cut`` and ``reason``, also after a change notice;
* one listing (a caller's ``list_tools`` and the relisting after a change notice): at most ``MAX_LIST_PAGES``
  pages (64) and one deadline for the whole listing (the caller's, bounded by ``CALL_DEADLINE_S``; each page
  waits only for what is left of it). Past either the listing ends: the tools read so far, ``cut`` is
  ``TOOL_LIST_CUT`` and ``reason`` names the bound (``LIST_CUT_PAGES``, ``LIST_CUT_TIME``; ``None`` for the
  tool count and for a whole list), the record carries the same ``reason``, and no page is asked for after it.
  A first page that gets no answer still raises ``CALL_TIMEOUT``, as every call does. Cursors are remembered
  as digests of one size, at most ``MAX_LIST_PAGES`` of them;
* unsent answers: the answers the client owes the server (``ping`` answers, refusals, answers to messages that
  are not valid JSON-RPC) and has not yet written are held to ``MAX_UNSENT_ANSWER_BYTES`` (1 MiB) of encoded
  bytes, the rule of the Control MCP server's own reply queue: an answer is always taken when none is held, the
  one being written counts until it is written, and an answer that would pass the bound is dropped (the server
  is not reading). Requests and notices the caller's calls send are never dropped by it; they keep their own
  deadlines. A stretch of drops is one pause of the server: it ends only when an answer is taken while none is
  held (an answer taken while others are still held does not end it). The first answer dropped in a stretch
  gives the record ``{"record": ANSWERS_DROPPED, "pending_bytes": ..., "limit_bytes": ...}`` and counts as a
  breach; the answer that ends the stretch gives ``{"record": ANSWERS_DROPPED, "dropped": <the count>}``;
* breaches: only what a cut does not already mend counts: a call timeout at the client's own deadline or at
  the ceiling (not at a shorter deadline the caller asked for), a listing cut by pages or by time, and each
  stretch of dropped answers count as one breach. A cut result and a list cut at ``MAX_TOOLS`` are marked
  and recorded and do not count. A breach older than ``BREACH_WINDOW_S`` (10 min) no longer counts; at
  ``MAX_BREACHES`` (3) within that window in this session the server is cut off: the breaching call still
  gets its own typed result, then the session ends ``SERVER_CUT_OFF``. The client keeps the times of at most
  ``MAX_BREACHES`` recent breaches. The count belongs to this client session; a new session starts it again.

A server given up or cut off is left out by the session's own ending: the client shuts the server's input
stream and stops reading; it starts, signals and ends no process.

SERVER TEXT. A server's text is untrusted: it may be long, not text at all, or shaped to mislead whoever reads
it. ``_server_text`` makes every excerpt of it the client keeps (``McpServerError.detail``, the detail of
``PROTOCOL_UNSUPPORTED``, the refused method in the record), and nothing else in the client prints or formats
a server-chosen value: only a ``str`` is taken (any other value is ``(not text: <its type>)``), it is cut to
``MAX_SERVER_TEXT_CHARS`` characters first, and only then is each character that is not printable written as
its backslash escape (``\\x1b``, ``\\t``, ``\\udfff``). ``McpServerError.error`` keeps the error as sent.

THREADS. Reading runs on a thread of its own and writing on another, so a server that is slow to answer,
or slow to read, never holds the caller past the deadline: a caller only puts a message in the outbox and
waits for its answer with a time limit. The writer's thread is also the session's one timer: between writes
it waits for the next message or the first deadline of a call in flight, and ends each call whose deadline
has passed (no thread per call); a caller that waits ends its own call at the deadline too, whichever comes
first. While the writer is inside a write that the server does not read, a call nobody waits on ends when
that write returns. The message is encoded on the caller's thread when the call is made:
a value JSON cannot hold raises there and nothing is queued, and a later change to the caller's objects
changes nothing that is sent. A cut result wakes its caller before its record is handed to ``on_record``. Every shared state is changed under one lock; nothing relies on a
global interpreter lock. A callback (``on_progress``, ``on_tools``, ``on_record``) that raises is ignored.

ENDING. The session ends once, for the first of: a line over the bound, end of the server's output
(``SERVER_GONE``), a write that fails or raises anything at all (``OUTPUT_FAILED``; an exception that is not
an ``Exception``, such as ``KeyboardInterrupt`` or ``SystemExit``, is raised again on the writer's thread
once the ending is set), ``close`` (``CLIENT_SHUT``) or a failed ``open`` (its own code, see ``open``). Every call still waiting then raises ``McpSessionEnded`` with that ending as its ``code``, and
``ending`` keeps it. The writer sends what it already holds and then closes ``outstream``, so the server
reads end of input; ``instream`` is left to its owner.

Lines that are not UTF-8 or not JSON, and responses naming no request in flight, are dropped, and so is a
line read after the session has ended. A number that is not finite (``1e999``, which the decoder reads as
infinity) is not JSON either: a message holding one anywhere is dropped, and when it answers a request in
flight that request fails ``BAD_ANSWER`` and its caller gets no value. Only such a number can be infinite, and
only a line that holds an exponent or a run of 309 digits or more can spell one (the words ``NaN`` and
``Infinity`` are refused by the decoder), so only the message of such a line is walked for one; every other
line pays one scan of its bytes. A message that is not valid JSON-RPC
is answered as the Control MCP server answers it (``jsonrpc.classify``).
"""

from __future__ import annotations

import collections
import hashlib
import itertools
import json
import math
import re
import threading
import time
from collections.abc import Callable
from typing import Any, BinaryIO

from recursion_core.control.mcp import jsonrpc
from recursion_core.control.mcp.framing import decode_line, encode

from .limits import (
    BREACH_WINDOW_S,
    CALL_CEILING_S,
    CALL_DEADLINE_S,
    INITIALIZE_DEADLINE_S,
    MAX_BREACHES,
    MAX_LIST_PAGES,
    MAX_MESSAGE_BYTES,
    MAX_SERVER_TEXT_CHARS,
    MAX_TOOL_RESULT_TEXT_BYTES,
    MAX_TOOLS,
    MAX_UNSENT_ANSWER_BYTES,
)

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
SERVER_START_TIMEOUT = "mcp_server_start_timeout"
SERVER_CUT_OFF = "mcp_server_cut_off"

# How one request fails while the session goes on (``McpClientError.code``); a failed ``open`` ends the
# session with the same code.
CALL_TIMEOUT = "mcp_call_timeout"
CALL_ABANDONED = "mcp_call_abandoned"
SERVER_ERROR = "mcp_server_error"
BAD_ANSWER = "mcp_bad_answer"
# A request made before ``open`` has finished; nothing was sent.
SESSION_NOT_OPEN = "mcp_session_not_open"

# A limit that cut what the server sent while the session goes on (a record, and in the result).
RESULT_CUT = "mcp_result_cut"
TOOL_LIST_CUT = "mcp_tool_list_cut"
# Which bound of one listing cut it (``ToolList.reason`` and the record's ``reason``).
LIST_CUT_PAGES = "pages"
LIST_CUT_TIME = "time"
# A call that progress reports kept alive met ``CALL_CEILING_S`` (the ``reason`` of its ``CALL_TIMEOUT`` record).
CALL_CUT_CEILING = "ceiling"
# Answers owed to a server that does not read were dropped (a record; the first counts as a breach).
ANSWERS_DROPPED = "mcp_answers_dropped"

# A line that can spell a number the decoder reads as infinite: an exponent, or 309 digits or more in a row (a
# run that long is found from its first digit only, so the scan stays linear in the line).
_MAY_BE_NOT_FINITE = re.compile(rb"[0-9][eE][+-]?[0-9]|(?<![0-9])[0-9]{309}")


def _server_text(value: Any) -> str:
    """The excerpt of a server-chosen value that the client keeps (the module text, SERVER TEXT)."""

    if not isinstance(value, str):
        return f"(not text: {'null' if value is None else type(value).__name__})"
    cut = value[:MAX_SERVER_TEXT_CHARS]  # first: nothing longer is ever escaped or printed
    if cut.isprintable():
        return cut
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in cut)


class McpClientError(Exception):
    """A request that did not get a usable answer; ``code`` says why."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class McpSessionEnded(McpClientError):
    """The session is over; ``code`` is its ending."""


class McpServerError(McpClientError):
    """The server answered the request with a JSON-RPC error, kept as sent as ``error`` (nothing prints it);
    ``detail`` is the excerpt of its ``message``."""

    def __init__(self, error: dict) -> None:
        super().__init__(SERVER_ERROR, _server_text(error.get("message", "")))
        self.error = error


def _ignore(_value: Any) -> None:
    pass


class ToolList(list):
    """The tools ``list_tools`` read; ``cut`` is ``TOOL_LIST_CUT`` when the list was listed to ``MAX_TOOLS`` or
    the listing was ended by one of its bounds, which ``reason`` then names (``LIST_CUT_PAGES``, ``LIST_CUT_TIME``)."""

    cut: str | None = None
    reason: str | None = None


def _marked_copy(tools: ToolList) -> ToolList:
    copy = ToolList(tools)
    copy.cut, copy.reason = tools.cut, tools.reason
    return copy


class _Pending:
    __slots__ = ("done", "result", "failure", "on_progress", "method", "deadline", "limit_s", "ceiling",
                 "at_limit", "cancel", "written")

    def __init__(self, on_progress: Callable[[dict], Any] | None, method: str = "", deadline: float = math.inf,
                 limit_s: float = 0.0, ceiling: float = math.inf, at_limit: bool = False,
                 cancel: bool = True) -> None:
        self.done = threading.Event()
        self.result: dict | None = None
        self.failure: McpClientError | None = None
        self.on_progress = on_progress
        self.method = method
        self.deadline = deadline  # moved only by a progress report on a tools/call, never past the ceiling
        self.limit_s = limit_s  # what each progress report gives the call again
        self.ceiling = ceiling  # CALL_CEILING_S after the call was made
        self.at_limit = at_limit  # the deadline is the client's own limit, not a shorter one asked for
        self.cancel = cancel  # False for initialize: never cancelled, and its deadline is kept by open
        self.written = False  # the writer has taken its bytes


class ToolCall:
    """One request in flight: ``wait`` for its result, or ``abandon`` it. The session holds its deadline: the
    call ends at it whether or not anyone waits."""

    def __init__(self, client: McpClient, request_id: int, pending: _Pending) -> None:
        self._client, self._id, self._pending = client, request_id, pending

    def wait(self) -> dict:
        """The result object; raises ``McpClientError`` (a subclass for a server error or an ended session)."""

        pending = self._pending
        while not pending.done.wait(max(0.0, pending.deadline - time.monotonic())):
            if self._client._time_out(self._id):  # ended here, or already settled; else a report moved it
                break
        pending.done.wait()  # set by the answer, the abandonment or the session's end, whichever was first
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
        self._on_record, self._on_tools = on_record, on_tools
        self._cond = threading.Condition()
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        # (the message, its size if an answer, the bytes encoded at the call and written, its id if a request)
        self._outbox: collections.deque[tuple[dict, int, bytes, int | None]] = collections.deque()
        self._unsent_answer_bytes = 0  # the answers owed and not yet written, the one being written included
        self._answers_dropped = 0  # in the stretch of drops under way
        self._started = False
        self._relisting = self._relist_again = False
        self._tools = ToolList()
        self._breach_times: collections.deque[float] = collections.deque()  # at most MAX_BREACHES, the newest
        self._open = False  # open() has finished: requests other than initialize may go out
        self.ending: str | None = None
        self.protocol_version: str | None = None
        self.server_capabilities: dict = {}
        self.server_info: dict = {}

    # -- session ----------------------------------------------------------------------------------------
    def open(self, deadline_s: float = INITIALIZE_DEADLINE_S) -> dict:
        """Negotiate the session (see the module text); the server's ``initialize`` result."""

        with self._cond:
            if self._started:
                raise RuntimeError("open is called once")
            self._started = True
        threading.Thread(target=self._read_loop, name="mcp-client-read", daemon=True).start()
        threading.Thread(target=self._write_loop, name="mcp-client-write", daemon=True).start()
        limit_s = min(deadline_s, INITIALIZE_DEADLINE_S)  # the start limit used, named in its record
        deadline = time.monotonic() + limit_s
        start_bound = deadline_s >= INITIALIZE_DEADLINE_S  # equal counts as the start bound
        asked = LATEST_PROTOCOL
        try:
            try:
                result = self._initialize(asked, deadline, start_bound, limit_s)
            except McpServerError:
                asked = LEGACY_PROTOCOL
                try:
                    result = self._initialize(asked, deadline, start_bound, limit_s)
                except McpServerError as exc:
                    self._end(INITIALIZE_REFUSED)
                    raise McpSessionEnded(INITIALIZE_REFUSED, exc.detail) from exc
        except McpSessionEnded:
            raise
        except McpClientError as exc:  # no answer within the deadline, a result that is not an object
            self._end(exc.code)
            raise McpSessionEnded(self.ending or exc.code, exc.detail) from exc
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in _ACCEPTED[asked]:
            self._end(PROTOCOL_UNSUPPORTED)
            raise McpSessionEnded(PROTOCOL_UNSUPPORTED, _server_text(version))
        caps, info = result.get("capabilities"), result.get("serverInfo")
        self.protocol_version = version
        self.server_capabilities = caps if isinstance(caps, dict) else {}
        self.server_info = info if isinstance(info, dict) else {}
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        with self._cond:
            self._open = True
        return result

    def _initialize(self, version: str, deadline: float, start_bound: bool, limit_s: float) -> dict:
        """One ``initialize`` request within the start's budget, never cancelled. The ending names the deadline
        that fired: the start bound ends the session ``SERVER_START_TIMEOUT`` with its record; a shorter
        deadline the caller gave ends it ``CALL_TIMEOUT`` (through ``open``), with no start-timeout record."""

        call = self._request("initialize", self._init_params(version), None,
                             max(0.0, deadline - time.monotonic()), cancel=False)
        if start_bound and not call._pending.done.wait(max(0.0, deadline - time.monotonic())):
            self._end(SERVER_START_TIMEOUT)  # first: an answer that comes from now on is dropped, never returned
            self._callback(self._on_record, {"record": SERVER_START_TIMEOUT, "limit_s": limit_s})
            raise McpSessionEnded(self.ending)
        return call.wait()

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

    def _breach(self, code: str, detail: dict, wake: Callable[[], Any] | None = None) -> None:
        """Record a limit that fired and count it; at ``MAX_BREACHES`` within ``BREACH_WINDOW_S`` in this session
        the server is cut off. Only the times of the newest ``MAX_BREACHES`` breaches are kept. ``wake``, when
        given, wakes the breaching caller once the breach is counted (and a cut-off has ended the session) and
        before any record is handed over, so a slow ``on_record`` never holds that caller."""

        with self._cond:
            counted = self.ending is None
            if counted:
                now = time.monotonic()
                times = self._breach_times
                while times and now - times[0] > BREACH_WINDOW_S:  # older than the window: no longer counted
                    times.popleft()
                times.append(now)
                while len(times) > MAX_BREACHES:
                    times.popleft()
            breaches = len(self._breach_times)
        if wake is not None:
            if counted and breaches >= MAX_BREACHES:
                self._end(SERVER_CUT_OFF)
            wake()
        self._callback(self._on_record, {"record": code, **detail})
        if counted and breaches >= MAX_BREACHES:
            self._callback(self._on_record, {"record": SERVER_CUT_OFF, "breaches": breaches})
            self._end(SERVER_CUT_OFF)

    # -- tools -------------------------------------------------------------------------------------------
    @property
    def tools(self) -> ToolList:
        """The last full list ``list_tools`` read, with its ``cut`` and ``reason``."""

        with self._cond:
            return _marked_copy(self._tools)

    def list_tools(self, deadline_s: float = CALL_DEADLINE_S) -> ToolList:
        limit_s = min(deadline_s, CALL_DEADLINE_S)
        deadline = time.monotonic() + limit_s  # one deadline for the whole listing
        tools = ToolList()
        listed = pages = 0
        cursor: str | None = None
        seen: set[bytes] = set()  # a digest of each cursor, never the cursor: one can be as long as a message
        while True:
            left = deadline - time.monotonic()
            if pages and left <= 0:  # no page is asked for after the listing's deadline
                tools.reason = LIST_CUT_TIME
                break
            params = {} if cursor is None else {"cursor": cursor}
            try:
                result = self._request("tools/list", params, None, left if pages else deadline_s).wait()
            except McpClientError as exc:
                if pages and exc.code == CALL_TIMEOUT and not isinstance(exc, McpSessionEnded):
                    tools.reason = LIST_CUT_TIME  # a later page ran into the listing's deadline
                    break
                raise
            pages += 1
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpClientError(BAD_ANSWER, "tools/list: tools is not a list")
            tools.extend(page)
            listed += len(page)
            if len(tools) > MAX_TOOLS:  # listed to the limit; no further page is asked for
                del tools[MAX_TOOLS:]
                tools.cut = TOOL_LIST_CUT
                break
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            digest = hashlib.sha256(cursor.encode("utf-8", "surrogatepass")).digest() if isinstance(cursor, str) else None
            if digest is None or digest in seen:
                raise McpClientError(BAD_ANSWER, "tools/list: nextCursor is not a new string")
            if pages >= MAX_LIST_PAGES:  # no further page is asked for
                tools.reason = LIST_CUT_PAGES
                break
            seen.add(digest)
        if tools.reason is not None:
            tools.cut = TOOL_LIST_CUT
        with self._cond:
            self._tools = _marked_copy(tools)
        self._callback(self._on_tools, _marked_copy(tools))
        if tools.reason == LIST_CUT_PAGES:
            self._breach(TOOL_LIST_CUT, {"reason": LIST_CUT_PAGES, "limit": MAX_LIST_PAGES, "listed": listed})
        elif tools.reason == LIST_CUT_TIME:
            self._breach(TOOL_LIST_CUT, {"reason": LIST_CUT_TIME, "limit_s": limit_s, "listed": listed})
        elif tools.cut is not None:  # mended by the cut: recorded, not counted
            self._callback(self._on_record, {"record": TOOL_LIST_CUT, "limit": MAX_TOOLS, "listed": listed})
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
                 deadline_s: float, cancel: bool = True) -> ToolCall:
        made = time.monotonic()
        limit_s, ceiling = min(deadline_s, CALL_DEADLINE_S), made + CALL_CEILING_S
        pending = _Pending(on_progress, method, min(made + limit_s, ceiling), limit_s, ceiling,
                           at_limit=deadline_s >= CALL_DEADLINE_S, cancel=cancel)
        with self._cond:
            if self.ending is not None:
                raise McpSessionEnded(self.ending)
            if not self._open and method != "initialize":  # nothing goes out before the initialize exchange
                raise McpClientError(SESSION_NOT_OPEN, f"{method}: open() has not finished")
            request_id = next(self._ids)
            self._pending[request_id] = pending
        if method == "tools/call":  # always: a server may report progress only on a request that carries a token
            params = {**params, "_meta": {"progressToken": request_id}}
        try:
            # Not taken once the session has ended (_end has then answered this call) or once the call has
            # already ended at its deadline (and then it is never sent).
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, request_id)
        except BaseException:  # a message JSON cannot hold: nothing was sent
            with self._cond:
                self._pending.pop(request_id, None)
            raise
        return ToolCall(self, request_id, pending)

    def _time_out(self, request_id: int) -> bool:
        """End a call whose deadline has passed (``CALL_TIMEOUT``), from whichever path sees it first: its
        caller's ``wait``, the writer's thread or the reader. ``False`` only while the call is in flight and its
        deadline, which a progress report may have moved, has not passed."""

        return self._abandon(request_id, CALL_TIMEOUT, due=True) is not None

    def _abandon(self, request_id: int, code: str, *, due: bool = False) -> bool | None:
        """Answer the call with ``code`` now; whether this ended it (``None``: ``due`` and not yet due). A call
        ended at its deadline before its request was written is taken out of the outbox and never sent, and
        no cancel goes out for it; a timeout at the client's own deadline or at the ceiling is a breach."""

        with self._cond:
            pending = self._pending.get(request_id)
            if pending is None:  # answered, abandoned or ended already
                return False
            if due and pending.deadline > time.monotonic():
                return None
            del self._pending[request_id]
            ceiling = pending.method == "tools/call" and pending.deadline >= pending.ceiling
            unsent = code == CALL_TIMEOUT and not pending.written
            if unsent:  # the server never saw it: nothing of it goes out
                for index, entry in enumerate(self._outbox):
                    if entry[3] == request_id:
                        del self._outbox[index]
                        break
        pending.failure = McpClientError(code, f"request {request_id}")
        if pending.cancel and not unsent:
            self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": request_id, "reason": code}})
        if code == CALL_TIMEOUT and (pending.at_limit or ceiling):  # counted, then the caller woken, then the record
            self._breach(CALL_TIMEOUT, {"request": request_id, **({"reason": CALL_CUT_CEILING} if ceiling else {})},
                         wake=pending.done.set)
        else:
            pending.done.set()
        return True

    def _send(self, message: dict, request_id: int | None = None) -> bool:
        data = encode(message)  # once, on the caller's thread: a value JSON cannot hold raises here
        with self._cond:
            if self.ending is not None or (request_id is not None and request_id not in self._pending):
                return False
            self._outbox.append((message, 0, data, request_id))
            self._cond.notify_all()
            return True

    def _answer(self, message: dict) -> None:
        """Hand over an answer the client owes the server, or drop it (the module text, LIMITS: unsent answers)."""

        data = encode(message)
        size = len(data)
        started = resumed = 0
        with self._cond:
            if self.ending is not None:
                return
            if self._unsent_answer_bytes and self._unsent_answer_bytes + size > MAX_UNSENT_ANSWER_BYTES:
                self._answers_dropped += 1
                if self._answers_dropped == 1:
                    started = self._unsent_answer_bytes
            else:
                if not self._unsent_answer_bytes:  # taken with none held: the server read all, the stretch ends
                    resumed, self._answers_dropped = self._answers_dropped, 0
                self._unsent_answer_bytes += size
                self._outbox.append((message, size, data, None))
                self._cond.notify_all()
        if started:
            self._breach(ANSWERS_DROPPED, {"pending_bytes": started, "limit_bytes": MAX_UNSENT_ANSWER_BYTES})
        elif resumed:
            self._callback(self._on_record, {"record": ANSWERS_DROPPED, "dropped": resumed})

    def _write_loop(self) -> None:
        try:
            while True:
                with self._cond:
                    due, entry = self._next_job()
                if due:
                    for request_id in due:
                        self._time_out(request_id)
                    continue
                if entry is None:
                    break
                _message, size, data, _request = entry
                written = self._write(data)
                if size:
                    with self._cond:
                        self._unsent_answer_bytes -= size
                if not written:
                    self._end(OUTPUT_FAILED)
                    break
        except BaseException:  # the stream raised what is not an Exception: the session still ends, typed
            self._end(OUTPUT_FAILED)
            raise
        finally:
            try:
                self._out.close()
            except Exception:  # noqa: BLE001 - a failed or already shut stream
                pass

    def _next_job(self) -> tuple[list, tuple | None]:
        """Under the lock, waiting for the first of them: the ids of the calls whose deadline has passed, or the
        next message to write, or ``([], None)`` once the session has ended and the outbox is empty."""

        while True:
            now, first, due = time.monotonic(), math.inf, []
            for request_id, pending in self._pending.items():
                if pending.cancel:  # initialize keeps its own deadline in open
                    if pending.deadline <= now:
                        due.append(request_id)
                    else:
                        first = min(first, pending.deadline)
            if due:
                return due, None
            if self._outbox:
                entry = self._outbox.popleft()
                taken = self._pending.get(entry[3]) if entry[3] is not None else None
                if taken is not None:
                    taken.written = True
                return [], entry
            if self.ending is not None:
                return [], None
            self._cond.wait(None if first == math.inf else first - now)

    def _write(self, data: bytes) -> bool:
        """The bytes of one message, whole (a short write is given the rest), then flushed; ``False`` when the
        stream failed (a write took nothing, or raised): the rule of the Control MCP server's ``FrameWriter``."""

        try:
            view = memoryview(data)
            while view:
                taken = self._out.write(view)
                if not taken or taken < 0:  # 0, None (a raw stream that would block) or a negative count
                    return False
                view = view[taken:]
            self._out.flush()
            return True
        except Exception:  # noqa: BLE001 - a pipe the server stopped reading, a stream already shut
            return False

    # -- reading -----------------------------------------------------------------------------------------
    def _read_loop(self) -> None:
        ending = SERVER_GONE
        try:
            while self.ending is None:
                line = self._in.readline(MAX_MESSAGE_BYTES + 1)
                if not line or self.ending is not None:  # end of input, or a line read after the ending
                    break
                if len(line) > MAX_MESSAGE_BYTES:
                    ending = MESSAGE_TOO_LARGE
                    self._callback(self._on_record, {"record": MESSAGE_TOO_LARGE, "limit_bytes": MAX_MESSAGE_BYTES})
                    break
                frame = decode_line(line)
                if frame is not None and frame.error is None:
                    if _MAY_BE_NOT_FINITE.search(line) is None or _finite(frame.message):
                        self._receive(frame.message)
                    else:
                        self._not_finite(frame.message)
        finally:  # end of input, a read that raised, or a line over the bound: every waiting call is answered
            self._end(ending)

    def _receive(self, message: Any) -> None:
        kind = jsonrpc.classify(message)
        if isinstance(kind, jsonrpc.Response):
            self._response(message)
        elif isinstance(kind, jsonrpc.Request):
            if kind.method == "ping":
                self._answer(jsonrpc.result(kind.id, {}))
            else:
                self._answer(jsonrpc.error(kind.id, jsonrpc.METHOD_NOT_FOUND, "Method not found"))
                self._callback(self._on_record, self._refused_record(kind.method))
        elif isinstance(kind, jsonrpc.Notification):
            if kind.method == "notifications/progress":
                self._progress(kind.params)
            elif kind.method == "notifications/tools/list_changed":
                self._schedule_relist()
        else:
            self._answer(jsonrpc.error(kind.id, kind.code, kind.message))

    def _not_finite(self, message: Any) -> None:
        """A message holding a number that is not finite is dropped; one that answers a request in flight
        fails that request ``BAD_ANSWER``, so its caller gets no value."""

        if not isinstance(jsonrpc.classify(message), jsonrpc.Response):
            return
        request_id = message.get("id")
        if not jsonrpc.valid_id(request_id):
            return
        with self._cond:
            pending = self._pending.pop(request_id, None)
        if pending is not None:
            pending.failure = McpClientError(BAD_ANSWER, "a number that is not finite")
            pending.done.set()

    @staticmethod
    def _refused_record(method: Any) -> dict:
        excerpt = _server_text(method)
        record = {"record": SERVER_REQUEST_REFUSED, "method": excerpt}
        if excerpt != method:  # long, escaped or not text: the reader of the record sees it is a server's, marked
            length = len(method) if isinstance(method, str) else None
            record["method_excerpt"] = {"length": length, "cut": length is None or length > MAX_SERVER_TEXT_CHARS}
        return record

    def _response(self, message: dict) -> None:
        request_id = message.get("id")
        if not jsonrpc.valid_id(request_id):
            return
        with self._cond:
            pending = self._pending.get(request_id)
            late = pending is not None and pending.cancel and pending.deadline <= time.monotonic()
            if not late:
                pending = self._pending.pop(request_id, None)
        if late:  # its deadline passed before the timer saw it: the call ends as a timeout, the answer dropped
            self._time_out(request_id)
            return
        if pending is None:  # a late answer to an abandoned call, or one never asked for: dropped
            return
        if "error" in message:
            error = message["error"]
            pending.failure = McpServerError(error if isinstance(error, dict) else {})
        elif isinstance(message.get("result"), dict):
            pending.result = message["result"]
            content = pending.result.get("content", [])
            if pending.method == "tools/call" and not isinstance(content, (list, str)):
                pending.result = None
                pending.failure = McpClientError(BAD_ANSWER, "tools/call: content is neither a list nor a string")
            elif pending.method == "tools/call":
                cut = _cut_result(pending.result)
                if cut is not None:  # the caller woken first, then the record; mended by the cut, not counted
                    pending.result = cut
                    pending.done.set()
                    record = {"record": RESULT_CUT, **cut["_meta"][RESULT_CUT]}
                    if "left_out" in record:
                        record["left_out"] = list(record["left_out"])
                    self._callback(self._on_record, record)
                    return
        else:
            pending.failure = McpClientError(BAD_ANSWER, "result is not an object")
        pending.done.set()

    def _progress(self, params: dict) -> None:
        token = params.get("progressToken")
        if not jsonrpc.valid_id(token):
            return
        with self._cond:
            pending = self._pending.get(token)
            late = pending is not None and pending.cancel and pending.deadline <= time.monotonic()
            if not late and pending is not None and pending.method == "tools/call":  # never past the ceiling
                pending.deadline = max(pending.deadline, min(time.monotonic() + pending.limit_s, pending.ceiling))
        if late:  # its deadline passed before the timer saw it: no more time, it ends as a timeout here
            self._time_out(token)
            return
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


def _finite(value: Any) -> bool:
    """Whether every number in a decoded message is finite (the decoder reads ``1e999`` as infinity)."""

    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                return False
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return True


def _wire_size(value: Any) -> int:
    """The bytes of a part that is not text, as it is on the wire: its compact JSON, UTF-8."""

    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogatepass"))


def _annotations(content: Any) -> list:
    """The annotations of a result's items and of their resources (one part of the budget)."""

    found = []
    for item in content if isinstance(content, list) else ():
        if isinstance(item, dict):
            resource = item.get("resource")
            found.extend(holder["annotations"] for holder in (item, resource)
                         if isinstance(holder, dict) and "annotations" in holder)
    return found


def _without_annotations(content: list) -> list:
    kept = []
    for item in content:
        if isinstance(item, dict):
            item = {key: value for key, value in item.items() if key != "annotations"}
            resource = item.get("resource")
            if isinstance(resource, dict) and "annotations" in resource:
                item["resource"] = {key: value for key, value in resource.items() if key != "annotations"}
        kept.append(item)
    return kept


def _cut_result(result: dict) -> dict | None:
    """``result`` with all a model reads of it held to ``MAX_TOOL_RESULT_TEXT_BYTES`` and the cut marked; ``None``
    if whole (the module text, LIMITS: one tool result). Text first: every string ``text`` of an item, whatever
    its ``type``, the string ``text`` of its ``resource``, or a ``content`` that is one string; then
    ``structuredContent``, the annotations and ``_meta``, each left out whole when the rest does not hold it."""

    content = result.get("content")
    budget, total, cut = MAX_TOOL_RESULT_TEXT_BYTES, 0, False
    if isinstance(content, str):
        data = content.encode("utf-8", "surrogatepass")
        total = len(data)
        if total > budget:
            content, budget, cut = data[:budget].decode("utf-8", "ignore"), 0, True
        else:
            budget -= total
    elif isinstance(content, list):
        kept = []
        for item in content:
            resource = item.get("resource") if isinstance(item, dict) else None
            places = [] if not isinstance(item, dict) else [
                place for place, holder in (("text", item), ("resource", resource))
                if isinstance(holder, dict) and isinstance(holder.get("text"), str)]
            if not places:
                kept.append(item)
                continue
            new, spent = item, 0
            for place in places:
                data = (item if place == "text" else resource)["text"].encode("utf-8", "surrogatepass")
                total += len(data)
                if len(data) <= budget:
                    budget -= len(data)
                    continue
                cut = True
                if budget == 0:  # the budget was spent before this text
                    spent += 1
                part = data[:budget].decode("utf-8", "ignore")  # a character the limit splits is left out whole
                budget = 0
                new = {**new, "text": part} if place == "text" else {**new, "resource": {**resource, "text": part}}
            if spent < len(places):  # an item whose every text found the budget spent is left out
                kept.append(new)
        content = kept
    notes = _annotations(content)
    parts = (("structuredContent", "structuredContent" in result, lambda: _wire_size(result["structuredContent"])),
             ("annotations", bool(notes), lambda: sum(_wire_size(note) for note in notes)),
             ("_meta", "_meta" in result, lambda: _wire_size(result["_meta"])))
    left_out = []
    for name, present, size in parts:
        if present:
            needed = size()
            if needed <= budget:
                budget -= needed
            else:  # left out whole: a part cut in the middle would not stay valid
                left_out.append(name)
    if not cut and not left_out:
        return None
    new_result = dict(result)
    if "content" in result:
        new_result["content"] = _without_annotations(content) if "annotations" in left_out else content
    if "structuredContent" in left_out:
        del new_result["structuredContent"]
    meta = result.get("_meta")
    meta = dict(meta) if isinstance(meta, dict) and "_meta" not in left_out else {}
    mark: dict = {"limit_bytes": MAX_TOOL_RESULT_TEXT_BYTES, "text_bytes": total}
    if left_out:
        mark["left_out"] = left_out
    meta[RESULT_CUT] = mark
    new_result["_meta"] = meta
    return new_result
