"""Control MCP face (RS2): MCP over newline-delimited stdio, fake driver only.

Every tool is one Control API method, so authentication, scopes, tenant
isolation and busy-reject are the Control API's, not re-implemented here. The
server holds one bearer token and forwards each call through a dispatcher
(in-process service or the loopback HTTP client). Nothing here can reach a
provider.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TextIO

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "recursion-control-fake"
SERVER_VERSION = "0"

Dispatcher = Callable[[str, dict[str, Any]], dict[str, Any]]

_SESSION = {
    "type": "object",
    "properties": {"engine": {"type": "string"}, "session": {"type": "string"}},
    "required": ["engine", "session"],
    "additionalProperties": False,
}
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}

TOOLS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "recursion_service_info": ("service.info", "Service info, scopes and engines", _EMPTY),
    "recursion_route_list": ("route.list", "List the routes the service exposes", _EMPTY),
    "recursion_session_open": ("session.open", "Open a session on an engine", _SESSION),
    "recursion_session_get": ("session.get", "Read a session's state", _SESSION),
    "recursion_session_close": ("session.close", "Close an idle session", _SESSION),
    "recursion_session_run": (
        "session.run",
        "Run one zero-tools prompt on a session (fake driver only)",
        {
            "type": "object",
            "properties": {
                "engine": {"type": "string"},
                "session": {"type": "string"},
                "prompt": {"type": "string"},
                "timeout_s": {"type": "number"},
            },
            "required": ["engine", "session", "prompt"],
            "additionalProperties": False,
        },
    ),
    "recursion_events_poll": (
        "events.poll",
        "Poll the tenant event journal",
        {
            "type": "object",
            "properties": {
                "after": {"type": "integer"},
                "limit": {"type": "integer"},
                "wait_s": {"type": "number"},
            },
            "required": ["after"],
            "additionalProperties": False,
        },
    ),
}


def _tool_list() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": description, "inputSchema": schema}
        for name, (_, description, schema) in TOOLS.items()
    ]


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class ControlMcpServer:
    def __init__(self, dispatch: Dispatcher) -> None:
        self._dispatch = dispatch

    def handle(self, message: Any) -> dict[str, Any] | None:
        """One MCP message in, one response out (None for notifications)."""

        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid_request")
        method, request_id = message.get("method"), message.get("id")
        params = message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            return _error(request_id, -32600, "invalid_request")
        if "id" not in message:
            return None
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _tool_list()}}
        if method == "tools/call":
            return self._call(request_id, params)
        return _error(request_id, -32601, "method_not_found")

    def _call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or name not in TOOLS or not isinstance(arguments, dict):
            return _error(request_id, -32602, "invalid_params")
        try:
            response = self._dispatch(TOOLS[name][0], arguments)
        except Exception:  # noqa: BLE001 - keep the stdio loop alive; leak nothing
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": "transport_error"}, sort_keys=True)}],
                    "isError": True,
                },
            }
        failed = "error" in response
        payload = response["error"] if failed else response["result"]
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
                "isError": failed,
            },
        }

    def serve(self, source: TextIO, sink: TextIO) -> None:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response = _error(None, -32700, "parse_error")
            else:
                response = self.handle(message)
            if response is not None:
                sink.write(json.dumps(response, sort_keys=True) + "\n")
                sink.flush()
