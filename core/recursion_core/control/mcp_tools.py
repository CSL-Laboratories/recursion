"""Control MCP tool descriptors and dispatch (CSL-798, candidate). See README.md.

Each tool is one control API method; ``call_tool`` forwards to ``ControlAPI.call`` and adds nothing,
so the MCP face has no Stop path of its own. Every annotation is set explicitly (PRODUCT.md 398cd812,
O41, decided Nov 7 "All picks"; Q48 is recorded on F60, CSL-762):

* read tools: ``readOnlyHint: true``, ``destructiveHint: false``, ``idempotentHint: true``,
  ``openWorldHint: false``;
* stop: ``destructiveHint: true`` (ending a run is not an additive update), ``openWorldHint: false``
  (it only ends a local run), ``readOnlyHint: false``, ``idempotentHint: true`` (a repeat stop sends
  nothing);
* start: ``readOnlyHint: false``, ``destructiveHint: false``, ``openWorldHint: true`` (the agent it
  launches may reach the network), ``idempotentHint: false``.

Codex asks before stop in Auto mode and refuses it under ``codex exec`` unless the operator's config
sets stop's per-tool ``approval_mode = "approve"``; see ``codex_config.py``. No MCP transport is
implemented here, and nothing talks to a real Codex or Claude Code client.
"""

from __future__ import annotations

import json

from recursion_core.control.api import SUCCESS_CODES, ControlAPI

_RUN_ID = {"type": "object", "properties": {"run_id": {"type": "string", "minLength": 1}},
           "required": ["run_id"], "additionalProperties": False}
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_START = {
    "type": "object",
    "properties": {"tenant": {"type": "string"}, "engine": {"type": "string"}, "prompt": {"type": "string"},
                   "client_key": {"type": "string"}},
    "required": ["tenant", "engine", "prompt"],
    "additionalProperties": False,
}

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
STOP = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}
START = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}

STOP_TOOL = "recursion_run_stop"

TOOLS = (
    {"name": "recursion_run_list", "method": "run.list", "title": "List runs",
     "description": "List runs and their states. Read only.", "inputSchema": _EMPTY, "annotations": READ},
    {"name": "recursion_run_status", "method": "run.status", "title": "Run status",
     "description": "Read one run's state. Read only.", "inputSchema": _RUN_ID, "annotations": READ},
    {"name": "recursion_run_start", "method": "run.start", "title": "Start a run",
     "description": "Start an agent run on an engine route.", "inputSchema": _START, "annotations": START},
    {"name": STOP_TOOL, "method": "run.stop", "title": "Stop a run",
     "description": "Stop a run. Answers 'stopped' only when cleanup is confirmed; otherwise 'pending'.",
     "inputSchema": _RUN_ID, "annotations": STOP},
)


def descriptors() -> list[dict]:
    """``tools/list`` entries: name, title, description, inputSchema and annotations (title included)."""

    out = []
    for tool in TOOLS:
        annotations = {"title": tool["title"], **tool["annotations"]}
        out.append({"name": tool["name"], "title": tool["title"], "description": tool["description"],
                    "inputSchema": json.loads(json.dumps(tool["inputSchema"])), "annotations": annotations})
    return out


def call_tool(api: ControlAPI, name: str, arguments: dict | None) -> dict:
    """``tools/call``: forward to the control API and return its answer verbatim as structured content."""

    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        return {"content": [{"type": "text", "text": json.dumps({"code": "method_unknown", "detail": name})}],
                "isError": True}
    result = api.call(tool["method"], arguments or {})
    return {"content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
            "structuredContent": result, "isError": result.get("code") not in SUCCESS_CODES}
