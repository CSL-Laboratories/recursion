"""The edit tool: replace an exact piece of text in a file this session has read. Asks first.

From vault record 20261028-recursion-frontier-dr-tool-editing-03 (F05, F13, F14), the parts this tool takes:
an exact match that must be unique unless ``replace_all`` is set; zero matches and several matches are refused
with typed names (``replace_not_found``, ``anchor_not_unique``); an empty anchor is refused; unknown fields are
refused (the registry's schema check); the file must have been read and be unchanged (``not_read``,
``stale_snapshot``). Set aside: the record's transaction and receipt design (it says it is not an accepted
contract) and the hash-line and syntax-tree formats (mixed evidence, F06 to F08).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from recursion_core.loop.providers import base as b
from recursion_core.loop.tools.files import atomic_write, keep_approved, read_fresh, request_facts, resolve_writable
from recursion_core.loop.tools.registry import Tool, ToolContext, ToolOutput
from recursion_core.loop.tools.workspace import ToolError

CONTEXT_LINES = 3

SPEC = b.ToolSpec(
    name="edit",
    description=(
        "Replace exact text in a file you have read in this session (it must be unchanged since). old_string must "
        "occur exactly once, character for character, unless replace_all is true; include enough surrounding "
        "lines to make it unique. Do not include the line numbers the read tool shows. The user is asked to "
        "approve each edit."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The file, relative to the workspace root."},
            "old_string": {"type": "string", "minLength": 1, "description": "The exact text to replace."},
            "new_string": {"type": "string", "description": "The text to put in its place."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
        },
        "required": ["path", "old_string", "new_string"],
        "additionalProperties": False,
    },
)


def _plan(args: Mapping[str, Any], ctx: ToolContext) -> tuple[Path, str, str, int, int]:
    ws = ctx.workspace
    path = resolve_writable(ws, args["path"])
    shown = ws.display(path)
    if not path.exists():
        raise ToolError(f"{shown} does not exist; use write to create it")
    data = read_fresh(ws, path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{shown} is not UTF-8 text; edit does not change it") from None
    old, new = args["old_string"], args["new_string"]
    if old == new:
        raise ToolError("old_string and new_string are the same; nothing would change")
    first = text.find(old)
    if first < 0 and "\n" in old and "\r" not in old and "\r\n" in text:
        # read shows a CRLF file's lines without their CR, so an anchor copied from it has bare LFs: it is met with
        # the file's own line ends, and so is the text that replaces it
        old, new = old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
        first = text.find(old)
    if first < 0:
        raise ToolError(f"replace_not_found: old_string does not occur in {shown}")
    replace_all = args.get("replace_all", False)
    if not replace_all and text.find(old, first + 1) >= 0:
        # Occurrences are counted where they start, overlapping ones too: "aaa" occurs twice in "aaaa" (astra R8).
        raise ToolError(f"anchor_not_unique: old_string occurs {_starts(text, old)} times in {shown}; include more "
                        f"of the surrounding text, or set replace_all")
    n = text.count(old) if replace_all else 1  # replace_all replaces left to right, without overlaps
    return path, text, text.replace(old, new, n), n, first


def _starts(text: str, old: str) -> int:
    count, at = 0, text.find(old)
    while at >= 0:
        count += 1
        at = text.find(old, at + 1)
    return count


def precheck(args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, str] | None:
    path, *_ = _plan(args, ctx)
    return request_facts(ctx.workspace, args["path"], path)


def _snippet(new_text: str, at: int) -> str:
    lines = new_text.split("\n")  # the lines ``read`` numbers: by LF only, and the CR of a CRLF is not shown
    if lines[-1] == "":
        lines.pop()
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    first = new_text.count("\n", 0, at)
    lo, hi = max(0, first - CONTEXT_LINES), min(len(lines), first + CONTEXT_LINES + 1)
    return "".join(f"{i + 1:>6}\t{lines[i]}\n" for i in range(lo, hi))


def run(args: Mapping[str, Any], ctx: ToolContext) -> ToolOutput:
    path, _, new_text, n, at = _plan(args, ctx)
    keep_approved(ctx, args["path"], path)
    ctx.cancel.raise_if_cancelled()
    atomic_write(ctx.workspace, path, new_text.encode("utf-8"))
    return ToolOutput(f"edited {ctx.workspace.display(path)}: {n} replacement{'s' if n != 1 else ''}\n"
                      + _snippet(new_text, at))


TOOL = Tool(SPEC, run, needs_approval=True, precheck=precheck)
