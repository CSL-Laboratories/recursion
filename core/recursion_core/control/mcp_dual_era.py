"""F56 / B5: Claude Code dual-era MCP negotiation over one run model (CSL-766, candidate).

PRODUCT.md at csl-research ``055cc7a5c793b7135e5d0565dc4198cf96230531`` (SHA-256
``e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf``), O60 and
grill Q42: both protocol versions from day one over one internal run model.
``2025-06-18`` with start, poll and cancel as plain tools for Claude Code run
with ``MCP_PROTOCOL_NEGOTIATION=legacy``; ``2026-07-28`` native Tasks for Claude
Code's default runtime from v2.1.304.

This module is an offline reading of:

* ``@anthropic-ai/claude-code`` ``2.1.312`` (installed package
  ``bin/claude.exe``: ``MCP_PROTOCOL_NEGOTIATION`` accepts only ``legacy`` or
  ``auto``; any other value is ignored with a warning; ``legacy`` returns
  ``{mode: "legacy"}`` and skips the modern probe);
* https://code.claude.com/docs/en/mcp (MCP client runtimes; MCP tasks for
  server authors, public beta from v2.1.304);
* modelcontextprotocol/modelcontextprotocol
  ``38353251fab3bdeead03dd8283b9e66076ce1a7f``
  (``schema/2026-07-28/schema.ts``: ``server/discover`` returns
  ``supportedVersions`` and ``capabilities``; modern requests carry a ``_meta``
  envelope).

It does not spawn Claude Code, talk to a provider, or open a network
connection. A live binary wire-capture of a real handshake is unmeasured here.

Deferred (O60 / A45): no pinned pre-release conformance tool has checked the
``2026-07-28`` shapes; they rest on the reading of ``schema.ts`` above.
``DualEraServer`` is an in-process mock with no stdio or HTTP transport.
"""

from __future__ import annotations

import hmac
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from recursion_core.control.api import SUCCESS_CODES, ControlAPI
from recursion_core.control import mcp_tools

# Sources read 2026-11-08. No Claude Code process was launched.
CLAUDE_CODE_PACKAGE_VERSION = "2.1.312"
CLAUDE_CODE_DOCS = "https://code.claude.com/docs/en/mcp"
MCP_SPEC_COMMIT = "38353251fab3bdeead03dd8283b9e66076ce1a7f"
MCP_SPEC_FILE = "schema/2026-07-28/schema.ts"
LIVE_WIRE_CAPTURE: str | None = None

LEGACY_PROTOCOL = "2025-06-18"
MODERN_PROTOCOL = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

VALID_NEGOTIATION = frozenset({"legacy", "auto"})
Era = Literal["legacy", "modern"]
Handshake = Literal["initialize", "server/discover"]


@dataclass(frozen=True, slots=True)
class Negotiation:
    """What Claude Code would send next, given ``MCP_PROTOCOL_NEGOTIATION``."""

    era: Era
    protocol: str
    handshake: Handshake
    probed: bool
    mode: str | None


def negotiation_mode(value: str | None) -> str | None:
    """``legacy`` or ``auto`` win; any other value is ignored (2.1.312 binary)."""

    if value in VALID_NEGOTIATION:
        return value
    return None


def claude_code_negotiate(
    env_value: str | None,
    *,
    server_supports_modern: bool = True,
    skip_cached_probe: bool = False,
) -> Negotiation:
    """Encode Claude Code's default vs ``MCP_PROTOCOL_NEGOTIATION=legacy``.

    Default (unset) and ``auto`` probe a dual-era server and stay on
    ``2026-07-28`` / ``server/discover`` when the server supports it. ``legacy``
    skips the probe and initializes on ``2025-06-18``. Unset may skip a cached
    stdio probe for about 7 days (docs); ``auto`` forces the probe.
    """

    mode = negotiation_mode(env_value)
    if mode == "legacy":
        return Negotiation("legacy", LEGACY_PROTOCOL, "initialize", False, mode)
    if skip_cached_probe and mode != "auto":
        return Negotiation("legacy", LEGACY_PROTOCOL, "initialize", False, mode)
    if server_supports_modern:
        return Negotiation("modern", MODERN_PROTOCOL, "server/discover", True, mode)
    return Negotiation("legacy", LEGACY_PROTOCOL, "initialize", True, mode)


def _same_token(expected: str, presented: object) -> bool:
    """Constant-time match. A non-string presented value is not the token."""

    if not isinstance(presented, str):
        return False
    return hmac.compare_digest(presented, expected)


def _meta(params: Mapping | None) -> dict:
    if not isinstance(params, Mapping):
        return {}
    raw = params.get("_meta")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp(value: object, fallback: float) -> float:
    """A finite epoch number, or ``fallback``.

    ``bool`` is an ``int`` and is not a timestamp. ``0`` is. ``NaN`` and
    infinities are not, and neither is a number ``datetime`` cannot format.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    number = float(value)
    if not math.isfinite(number):
        return fallback
    try:
        datetime.fromtimestamp(number, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return fallback
    return number


def _run_status(state: str) -> str:
    """F56 run-state map onto the Tasks extension statuses.

    ``cancelled`` is terminal for Claude Code, so only a confirmed ``stopped`` maps to it. An unconfirmed
    Stop (``stop_pending``) and a launch still being recorded (``starting``) stay ``working``.
    """

    if state == "stopped":
        return "cancelled"
    if state in {"starting", "running", "stop_pending"}:
        return "working"
    return "failed"


def _task_status(rec: Mapping) -> str:
    """A run whose Stop is not confirmed stays ``working`` (S6 "pending"), even ``start_failed``: the launch
    may have happened, so a terminal ``failed`` would tell the client nothing is left to stop."""

    return "working" if _stop_pending(rec) else _run_status(rec.get("state", "failed"))


def _stop_pending(rec: Mapping) -> bool:
    """S6: a Stop the core has not confirmed reads "pending" on every surface, Tasks included."""

    # Same rule as the TUI and CLI (``tui.protocol.stop_unconfirmed``): any state but ``stopped`` that carries a
    # stop record, e.g. a Stop kept as intent on a ``starting`` or ``start_failed`` run (t1430u X1c).
    state = rec.get("state")
    if state == "stopped":
        return False
    return state == "stop_pending" or isinstance(rec.get("stop"), Mapping)


def _printable_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) > 128 or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        return None
    return value


class DualEraServer:
    """One internal ``ControlAPI`` spoken as legacy tools and as modern Tasks.

    Legacy: session-scoped ``initialize`` at ``2025-06-18``, then plain
    ``tools/call`` for start, poll and cancel. Modern: stateless
    ``server/discover``, a ``_meta`` envelope on every request, ``resultType``
    on every result, and ``CreateTaskResult`` from ``tools/call`` only when that
    request lists the Tasks extension. ``tasks/list`` and
    ``notifications/cancelled`` / ``notifications/tasks`` are not emitted.
    An inbound message with no ``id`` is a notification: it is recorded and
    answered with nothing, and it does not stop a run. JSON-RPC id ``0`` is a
    request and is still answered.
    A "list runs" call stays the plain ``recursion_run_list`` tool in both eras.

    ``tools/list`` returns the control tool descriptors in both eras. When
    ``auth_token`` is set, ``tools/list``, ``tools/call`` and every ``tasks/*``
    request need it (``_meta.authorization``) before anything reaches
    ``ControlAPI``; only the handshakes are open. An unknown ``taskId`` is a
    JSON-RPC ``-32602`` ``run_unknown`` error, never a task that "failed". Task
    timestamps come from the run record's ``started_at``; the default clock is
    wall time.
    """

    def __init__(self, api: ControlAPI, *, auth_token: str | None = None, clock=None) -> None:
        self.api = api
        self.auth_token = auth_token
        self._clock = clock or time.time
        self.notifications: list[dict] = []
        self.legacy_sessions = 0

    def handle(self, message: Mapping) -> dict | None:
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
        if "id" not in message:
            # A notification has no id. JSON-RPC id 0 is a request and is answered below.
            # notifications/cancelled names a request id, not a task, and is not a Stop.
            self.notifications.append({"method": method, "params": dict(params)})
            return None
        req_id = message.get("id")
        if method == "initialize":
            return self._ok(req_id, self._initialize(params))
        if method == "server/discover":
            return self._ok(req_id, self._discover(params))
        if method in {"tools/list", "tools/call"} and not self._authorized(params):
            return self._error(req_id, -32001, "unauthorized")
        if method == "tools/list":
            return self._ok(req_id, self._tools_list(params))
        if method == "tools/call":
            return self._ok(req_id, self._tools_call(params))
        if method == "tasks/get":
            return self._tasks(req_id, params, self._tasks_get)
        if method == "tasks/cancel":
            return self._tasks(req_id, params, self._tasks_cancel)
        if method == "tasks/list":
            return self._error(req_id, -32601, "tasks/list is not served; use recursion_run_list")
        return self._error(req_id, -32601, f"method_unknown:{method}")

    def _ok(self, req_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _error(self, req_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def _initialize(self, params: Mapping) -> dict:
        requested = params.get("protocolVersion")
        protocol = requested if requested == LEGACY_PROTOCOL else LEGACY_PROTOCOL
        self.legacy_sessions += 1
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "recursion-control", "version": "candidate"},
        }

    def _discover(self, params: Mapping) -> dict:
        meta = _meta(params)
        if meta.get(META_PROTOCOL) not in {None, MODERN_PROTOCOL}:
            # Still answer; a dual-era server advertises what it speaks.
            pass
        return {
            "resultType": "complete",
            "supportedVersions": [MODERN_PROTOCOL, LEGACY_PROTOCOL],
            "capabilities": {
                "tools": {},
                "extensions": {TASKS_EXTENSION: {}},
            },
            "_meta": {
                META_SERVER_INFO: {"name": "recursion-control", "version": "candidate"},
            },
        }

    def _modern_request(self, params: Mapping) -> bool:
        meta = _meta(params)
        return meta.get(META_PROTOCOL) == MODERN_PROTOCOL

    def _wants_task(self, params: Mapping) -> bool:
        caps = _meta(params).get(META_CLIENT_CAPS)
        if not isinstance(caps, Mapping):
            return False
        extensions = caps.get("extensions")
        return isinstance(extensions, Mapping) and TASKS_EXTENSION in extensions

    def _authorized(self, params: Mapping) -> bool:
        if self.auth_token is None:
            return True
        meta = _meta(params)
        return _same_token(self.auth_token, meta.get("authorization")) or _same_token(
            self.auth_token, params.get("authorization")
        )

    def _tools_list(self, params: Mapping) -> dict:
        tools = mcp_tools.descriptors()
        if self._modern_request(params):
            return {"resultType": "complete", "tools": tools}
        return {"tools": tools}

    def _tools_call(self, params: Mapping) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
        result = mcp_tools.call_tool(self.api, str(name), dict(arguments))
        if self._modern_request(params):
            if name == mcp_tools.STOP_TOOL:
                return self._plain_modern_tool(result)
            if self._wants_task(params) and name == "recursion_run_start":
                return self._create_task(result)
            return self._plain_modern_tool(result)
        return result

    def _plain_modern_tool(self, result: dict) -> dict:
        body = {
            "resultType": "complete",
            "content": result.get("content", []),
            "structuredContent": result.get("structuredContent"),
            "isError": result.get("isError", False),
        }
        return body

    def _create_task(self, result: dict) -> dict:
        structured = result.get("structuredContent") or {}
        if structured.get("code") not in SUCCESS_CODES:
            return self._plain_modern_tool(result)
        run = structured.get("run") or {}
        run_id = _printable_id(run.get("run_id"))
        if run_id is None:
            return self._plain_modern_tool(result)
        # Re-read the record: a Stop that landed during the launch (#327 stop intent) leaves the run
        # stop_pending, and S6 says the first Tasks reply reads "pending" too (red-team r2 finding 1).
        status_answer = self.api.call("run.status", {"run_id": run_id})
        if status_answer.get("code") == "ok" and isinstance(status_answer.get("run"), Mapping):
            run = status_answer["run"]
        now = _iso(_timestamp(run.get("started_at"), self._clock()))
        body = {
            "resultType": "task",
            "taskId": run_id,
            "status": _task_status(dict(run, state=run.get("state", "running"))),
            "createdAt": now,
            "lastUpdatedAt": now,
            "ttlMs": None,
        }
        if _stop_pending(run):
            body["statusMessage"] = "pending"
        return body

    def _tasks(self, req_id, params: Mapping, handler) -> dict:
        if not self._authorized(params):
            return self._error(req_id, -32001, "unauthorized")
        task_id = _printable_id(params.get("taskId"))
        if task_id is None:
            return self._error(req_id, -32602, "invalid taskId")
        body = handler(task_id, params)
        if isinstance(body, str):
            # Only an unknown run is invalid params; a registry outage is the server's error, not a
            # missing task (red-team r3 finding 1).
            return self._error(req_id, -32602 if body in {"run_unknown", "request_invalid"} else -32603, body)
        return self._ok(req_id, body)

    def _task_body(self, rec: Mapping, *, extra: dict | None = None) -> dict:
        clock = self._clock()
        started = _timestamp(rec.get("started_at"), clock)
        now = _iso(started)
        updated = rec.get("stop") or {}
        last = _timestamp(updated.get("requested_at"), started)
        body = {
            "resultType": "complete",
            "taskId": rec["run_id"],
            "status": _task_status(rec),
            "createdAt": now,
            "lastUpdatedAt": _iso(last),
            "ttlMs": None,
        }
        if extra:
            body.update(extra)
        if _stop_pending(rec):
            body["statusMessage"] = "pending"
        return body

    def _tasks_get(self, task_id: str, params: Mapping) -> dict | str:
        answer = self.api.call("run.status", {"run_id": task_id})
        if answer.get("code") != "ok":
            return _error_code(answer)  # run_unknown: -32602, not a task that failed
        rec = answer["run"]
        extra = {}
        if rec.get("state") == "stopped":
            extra["result"] = {"content": [{"type": "text", "text": "cancelled"}]}
        return self._task_body(rec, extra=extra)

    def _tasks_cancel(self, task_id: str, params: Mapping) -> dict | str:
        # Cooperative cancel through the shared Stop path. No notifications/cancelled.
        before = self.api.call("run.status", {"run_id": task_id})
        if before.get("code") != "ok":
            return _error_code(before)  # nothing is sent to the Stop path
        answer = self.api.call("run.stop", {"run_id": task_id})
        if answer.get("code") not in {"stopped", "pending"}:
            return _error_code(answer)  # run.stop sent nothing (unknown run or registry down at its read)
        status_answer = self.api.call("run.status", {"run_id": task_id})
        if status_answer.get("code") == "ok" and answer.get("recorded") is not False:
            rec = status_answer["run"]
        else:
            # The Stop was attempted but its result is not in the registry (the re-read failed, or the
            # write-back did): the run.stop answer is the truth (S6). An unconfirmed Stop reads working +
            # "pending", and a confirmed one reads cancelled, never pending from a stale record (red-team r3
            # finding 1; bot review 4511585104). The stop time is kept from the record when it has one
            # (bot review 4511755938). A write that never landed has no stored time: inventing the server
            # clock makes this reply newer than the next tasks/get, which still reads the start time
            # (bot review 4517669064).
            state = "stopped" if answer["code"] == "stopped" else "stop_pending"
            rec = dict(before["run"], state=state)
            stop = dict(rec.get("stop") or {})
            requested_at = stop.get("requested_at")
            # bool is an int subclass, so it is not a stored stop time.
            has_time = not isinstance(requested_at, bool) and isinstance(requested_at, (int, float))
            if not has_time and answer.get("recorded") is not False:
                stop["requested_at"] = self._clock()
            if stop:
                rec["stop"] = stop
        extra = {"statusMessage": answer.get("code")}
        if rec.get("state") == "stopped":
            extra["result"] = {"content": [{"type": "text", "text": "cancelled"}]}
        return self._task_body(rec, extra=extra)


def _error_code(answer: Mapping) -> str:
    """Typed API code carried as the JSON-RPC error message (``run_unknown``, ``registry_unavailable``...)."""

    code = answer.get("code")
    return code if isinstance(code, str) and code else "internal_error"


def modern_params(protocol: str = MODERN_PROTOCOL, *, tasks: bool = False, **extra) -> dict:
    """Build a 2026-07-28 ``_meta`` envelope for tests (no live client)."""

    caps: dict = {}
    if tasks:
        caps = {"extensions": {TASKS_EXTENSION: {}}}
    meta = {
        META_PROTOCOL: protocol,
        META_CLIENT_INFO: {"name": "test-client", "version": "0"},
        META_CLIENT_CAPS: caps,
    }
    meta.update(extra.pop("meta", {}))
    params = {"_meta": meta}
    params.update(extra)
    return params
