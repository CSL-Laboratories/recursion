"""The tools this slice serves: the four run verbs, exactly as ``control/mcp_tools.py`` describes them.

``control/mcp_tools.py`` (lane CONTROL's file) holds the four descriptors with their names, titles,
input schemas and marks by effect (decided Nov 7, "All picks"; stop ``destructiveHint: true``,
``openWorldHint: false``). This module reads them; it copies, renames and re-marks nothing, so there is
one source until the tool table projected from the method table replaces it (the lane's next branch).
Only these four are served: every one is implemented by the control API over the daemon's socket.

``check_arguments`` checks a call's arguments against the tool's own ``inputSchema`` before anything
is sent to the daemon: 2025-06-18 answers invalid arguments as a protocol error (``-32602``), and a call
that does not match what the server advertised must have no side effect. It knows only the schema
keywords the descriptors use; a descriptor with any other keyword is refused when the table is built
(``SchemaUnsupported``), so a schema is never half checked.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from recursion_core.control import mcp_tools

STOP_METHOD = "run.stop"

_SUPPORTED = frozenset({"type", "properties", "required", "additionalProperties", "minLength", "maxLength",
                        "minimum", "maximum"})
_TYPES = frozenset({"object", "string", "integer"})


class SchemaUnsupported(ValueError):
    """A descriptor's input schema uses a keyword ``check_arguments`` does not implement."""


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    method: str
    descriptor: dict  # the ``tools/list`` entry, as control/mcp_tools.descriptors() gives it

    @property
    def input_schema(self) -> dict:
        return self.descriptor["inputSchema"]

    @property
    def is_stop(self) -> bool:
        return self.method == STOP_METHOD


def _require_supported(schema: Any, where: str) -> None:
    if not isinstance(schema, Mapping):
        raise SchemaUnsupported(f"{where}: a schema must be an object")
    unknown = set(schema) - _SUPPORTED
    if unknown:
        raise SchemaUnsupported(f"{where}: unsupported keyword(s) {sorted(unknown)}")
    if schema.get("type") not in _TYPES:
        raise SchemaUnsupported(f"{where}: type must be one of {sorted(_TYPES)}")
    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        raise SchemaUnsupported(f"{where}: additionalProperties may only be false")
    for name, sub in schema.get("properties", {}).items():
        _require_supported(sub, f"{where}.{name}")


def tool_table() -> tuple[Tool, ...]:
    """The served tools, in the order control/mcp_tools.py lists them."""

    descriptors = {d["name"]: d for d in mcp_tools.descriptors()}
    tools = []
    for entry in mcp_tools.TOOLS:
        descriptor = copy.deepcopy(descriptors[entry["name"]])
        _require_supported(descriptor["inputSchema"], entry["name"])
        if descriptor["inputSchema"].get("type") != "object":
            raise SchemaUnsupported(f"{entry['name']}: a tool's input schema must be an object")
        tools.append(Tool(entry["name"], entry["method"], descriptor))
    return tuple(tools)


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "string":
        return isinstance(value, str)
    # "integer": a JSON integer. bool is an int in Python and is not one; 1.0 is refused, as run.list refuses it.
    return type(value) is int


def check_arguments(schema: Mapping, value: Any, path: str = "arguments") -> str | None:
    """The first way ``value`` breaks ``schema``, as one line, or ``None`` when it conforms."""

    kind = schema["type"]
    if not _type_ok(kind, value):
        return f"{path} must be {'an' if kind[0] in 'aeiou' else 'a'} {kind}"
    if kind == "object":
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
    elif kind == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{path} must be at least {schema['minLength']} character(s) long"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} must be at most {schema['maxLength']} character(s) long"
    else:
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} must be at least {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} must be at most {schema['maximum']}"
    return None
