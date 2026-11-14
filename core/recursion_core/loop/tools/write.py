"""The write tool: create a file, or replace one this session has read and that is unchanged since. Asks first."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from recursion_core.loop.providers import base as b
from recursion_core.loop.tools.files import atomic_write, read_fresh, resolve_writable
from recursion_core.loop.tools.registry import Tool, ToolContext, ToolOutput

SPEC = b.ToolSpec(
    name="write",
    description=(
        "Write a whole file in the workspace: create it, or replace one you have read in this session (it must "
        "be unchanged since you read it). Folders are created as needed. For a small change to an existing file "
        "use edit instead. The user is asked to approve each write."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The file, relative to the workspace root."},
            "content": {"type": "string", "description": "The file's complete new text."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)


def precheck(args: Mapping[str, Any], ctx: ToolContext) -> None:
    path = resolve_writable(ctx.workspace, args["path"])
    if path.exists():
        read_fresh(ctx.workspace, path)


def run(args: Mapping[str, Any], ctx: ToolContext) -> ToolOutput:
    ws = ctx.workspace
    path = resolve_writable(ws, args["path"])
    shown = ws.display(path)
    before = read_fresh(ws, path) if path.exists() else None
    ctx.cancel.raise_if_cancelled()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = args["content"].encode("utf-8")
    atomic_write(ws, path, data)
    if before is None:
        return ToolOutput(f"created {shown} ({len(data)} bytes)")
    return ToolOutput(f"replaced {shown} ({len(data)} bytes; it had {len(before)})")


TOOL = Tool(SPEC, run, needs_approval=True, precheck=precheck)
