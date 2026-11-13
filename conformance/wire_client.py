#!/usr/bin/env python3
"""A client of the published control records that knows nothing but the schemas and their fixtures.

It reads the JSON Schemas under ``schemas/control/v1`` and ``schemas/plugin/v1`` and the fixtures beside
them, and checks that every good fixture validates and every bad fixture is refused, for the reason the
fixture index gives. It imports the standard library and ``jsonschema`` (Draft 2020-12) and nothing from
the product: a client in any language can do the same from the same files.

    python conformance/wire_client.py            # prints one line per fixture, exits 1 on any failure
    python -m unittest discover -s conformance   # the same checks as tests

The fixture index (``fixtures/index.json`` in each schema folder) lists every fixture file::

    {"file": "good/event.v1/h01-state-changed.json", "schema": "event.v1.json", "shows": "..."}
    {"file": "bad/event.v1/b01-seq-zero.json", "schema": "event.v1.json", "rule": "...",
     "expect": {"keyword": "minimum", "at": "/seq"}}

``schema`` may carry a fragment (``common.v1.json#/$defs/cursor``). A file under ``good/`` must validate. A
file under ``bad/`` must be refused, and one of the validator's errors must be the keyword ``expect`` names
at the instance location it names, so a fixture cannot be refused by accident for some other reason.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

REPO = Path(__file__).resolve().parent.parent
SCHEMA_FOLDERS = ("schemas/control/v1", "schemas/plugin/v1")
DIALECT = "https://json-schema.org/draft/2020-12/schema"
MIN_GOOD = MIN_BAD = 3


class ConformanceError(Exception):
    """The published files themselves are inconsistent (not a fixture verdict)."""


def read_json(path: Path) -> Any:
    def no_duplicates(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                raise ConformanceError(f"{path}: duplicate key {key!r}")
            seen[key] = value
        return seen

    def no_constants(name):
        raise ConformanceError(f"{path}: {name} is not JSON")

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates,
                          parse_constant=no_constants)
    except ValueError as exc:
        raise ConformanceError(f"{path}: {exc}") from None


class SchemaSet:
    """Every ``*.json`` schema of one folder, each registered under its own ``$id`` (its file name)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.schemas: dict[str, dict] = {}
        for path in sorted(folder.glob("*.json")):
            schema = read_json(path)
            if not isinstance(schema, dict) or "$schema" not in schema:
                continue  # a data file, not a schema
            if schema.get("$schema") != DIALECT:
                raise ConformanceError(f"{path}: $schema must be {DIALECT}")
            if schema.get("$id") != path.name:
                raise ConformanceError(f"{path}: $id must be the file name")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise ConformanceError(f"{path}: not a valid schema: {exc.message}") from None
            self.schemas[path.name] = schema
        self.registry = Registry().with_resources(
            (name, Resource(contents=schema, specification=DRAFT202012)) for name, schema in self.schemas.items())

    def resolve(self, ref: str, base: str = "") -> Any:
        """The schema a ``$ref`` names, or raise ``ConformanceError``."""
        try:
            return self.registry.resolver(base_uri=base).lookup(ref).contents
        except Exception as exc:  # noqa: BLE001 - every resolution failure is the same finding
            raise ConformanceError(f"unresolved $ref {ref!r} (from {base or 'the index'}): {type(exc).__name__}") from None

    def validator(self, ref: str) -> Draft202012Validator:
        self.resolve(ref)
        return Draft202012Validator({"$schema": DIALECT, "$ref": ref}, registry=self.registry)

    def refs(self) -> Iterator[tuple[str, str]]:
        """Every ``(file, $ref)`` written anywhere in the folder's schemas."""
        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref" and isinstance(value, str):
                        yield value
                    else:
                        yield from walk(value)
            elif isinstance(node, list):
                for item in node:
                    yield from walk(item)

        for name, schema in self.schemas.items():
            for ref in walk(schema):
                yield name, ref


def location_of(error) -> str:
    """Where in the fixture an error is, as a JSON Pointer ('' for the whole value)."""
    return "".join(f"/{part}" for part in error.absolute_path)


def error_objects(validator: Draft202012Validator, instance: Any) -> list:
    """Every error the validator reports, including those inside anyOf and oneOf."""
    found = []

    def visit(error):
        found.append(error)
        for inner in error.context or ():
            visit(inner)

    for error in validator.iter_errors(instance):
        visit(error)
    return found


def leaf_errors(validator: Draft202012Validator, instance: Any) -> list[tuple[str, str, str]]:
    """Every error as ``(keyword, instance location, message)``, including those inside anyOf and oneOf."""
    return [(str(error.validator), location_of(error), error.message) for error in error_objects(validator, instance)]


def load_index(folder: Path) -> list[dict]:
    index = read_json(folder / "fixtures" / "index.json")
    entries = index.get("fixtures") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        raise ConformanceError(f"{folder}/fixtures/index.json: no 'fixtures' list")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str) or not isinstance(entry.get("schema"), str):
            raise ConformanceError(f"{folder}/fixtures/index.json: an entry needs 'file' and 'schema'")
        kind = entry["file"].split("/", 1)[0]
        if kind == "good":
            if not isinstance(entry.get("shows"), str) or not entry["shows"]:
                raise ConformanceError(f"{entry['file']}: a good fixture says what it shows")
        elif kind == "bad":
            expect = entry.get("expect")
            if not isinstance(entry.get("rule"), str) or not entry["rule"] or not isinstance(expect, dict) \
                    or not isinstance(expect.get("keyword"), str) or not isinstance(expect.get("at"), str):
                raise ConformanceError(f"{entry['file']}: a bad fixture names its rule and what to expect")
        else:
            raise ConformanceError(f"{entry['file']}: fixtures live under good/ or bad/")
    return entries


def fixture_files(folder: Path) -> set[str]:
    root = folder / "fixtures"
    return {path.relative_to(root).as_posix() for kind in ("good", "bad") for path in (root / kind).rglob("*.json")}


def check_entry(schemas: SchemaSet, entry: dict) -> str | None:
    """None when the fixture behaves as the index says, else what went wrong."""
    instance = read_json(schemas.folder / "fixtures" / entry["file"])
    errors = leaf_errors(schemas.validator(entry["schema"]), instance)
    if entry["file"].startswith("good/"):
        if errors:
            keyword, location, message = errors[0]
            return f"refused, but it is a good fixture: {keyword} at {location or '/'}: {message[:200]}"
        return None
    if not errors:
        return f"accepted, but it breaks a rule: {entry['rule']}"
    expect = entry["expect"]
    if not any(keyword == expect["keyword"] and location == expect["at"] for keyword, location, _ in errors):
        seen = sorted({f"{keyword} at {location or '/'}" for keyword, location, _ in errors})
        return f"refused, but not for its own rule ({expect['keyword']} at {expect['at'] or '/'}); saw: {'; '.join(seen)[:400]}"
    return None


def folders() -> list[Path]:
    return [REPO / name for name in SCHEMA_FOLDERS if (REPO / name).is_dir()]


def main() -> int:
    failures = total = 0
    for folder in folders():
        schemas = SchemaSet(folder)
        for entry in load_index(folder):
            total += 1
            problem = check_entry(schemas, entry)
            failures += problem is not None
            print(f"{'FAIL' if problem else 'ok  '} {folder.relative_to(REPO).as_posix()}/fixtures/{entry['file']}"
                  + (f": {problem}" if problem else ""))
    print(f"{total} fixtures, {failures} failed")
    return 1 if failures or not total else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConformanceError as exc:
        print(f"conformance files are inconsistent: {exc}", file=sys.stderr)
        raise SystemExit(2)
