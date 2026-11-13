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
from contextlib import contextmanager
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


SWEPT = ("additionalProperties", "required", "enum", "const")
DATA_FILES = {"methods.table.v1.json": "methods.v1.json"}  # a data file, and the schema whose root describes it


class Rule:
    """One rule a schema states, which can be taken away to see whether any fixture notices.

    ``additionalProperties: false`` (taken away by setting it to true), one name in a ``required`` list
    (taken away by dropping that name), an ``enum`` and a ``const`` (taken away by deleting the keyword).
    """

    def __init__(self, kind: str, file: str, pointer: str, node: dict, name: str | None = None) -> None:
        self.kind, self.file, self.pointer, self.node, self.name = kind, file, pointer, node, name

    @property
    def key(self) -> tuple:
        return (self.kind, id(self.node), self.name)

    def __str__(self) -> str:
        what = f"required '{self.name}'" if self.kind == "required" else self.kind
        return f"{self.file}#{self.pointer or '/'}: {what}"

    @contextmanager
    def taken_away(self) -> Iterator[None]:
        """The rule is gone from the loaded schema inside the ``with`` block and back afterwards."""
        saved = self.node[self.kind]
        if self.kind == "additionalProperties":
            self.node[self.kind] = True
        elif self.kind == "required":
            self.node[self.kind] = [name for name in saved if name != self.name]
        else:
            del self.node[self.kind]
        try:
            yield
        finally:
            self.node[self.kind] = saved


def rules_of(schemas: SchemaSet) -> list[Rule]:
    """Every rule of the four swept kinds, in every schema of the folder.

    The walk follows the schema, not the JSON: a property that happens to be called ``required`` or
    ``enum`` is a name, not a keyword. What stands inside an ``if`` or a ``not`` is a condition or a
    prohibition, not a rule of this kind, and is not listed.
    """
    found: list[Rule] = []

    def walk(schema, file, pointer):
        if not isinstance(schema, dict):
            return
        if schema.get("additionalProperties") is False:
            found.append(Rule("additionalProperties", file, pointer, schema))
        if isinstance(schema.get("required"), list):
            found.extend(Rule("required", file, pointer, schema, name) for name in schema["required"])
        if "enum" in schema:
            found.append(Rule("enum", file, pointer, schema))
        if "const" in schema:
            found.append(Rule("const", file, pointer, schema))
        for key in ("properties", "$defs", "patternProperties", "dependentSchemas"):
            for name, sub in (schema.get(key) or {}).items():
                walk(sub, file, f"{pointer}/{key}/{name}")
        for key in ("items", "additionalProperties", "propertyNames", "contains", "then", "else"):
            walk(schema.get(key), file, f"{pointer}/{key}")
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            for i, sub in enumerate(schema.get(key) or ()):
                walk(sub, file, f"{pointer}/{key}/{i}")

    for file, schema in sorted(schemas.schemas.items()):
        walk(schema, file, "")
    return found


def rules_broken_by(schemas: SchemaSet, ref: str, instance: Any, rules: dict) -> list[tuple]:
    """Which of ``rules`` (by key) refuse ``instance``, each with the place in the instance where it does."""
    broken = []
    for error in error_objects(schemas.validator(ref), instance):
        kind = str(error.validator)
        if kind not in SWEPT:
            continue
        names = [None]
        if kind == "required" and isinstance(error.instance, dict):
            names = [name for name in error.validator_value if name not in error.instance]
        for name in names:
            key = (kind, id(error.schema), name)
            if key in rules:
                broken.append((key, location_of(error)))
    return broken


def proofs_of(schemas: SchemaSet, rules: list[Rule]) -> dict:
    """For each rule (by key), the bad fixtures it refuses with the very error the fixture names.

    A bad fixture names one keyword at one place. It can only prove a rule that is the one refusing it
    there: a rule that also happens to refuse it somewhere else is not what the fixture is about.
    """
    by_key = {rule.key: rule for rule in rules}
    found: dict = {}
    for entry in load_index(schemas.folder):
        if not entry["file"].startswith("bad/") or entry["expect"]["keyword"] not in SWEPT:
            continue
        instance = read_json(schemas.folder / "fixtures" / entry["file"])
        for key, location in rules_broken_by(schemas, entry["schema"], instance, by_key):
            if key[0] == entry["expect"]["keyword"] and location == entry["expect"]["at"]:
                proofs = found.setdefault(key, [])
                if not any(entry is listed for listed, _ in proofs):
                    proofs.append((entry, instance))
    return found


def data_file_notices(schemas: SchemaSet, rule: Rule) -> bool:
    """Whether a data file of the folder proves a rule at the root of the schema that describes it.

    The method table is data, not a fixture, and nothing else is an instance of its schema's root. Its
    bad instance is the table itself with one change: a field nobody defined added, or a required field
    gone. Taking the rule away must make that changed table stop being refused for it.
    """
    for data_name, schema_name in DATA_FILES.items():
        path = schemas.folder / data_name
        if rule.file != schema_name or rule.pointer or not path.is_file():
            continue
        spoiled = read_json(path)
        if rule.kind == "additionalProperties":
            spoiled["x-nobody-defined-this"] = True
        elif rule.kind == "required" and rule.name in spoiled:
            del spoiled[rule.name]
        else:
            continue

        def refused() -> bool:
            errors = leaf_errors(schemas.validator(schema_name), spoiled)
            return any(keyword == rule.kind and location == "" for keyword, location, _ in errors)

        if refused():
            with rule.taken_away():
                if not refused():
                    return True
    return False


def unproved_rules(schemas: SchemaSet) -> list[Rule]:
    """The rules that can be taken away, one at a time, without any bad fixture noticing.

    A fixture notices when, with the rule gone, it is accepted or is no longer refused for its own rule.
    """
    rules = rules_of(schemas)
    proofs = proofs_of(schemas, rules)
    unproved = []
    for rule in rules:
        with rule.taken_away():
            noticed = any(check_instance(schemas, entry, instance) is not None
                          for entry, instance in proofs.get(rule.key, ()))
        if not noticed and not data_file_notices(schemas, rule):
            unproved.append(rule)
    return unproved


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
    return check_instance(schemas, entry, read_json(schemas.folder / "fixtures" / entry["file"]))


def check_instance(schemas: SchemaSet, entry: dict, instance: Any) -> str | None:
    """``check_entry`` for a fixture that is already read."""
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
