"""The tool registry and the one output cap every tool's result passes through.

Each tool is one immutable entry: its name, description and input schema (sent to the model as they are), the
function that runs it, and whether it must ask the approval queue first (write, edit and shell will; read and
search do not). ``dispatch`` is the only way a tool runs:

1. the call's input must have arrived as a JSON object and must match the tool's schema (a small, strict subset
   of JSON Schema: object, string, integer, number, boolean, array, enum, required, additionalProperties false,
   minimum and maximum, minLength and maxLength); otherwise the tool does not run and the model gets an error;
2. the tool runs with the turn's cancel token; an exception becomes an error result naming its type;
3. every secret the loop knows is removed from the output;
4. the ONE cap applies: at most ``max_bytes`` bytes and ``max_lines`` lines reach the model. A larger output is
   saved whole to the spill store and the model sees its head and its tail around a single marker that names
   the saved copy (``spill:<sha256>``), which the read tool can page through. A tool that pages itself (read,
   search) keeps under the cap and so is never cut twice; the cap still holds if it does not.

Sizes, from the research (vault record 20261028-recursion-frontier-b4-tool-output-spill-04, sections 2, 3
and 5): OpenCode and Pi cap at 2,000 lines or 50 KiB, Codex at about 10k tokens; this cap is 50 KiB and 2,000
lines, kept as parameters so a share of the model's window can replace them. The cut keeps whole lines, 40%
of the room for the head and 60% for the tail (the record's 35/50/15 with its error-line share, which needs a
content classifier, given to head and tail in proportion).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from recursion_core.loop.providers import base as b
from recursion_core.loop.tools.workspace import ToolError, Workspace

DEFAULT_MAX_BYTES = 50 * 1024
DEFAULT_MAX_LINES = 2000
MARKER_RESERVE = 600  # bytes kept free for the marker inside the cap
HEAD_SHARE = 0.4
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

OK, ERROR, DENIED, CANCELLED = "ok", "error", "denied", "cancelled"


@dataclass(frozen=True)
class ToolOutput:
    text: str
    status: str = OK

    @property
    def is_error(self) -> bool:
        return self.status != OK


@dataclass
class ToolContext:
    workspace: Workspace
    cancel: b.CancelToken
    scrub: Callable[[str], str]
    max_bytes: int = DEFAULT_MAX_BYTES
    max_lines: int = DEFAULT_MAX_LINES


ToolFn = Callable[[Mapping[str, Any], ToolContext], ToolOutput]


@dataclass(frozen=True)
class Tool:
    spec: b.ToolSpec
    run: ToolFn
    needs_approval: bool = False


@dataclass(frozen=True)
class Outcome:
    """What one call came to: what the model sees, and what the loop writes on its standard output."""

    call_id: str
    name: str
    status: str
    model_text: str
    truncated: bool
    spill_ref: str | None
    output_bytes: int


def validate(schema: Mapping[str, Any], value: Any, where: str = "input") -> None:
    """Raise ToolError if ``value`` does not match ``schema`` (the subset described above)."""

    kind = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{where} must be one of {list(schema['enum'])}")
    if kind == "object":
        if not isinstance(value, dict):
            raise ToolError(f"{where} must be an object")
        props = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                raise ToolError(f"{where}.{name} is required")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise ToolError(f"{where} has unknown field(s): {', '.join(extra)}")
        for name, sub in props.items():
            if name in value:
                validate(sub, value[name], f"{where}.{name}")
    elif kind == "string":
        if not isinstance(value, str):
            raise ToolError(f"{where} must be a string")
        if len(value) < schema.get("minLength", 0):
            raise ToolError(f"{where} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolError(f"{where} is longer than {schema['maxLength']} characters")
    elif kind in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind == "integer"
                                                                             and not isinstance(value, int)):
            raise ToolError(f"{where} must be an {kind}" if kind == "integer" else f"{where} must be a number")
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError(f"{where} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError(f"{where} must be at most {schema['maximum']}")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ToolError(f"{where} must be true or false")
    elif kind == "array":
        if not isinstance(value, list):
            raise ToolError(f"{where} must be an array")
        items = schema.get("items")
        if items:
            for i, item in enumerate(value):
                validate(items, item, f"{where}[{i}]")
    elif kind is not None:
        raise ToolError(f"schema type {kind!r} is not supported")  # a registry bug, caught by its tests


def _utf8_prefix(data: bytes, limit: int) -> bytes:
    cut = data[:limit]
    return cut.decode("utf-8", errors="ignore").encode("utf-8")


def _utf8_suffix(data: bytes, limit: int) -> bytes:
    cut = data[len(data) - limit:] if limit < len(data) else data
    return cut.decode("utf-8", errors="ignore").encode("utf-8")


def cut_head_tail(text: str, max_bytes: int, max_lines: int) -> tuple[str, str, dict]:
    """Split ``text`` into a head and a tail that together fit ``max_bytes`` bytes and ``max_lines`` lines,
    on whole lines where one fits. Returns (head, tail, facts about what was left out)."""

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    head_room = int(max_bytes * HEAD_SHARE)
    tail_room = max_bytes - head_room
    head_lines_room = max(1, int(max_lines * HEAD_SHARE))
    tail_lines_room = max(1, max_lines - head_lines_room)
    head: list[str] = []
    used = 0
    for line in lines:
        size = len(line.encode("utf-8"))
        if used + size > head_room or len(head) >= head_lines_room:
            break
        head.append(line)
        used += size
    head_text = "".join(head)
    start = len(head)
    partial = False
    if not head and lines:
        # The first line alone is larger than the head's room: show its start, cut at a UTF-8 boundary.
        head_text = _utf8_prefix(lines[0].encode("utf-8"), head_room).decode("utf-8")
        partial = True
        start = 1
    tail: list[str] = []
    used_t = 0
    for line in reversed(lines[start:]):
        size = len(line.encode("utf-8"))
        if used_t + size > tail_room or len(tail) >= tail_lines_room:
            break
        tail.append(line)
        used_t += size
    tail.reverse()
    tail_text = "".join(tail)
    if not tail and len(lines) > start:
        # The last line alone is larger than the tail's room: show its end.
        tail_text = _utf8_suffix(lines[-1].encode("utf-8"), tail_room).decode("utf-8")
        partial = True
    elif not tail and len(lines) == 1 and partial:
        tail_text = _utf8_suffix(lines[0].encode("utf-8"), tail_room).decode("utf-8")  # one long line: its end
    facts = {
        "total_lines": total_lines,
        "head_lines": len(head),
        "tail_lines": len(tail),
        "partial_lines": partial,
        "shown_bytes": len(head_text.encode("utf-8")) + len(tail_text.encode("utf-8")),
    }
    return head_text, tail_text, facts


def marker(facts: dict, total_bytes: int, ref: str | None, error: str | None) -> str:
    n = facts["total_lines"]
    h, t = facts["head_lines"], facts["tail_lines"]
    if facts["partial_lines"] or h + t == 0:
        shown = f"showing the start and the end ({facts['shown_bytes']} of {total_bytes} bytes; lines are cut)"
    else:
        shown = (f"showing lines 1-{h} and {n - t + 1}-{n} of {n}" if t else f"showing lines 1-{h} of {n}") + \
                f" ({facts['shown_bytes']} of {total_bytes} bytes)"
    if ref is not None:
        where = (f"The full output is saved as {ref}; read it with the read tool (path \"{ref}\", with offset and "
                 f"limit), or narrow what you asked for.")
    else:
        where = f"The full output could not be saved ({error}); narrow what you asked for."
    return f"[output truncated: {shown}. {where}]"


class ToolRegistry:
    def __init__(self, tools: Sequence[Tool], *, max_bytes: int = DEFAULT_MAX_BYTES,
                 max_lines: int = DEFAULT_MAX_LINES) -> None:
        if max_bytes <= MARKER_RESERVE * 2 or max_lines < 2:
            raise ValueError("the output cap is too small to hold a head, a tail and the marker")
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            name = tool.spec.name
            if not _NAME.fullmatch(name):
                raise ValueError(f"tool name {name!r}")
            if name in self._tools:
                raise ValueError(f"tool {name!r} registered twice")
            if tool.spec.input_schema.get("type") != "object":
                raise ValueError(f"tool {name!r}: the input schema must be an object")
            self._tools[name] = tool
        self.max_bytes = max_bytes
        self.max_lines = max_lines

    def specs(self) -> tuple[b.ToolSpec, ...]:
        return b.tool_specs([t.spec for t in self._tools.values()])

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def dispatch(self, call: b.ToolCall, ctx: ToolContext) -> Outcome:
        tool = self._tools.get(call.name)
        if ctx.cancel.cancelled:
            return self._finish(call, ToolOutput("not run: the turn was interrupted", CANCELLED), ctx)
        if tool is None:
            known = ", ".join(self.names())
            return self._finish(call, ToolOutput(f"no tool is named {call.name!r}; the tools are: {known}", ERROR),
                                ctx)
        if call.input is None:
            # The documented answer to input that is not JSON: hand the raw text back, built with the JSON
            # library so quotes in it are escaped, and do not run the tool.
            text = json.dumps({"INVALID_JSON": call.raw_input}, ensure_ascii=False)
            return self._finish(call, ToolOutput(text, ERROR), ctx)
        try:
            validate(tool.spec.input_schema, dict(call.input))
        except ToolError as exc:
            return self._finish(call, ToolOutput(f"invalid input: {exc}", ERROR), ctx)
        if tool.needs_approval:
            # The approval path arrives with write, edit and shell (second pull request). Until then a tool that
            # needs approval is refused, never run without it.
            return self._finish(call, ToolOutput("refused: this tool needs an approval and none can be asked yet",
                                                 DENIED), ctx)
        ctx.max_bytes, ctx.max_lines = self.max_bytes, self.max_lines
        try:
            output = tool.run(dict(call.input), ctx)
        except b.Cancelled:
            output = ToolOutput("stopped: the turn was interrupted", CANCELLED)
        except ToolError as exc:
            output = ToolOutput(str(exc), ERROR)
        except Exception as exc:  # noqa: BLE001 - a tool's failure is the model's to see, not the loop's to die of
            output = ToolOutput(f"the tool failed: {type(exc).__name__}: {exc}", ERROR)
        return self._finish(call, output, ctx)

    def _finish(self, call: b.ToolCall, output: ToolOutput, ctx: ToolContext) -> Outcome:
        text = ctx.scrub(output.text)
        data = text.encode("utf-8")
        line_count = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        if len(data) <= self.max_bytes and line_count <= self.max_lines:
            return Outcome(call.call_id, call.name, output.status, text, False, None, len(data))
        ref, error = None, None
        try:
            ref = ctx.workspace.spill.put(data)
        except (OSError, ToolError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        head, tail, facts = cut_head_tail(text, self.max_bytes - MARKER_RESERVE, self.max_lines - 1)
        note = marker(facts, len(data), ref, error)
        joiner = "" if head.endswith("\n") or not head else "\n"
        shown = f"{head}{joiner}{note}\n{tail}"
        return Outcome(call.call_id, call.name, output.status, shown, True, ref, len(data))
