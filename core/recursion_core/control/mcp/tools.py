"""The tools the server serves, how a call is forwarded, and how a call's arguments are checked.

Two tables can be served:

* the LEGACY table (``tool_table``): the four run tools exactly as ``control/mcp_tools.py`` (lane CONTROL's
  file) describes them, with their names, titles, input schemas and marks by effect (decided Nov 7, "All
  picks"; stop ``destructiveHint: true``, ``openWorldHint: false``). It is read from that file, never copied.
  It is what a daemon from before the method table answers;
* the PROJECTED table (``projection.py``): the published method table's exposed rows that the daemon says it
  implements, with their published names, annotations and parameter schemas.

``forward`` sends a call to the control API and answers as ``control/mcp_tools.call_tool`` does: the API's
answer as ``structuredContent``, the same JSON as text, and ``isError`` unless the code is ``started``,
``ok`` or ``stopped`` (``control.api.SUCCESS_CODES``). The MCP face adds nothing and has no Stop path of
its own.

``check_arguments`` checks a call's arguments against the tool's own ``inputSchema`` before anything is
sent: 2025-06-18 answers invalid arguments as a protocol error (``-32602``), and a call that does not
match what the server advertised must have no side effect. It implements exactly the keywords the
published parameter schemas use (``SUPPORTED``); a schema with any other keyword, or a ``pattern`` it
cannot read as ECMA-262 reads it, is refused when the table is built (``SchemaUnsupported``), so a schema
is never half checked. ``description`` is an annotation and checks nothing.

``pattern`` is read as JSON Schema says, as an ECMA-262 regular expression, unanchored: outside a
character class ``$`` matches only at the very end (Python's ``$`` also matches before a last newline) and
``.`` matches no line terminator (``\\n``, ``\\r``, U+2028, U+2029). Escapes other than those listed in
``_ESCAPES``, and groups that begin ``(?``, are refused rather than read differently.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from recursion_core.control import mcp_tools
from recursion_core.control.api import SUCCESS_CODES

# Calls to these methods may use the reserved slots (server.RESERVED_STOP): a Stop or an interrupt must not
# wait behind slow starts.
STOP_METHODS = frozenset({"run.stop", "session.stop", "session.interrupt"})

SUPPORTED = frozenset({"type", "properties", "required", "additionalProperties", "minLength", "maxLength",
                       "minimum", "maximum", "pattern", "not", "enum", "anyOf", "items", "description"})
TYPES = frozenset({"object", "string", "integer", "number", "boolean", "array", "null"})
_ESCAPES = frozenset("rnt.-\\/^$()[]{}|*+?")
_LINE_TERMINATORS = "[^\n\r\u2028\u2029]"


class SchemaUnsupported(ValueError):
    """An input schema uses a keyword or a pattern ``check_arguments`` does not implement."""


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    method: str
    descriptor: dict  # the ``tools/list`` entry

    @property
    def input_schema(self) -> dict:
        return self.descriptor["inputSchema"]

    @property
    def is_stop(self) -> bool:
        return self.method in STOP_METHODS


# -- patterns -----------------------------------------------------------------------------------------
def ecma_pattern(pattern: str) -> re.Pattern:
    """``pattern`` compiled so that Python matches what ECMA-262 matches; ``SchemaUnsupported`` otherwise."""

    if not isinstance(pattern, str):
        raise SchemaUnsupported("a pattern must be a string")
    out, i, in_class = [], 0, False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            if i + 1 >= len(pattern) or pattern[i + 1] not in _ESCAPES:
                raise SchemaUnsupported(f"pattern {pattern!r}: escape not read the same in Python")
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            out.append(ch)
        elif ch == "[":
            in_class = True
            out.append(ch)
            if pattern[i + 1:i + 2] == "^":  # a negated class: copy the caret too
                out.append("^")
                i += 1
        elif ch == "(" and pattern[i + 1:i + 2] == "?":
            raise SchemaUnsupported(f"pattern {pattern!r}: '(?' groups are not read the same in Python")
        elif ch == "$":
            out.append(r"\Z")
        elif ch == ".":
            out.append(_LINE_TERMINATORS)
        else:
            out.append(ch)
        i += 1
    if in_class:
        raise SchemaUnsupported(f"pattern {pattern!r}: an unterminated class")
    try:
        return re.compile("".join(out))
    except re.error as exc:
        raise SchemaUnsupported(f"pattern {pattern!r}: {exc}") from None


# -- schemas --------------------------------------------------------------------------------------------
def require_supported(schema: Any, where: str) -> None:
    """Refuse a schema ``check_arguments`` would not check completely."""

    if not isinstance(schema, Mapping):
        raise SchemaUnsupported(f"{where}: a schema must be an object")
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise SchemaUnsupported(f"{where}: unsupported keyword(s) {sorted(unknown)}")
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not kinds or len(set(map(str, kinds))) != len(kinds) or any(k not in TYPES for k in kinds):
            raise SchemaUnsupported(f"{where}: type must name one or more of {sorted(TYPES)}")
    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        raise SchemaUnsupported(f"{where}: additionalProperties may only be false")
    if "required" in schema and (not isinstance(schema["required"], list)
                                 or any(not isinstance(n, str) for n in schema["required"])):
        raise SchemaUnsupported(f"{where}: required must be a list of names")
    for key in ("minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise SchemaUnsupported(f"{where}: {key} must be a whole number")
    for key in ("minimum", "maximum"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], (int, float))):
            raise SchemaUnsupported(f"{where}: {key} must be a number")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise SchemaUnsupported(f"{where}: enum must be a non-empty list")
    if "pattern" in schema:
        ecma_pattern(schema["pattern"])
    if not isinstance(schema.get("properties", {}), Mapping):
        raise SchemaUnsupported(f"{where}: properties must be an object")
    for name, sub in schema.get("properties", {}).items():
        require_supported(sub, f"{where}.{name}")
    for key in ("not", "items"):
        if key in schema:
            require_supported(schema[key], f"{where}.{key}")
    if "anyOf" in schema:
        if not isinstance(schema["anyOf"], list) or not schema["anyOf"]:
            raise SchemaUnsupported(f"{where}: anyOf must be a non-empty list")
        for n, sub in enumerate(schema["anyOf"]):
            require_supported(sub, f"{where}.anyOf[{n}]")


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        # A JSON integer. bool is an int in Python and is not one; 1.0 is refused, as run.list refuses it.
        return type(value) is int
    if kind == "number":
        return (type(value) is int) or (type(value) is float and math.isfinite(value))
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    return value is None


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def check_arguments(schema: Mapping, value: Any, path: str = "arguments") -> str | None:
    """The first way ``value`` breaks ``schema``, as one line, or ``None`` when it conforms."""

    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(k, value) for k in kinds):
            if len(kinds) == 1:
                return f"{path} must be {'an' if kinds[0][0] in 'aeiou' else 'a'} {kinds[0]}"
            return f"{path} must be one of the types {', '.join(kinds)}"
    if "enum" in schema and _json(value) not in {_json(v) for v in schema["enum"]}:
        return f"{path} must be one of {_json(schema['enum'])}"
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                return f"{path}.{name} is required"
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    return f"{path}.{name} is not a known argument"
        for name, sub in properties.items():
            if name in value:
                problem = check_arguments(sub, value[name], f"{path}.{name}")
                if problem is not None:
                    return problem
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{path} must be at least {schema['minLength']} character(s) long"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} must be at most {schema['maxLength']} character(s) long"
        if "pattern" in schema and ecma_pattern(schema["pattern"]).search(value) is None:
            return f"{path} does not match the pattern {schema['pattern']}"
    elif isinstance(value, list):
        if "items" in schema:
            for n, item in enumerate(value):
                problem = check_arguments(schema["items"], item, f"{path}[{n}]")
                if problem is not None:
                    return problem
    elif not isinstance(value, bool) and isinstance(value, (int, float)):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} must be at least {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} must be at most {schema['maximum']}"
    if "not" in schema and check_arguments(schema["not"], value, path) is None:
        return f"{path} is in a form it must not take"
    if "anyOf" in schema and all(check_arguments(sub, value, path) is not None for sub in schema["anyOf"]):
        return f"{path} matches none of the allowed forms"
    return None


# -- the legacy table and forwarding ----------------------------------------------------------------------------
def tool_table() -> tuple[Tool, ...]:
    """The legacy tools, in the order control/mcp_tools.py lists them."""

    descriptors = {d["name"]: d for d in mcp_tools.descriptors()}
    tools = []
    for entry in mcp_tools.TOOLS:
        descriptor = copy.deepcopy(descriptors[entry["name"]])
        require_supported(descriptor["inputSchema"], entry["name"])
        if descriptor["inputSchema"].get("type") != "object":
            raise SchemaUnsupported(f"{entry['name']}: a tool's input schema must be an object")
        tools.append(Tool(entry["name"], entry["method"], descriptor))
    return tuple(tools)


def forward(api: Any, tool: Tool, arguments: dict) -> dict:
    """Send one call to the control API; the ``tools/call`` result, as control/mcp_tools.call_tool makes it."""

    answer = api.call(tool.method, arguments or {})
    # An answer that is not an object raises here (it has no .get), and the session answers that call
    # as an outcome it does not know.
    return {"content": [{"type": "text", "text": json.dumps(answer, sort_keys=True)}],
            "structuredContent": answer, "isError": answer.get("code") not in SUCCESS_CODES}
