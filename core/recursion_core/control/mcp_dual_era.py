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
"""

from __future__ import annotations

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


def _meta(params: Mapping | None) -> dict:
    if not isinstance(params, Mapping):
        return {}
    raw = params.get("_meta")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_status(state: str) -> str:
    """F56 run-state map onto the Tasks extension statuses."""

    if state == "stopped":
        return "cancelled"
    if state in {"running", "stop_pending"}:
        return "working"
    return "failed"


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
    A "list runs" call stays the plain ``recursion_run_list`` tool in both eras.
    """

    def __init__(self, api: ControlAPI, *, auth_token: str | None = None, clock=None) -> None:
        self.api = api
        self.auth_token = auth_token
        self._clock = clock or (lambda: 1_000.0)
        self.notifications: list[dict] = []
        self.legacy_sessions = 0

    def handle(self, message: Mapping) -> dict:
        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
        if method == "initialize":
            return self._ok(req_id, self._initialize(params))
        if method == "server/discover":
            return self._ok(req_id, self._discover(params))
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
        return meta.get("authorization") == self.auth_token or params.get("authorization") == self.auth_token

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
        now = _iso(self._clock())
        return {
            "resultType": "task",
            "taskId": run_id,
            "status": _run_status(run.get("state", "running")),
            "createdAt": now,
            "lastUpdatedAt": now,
            "ttlMs": None,
        }

    def _tasks(self, req_id, params: Mapping, handler) -> dict:
        if not self._authorized(params):
            return self._error(req_id, -32001, "unauthorized")
        task_id = _printable_id(params.get("taskId"))
        if task_id is None:
            return self._error(req_id, -32602, "invalid taskId")
        return self._ok(req_id, handler(task_id, params))

    def _task_body(self, rec: Mapping, *, extra: dict | None = None) -> dict:
        now = _iso(rec.get("started_at", self._clock()) if rec.get("started_at") else self._clock())
        updated = rec.get("stop") or {}
        last = updated.get("requested_at", rec.get("started_at", self._clock()))
        body = {
            "resultType": "complete",
            "taskId": rec["run_id"],
            "status": _run_status(rec.get("state", "failed")),
            "createdAt": _iso(rec.get("started_at", self._clock())),
            "lastUpdatedAt": _iso(last) if isinstance(last, (int, float)) else now,
            "ttlMs": None,
        }
        if extra:
            body.update(extra)
        return body

    def _tasks_get(self, task_id: str, params: Mapping) -> dict:
        answer = self.api.call("run.status", {"run_id": task_id})
        if answer.get("code") != "ok":
            return {"resultType": "complete", "taskId": task_id, "status": "failed",
                    "createdAt": _iso(self._clock()), "lastUpdatedAt": _iso(self._clock()),
                    "ttlMs": None, "error": {"code": -32602, "message": answer.get("code", "run_unknown")}}
        rec = answer["run"]
        extra = {}
        if rec.get("state") == "stopped":
            extra["result"] = {"content": [{"type": "text", "text": "cancelled"}]}
        return self._task_body(rec, extra=extra)

    def _tasks_cancel(self, task_id: str, params: Mapping) -> dict:
        # Cooperative cancel through the shared Stop path. No notifications/cancelled.
        answer = self.api.call("run.stop", {"run_id": task_id})
        status_answer = self.api.call("run.status", {"run_id": task_id})
        if status_answer.get("code") != "ok":
            return {"resultType": "complete", "taskId": task_id, "status": "failed",
                    "createdAt": _iso(self._clock()), "lastUpdatedAt": _iso(self._clock()),
                    "ttlMs": None,
                    "error": {"code": -32602, "message": answer.get("code", "run_unknown")}}
        rec = status_answer["run"]
        extra = {"statusMessage": answer.get("code")}
        if rec.get("state") == "stopped":
            extra["result"] = {"content": [{"type": "text", "text": "cancelled"}]}
        return self._task_body(rec, extra=extra)


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
