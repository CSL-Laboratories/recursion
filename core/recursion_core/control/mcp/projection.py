"""The tool table projected from the published method table, joined with what the daemon implements now.

THE PUBLISHED TABLE is ``schemas/control/v1/methods.table.v1.json`` (lane SPINE's) with the schema files it
refers to. Each row carries an ``mcp`` object: ``exposed``, the tool's name and its annotations (the marks by
effect). A row's ``params`` names its parameter schema in ``methods.v1.json``; ``inline`` makes it
self-contained (an MCP client is handed one schema per tool and resolves no reference), refusing a reference
cycle, a reference with sibling keywords, or a target that does not exist.

WHAT THE DAEMON IMPLEMENTS comes from ``methods.list``: ``{code: "ok", table_version, methods: [{name,
implemented, beta}]}``, the published answer's shape and no other (``answer.methods.list`` in
``methods.v1.json``: those members exactly, ``implemented`` and ``beta`` booleans, each name once), checked
before any table is chosen. ``project`` advertises, in the table's order, exactly the rows that are
``mcp.exposed`` AND that the daemon lists as implemented, under their published tool names and with their
published annotations, only when the daemon's ``table_version`` is the published one. A row that is not
exposed is never a tool, whatever the daemon implements: ``approval.open`` and ``approval.answer`` (no
approval is answered through MCP), ``events.follow``, ``operator.read``, ``workspace.publish``, ``plugin.call``,
``surface.prefs.get``/``set`` and ``methods.list`` itself. A verb whose row is added to the table appears as a
tool, with its mark, with no change here. A tool's description is written from its row (method, scope, the
codes it answers); the table publishes no title.

``choose_tools`` is the server's choice at start:

* the daemon answers ``methods.list`` with ``method_unknown``: it is from before the method table, and the
  LEGACY four (``tools.tool_table``) are served, as slice 1 does;
* it answers ``ok`` in another shape: refused (``listing_invalid``), whatever comes next;
* it answers ``ok`` and the published files are present: the projected table; a different ``table_version``
  refuses (``table_version_mismatch``), because a schema advertised for another version of the table could
  be wrong;
* it answers ``ok`` and the published files are absent (a package installed without them): the legacy four,
  and the reason is logged. Until INSTALL ships ``schemas/control/v1/*.json`` as package data (the Liaison's
  ruling of Nov 15 on the lane's Q9), only a source tree has them;
* any other answer (``unavailable``, ``scope_refused``, the socket client's ``control_api_unavailable``)
  refuses (``listing_unavailable``), and so does a ``methods.list`` call that raises (``listing_failed``; an
  exit or an interrupt goes on).

Every refusal is a ``ProjectionRefused(reason, detail)``. Published files that are not the published shape
(a table that is not an object, a row that is not one, a row whose ``name`` is not a non-empty string or
whose ``mcp`` is not an object, an exposed row whose ``codes`` are not strings or whose ``scope`` is not one,
a ``$ref`` that is not a string, a parameter schema or pattern this server cannot check, a file or a chain
of references nested deeper than Python's stack) are ``published_incomplete`` or ``published_invalid``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import recursion_core

from .tools import SchemaUnsupported, Tool, require_supported, tool_table

TABLE_FILE = "methods.table.v1.json"
PARAMS_FILE = "methods.v1.json"
DEFAULT_DIRECTORY = Path(recursion_core.__file__).resolve().parents[2] / "schemas" / "control" / "v1"
LEGACY, PROJECTED = "legacy", "projected"


class ProjectionRefused(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


class Published:
    """The published schema files of ``schemas/control/v1``, by file name."""

    def __init__(self, documents: Mapping[str, dict]) -> None:
        if TABLE_FILE not in documents or PARAMS_FILE not in documents:
            raise ProjectionRefused("published_incomplete", f"needs {TABLE_FILE} and {PARAMS_FILE}")
        try:
            self.documents = {name: copy.deepcopy(doc) for name, doc in documents.items()}
        except RecursionError:  # an exit or an interrupt goes on
            raise ProjectionRefused("published_invalid", "a published file is nested deeper than this server reads") from None
        table = self.documents[TABLE_FILE]
        if not isinstance(table, Mapping):
            raise ProjectionRefused("published_invalid", f"{TABLE_FILE} is not an object")
        self.table_version = table.get("table_version")
        self.rows = table.get("methods")
        if not isinstance(self.table_version, str) or not isinstance(self.rows, list):
            raise ProjectionRefused("published_invalid", "the table needs table_version and methods")
        if not all(isinstance(row, Mapping) for row in self.rows):
            raise ProjectionRefused("published_invalid", "a row of the table is not an object")
        if not all(isinstance(row.get("name"), str) and row["name"] for row in self.rows):
            raise ProjectionRefused("published_invalid", "a row of the table has no name, or one that is not a string")

    @classmethod
    def from_directory(cls, directory: Path) -> Published | None:
        """The files of ``directory`` (its top level only), or ``None`` when it holds no method table."""

        directory = Path(directory)
        if not (directory / TABLE_FILE).is_file():
            return None
        documents = {}
        for path in sorted(directory.glob("*.json")):
            try:  # OSError and ValueError go on to the caller, which reads them as published_unreadable
                documents[path.name] = json.loads(path.read_text(encoding="utf-8"))
            except RecursionError:  # valid JSON nested past the parser's stack
                raise ProjectionRefused("published_invalid", f"{path.name} is nested deeper than this server reads") from None
        return cls(documents)

    def _target(self, ref: str, current: str) -> tuple[Any, str]:
        name, hash_mark, fragment = ref.partition("#")
        name = name or current
        if not hash_mark or name not in self.documents or not fragment.startswith("/"):
            raise SchemaUnsupported(f"reference {ref!r} (from {current}) names nothing published")
        node: Any = self.documents[name]
        for part in fragment[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, Mapping) or part not in node:
                raise SchemaUnsupported(f"reference {ref!r} (from {current}) names nothing published")
            node = node[part]
        return node, name

    def inline(self, schema: Any, current: str = PARAMS_FILE, _seen: tuple = ()) -> Any:
        """``schema`` with every ``$ref`` replaced by what it names, recursively."""

        if isinstance(schema, list):
            return [self.inline(item, current, _seen) for item in schema]
        if not isinstance(schema, Mapping):
            return schema
        if "$ref" in schema:
            ref = schema["$ref"]
            if not isinstance(ref, str):
                raise SchemaUnsupported(f"a reference must be a string, not {ref!r} (in {current})")
            if set(schema) != {"$ref"}:
                raise SchemaUnsupported(f"reference {ref!r} has sibling keywords {sorted(set(schema) - {'$ref'})}")
            target, name = self._target(ref, current)
            key = (name, ref.partition("#")[2])
            if key in _seen:
                raise SchemaUnsupported(f"reference cycle through {ref!r}")
            return self.inline(target, name, _seen + (key,))
        return {key: (value if key in ("enum", "required", "description") else self.inline(value, current, _seen))
                for key, value in schema.items()}


_ANSWER_MEMBERS = frozenset({"code", "table_version", "methods"})
_ENTRY_MEMBERS = frozenset({"name", "implemented", "beta"})


def _listing(answer: Any) -> dict[str, bool]:
    """``methods.list``'s ``ok`` answer as {name: implemented}; ``ProjectionRefused`` when it is not the published
    shape (``answer.methods.list``: those members exactly, each entry ``name``, ``implemented`` and ``beta``
    exactly, the two booleans, each name once)."""

    if (not isinstance(answer, Mapping) or set(answer) != _ANSWER_MEMBERS or not isinstance(answer["methods"], list)
            or not isinstance(answer["table_version"], str)):
        raise ProjectionRefused("listing_invalid", "methods.list answered ok, but not as code, table_version and methods")
    implemented: dict[str, bool] = {}
    for entry in answer["methods"]:
        if (not isinstance(entry, Mapping) or set(entry) != _ENTRY_MEMBERS or not isinstance(entry["name"], str)
                or not isinstance(entry["implemented"], bool) or not isinstance(entry["beta"], bool)
                or entry["name"] in implemented):
            raise ProjectionRefused("listing_invalid", f"a method entry is not name/implemented/beta: {entry!r}")
        implemented[entry["name"]] = entry["implemented"]
    return implemented


def _description(row: Mapping) -> str:
    """A tool's description, written from its row; ``ProjectionRefused`` for a row it cannot be written from."""

    codes, scope = row.get("codes"), row.get("scope")
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes) or not isinstance(scope, str):
        raise ProjectionRefused("published_invalid", f"row {row.get('name')!r}: codes must be strings and scope a string")
    return f"The Recursion control method {row['name']} (scope: {scope}). It answers one of: {', '.join(codes)}."


def project(published: Published, answer: Mapping) -> tuple[Tool, ...]:
    """The tools for a daemon whose ``methods.list`` answered ``answer``."""

    implemented = _listing(answer)
    if answer["table_version"] != published.table_version:
        raise ProjectionRefused("table_version_mismatch",
                                f"the daemon's table is {answer['table_version']}, the published one {published.table_version}")
    tools, names = [], set()
    for row in published.rows:  # each an object (Published)
        mcp = row.get("mcp")
        if not isinstance(mcp, Mapping):
            raise ProjectionRefused("published_invalid", f"row {row.get('name')!r}: mcp must be an object")
        if mcp.get("exposed") is not True or implemented.get(row.get("name")) is not True:
            continue
        name, annotations = mcp.get("tool"), mcp.get("annotations")
        if not isinstance(name, str) or not name or name in names or not isinstance(annotations, Mapping):
            raise ProjectionRefused("published_invalid", f"row {row.get('name')!r}: an exposed row needs a unique tool and annotations")
        try:
            schema = published.inline({"$ref": f"{PARAMS_FILE}{row['params']}"})
            require_supported(schema, name)
        except (SchemaUnsupported, KeyError, TypeError, RecursionError) as exc:  # an exit or an interrupt goes on
            reason = "nested deeper than this server reads" if isinstance(exc, RecursionError) else str(exc)
            raise ProjectionRefused("published_invalid", f"row {row.get('name')!r}: {reason}") from None
        if schema.get("type") != "object":
            raise ProjectionRefused("published_invalid", f"row {row.get('name')!r}: parameters must be an object")
        names.add(name)
        tools.append(Tool(name, row["name"], {"name": name, "description": _description(row), "inputSchema": schema,
                                              "annotations": dict(annotations)}))
    return tuple(tools)


def choose_tools(api: Any, published: Published | None) -> tuple[tuple[Tool, ...], dict]:
    """The tools to serve, and a log entry saying which table and why."""

    try:
        answer = api.call("methods.list", {})
    except Exception as exc:  # noqa: BLE001 - no listing: refused, and named; an exit or an interrupt goes on
        raise ProjectionRefused("listing_failed", type(exc).__name__) from None
    code = answer.get("code") if isinstance(answer, Mapping) else None
    if code == "method_unknown":
        return tool_table(), {"event": "tools", "table": LEGACY, "reason": "daemon_without_method_table"}
    if code != "ok":
        detail = answer.get("detail") if isinstance(answer, Mapping) else None
        raise ProjectionRefused("listing_unavailable", f"methods.list answered {code!r}" + (f" ({detail})" if detail else ""))
    _listing(answer)  # its shape is checked whatever comes next, the fallback included
    if published is None:
        return tool_table(), {"event": "tools", "table": LEGACY, "reason": "published_table_missing"}
    tools = project(published, answer)
    return tools, {"event": "tools", "table": PROJECTED, "table_version": published.table_version,
                   "tools": [t.name for t in tools]}


def default_published() -> Published | None:
    """The published files of this source tree, or ``None`` where the package was installed without them."""

    return Published.from_directory(DEFAULT_DIRECTORY)


def names(tools: Iterable[Tool]) -> list[str]:
    return [t.name for t in tools]
