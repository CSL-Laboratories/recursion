#!/usr/bin/env python3
"""A client of the published control records that knows nothing but the schemas, their fixtures and
their vectors.

It reads the JSON Schemas under ``schemas/control/v1`` and ``schemas/plugin/v1`` and the fixtures beside
them, and checks that every good fixture validates and every bad fixture is refused, for the reason the
fixture index gives. It also checks the vectors (``vectors/index.json``) against the rules a schema
cannot state, which ``rules.py`` beside this file states as code. It imports the standard library and
``jsonschema`` (Draft 2020-12) and nothing from the product: a client in any language can do the same
from the same files.

    python -m unittest discover -s conformance   # everything, the consistency checks included
    python conformance/wire_client.py            # the fixtures and the vectors only, one line each

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

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import rules  # noqa: E402  beside this file: the rules a schema cannot state (standard library only)

REPO = HERE.parent
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


VECTOR_RULES = ("call.profile", "approval.binding", "approval.lifecycle", "transcript.projection", "queue.delivery",
                "task.derivation")


def load_vectors(folder: Path) -> list[dict]:
    """The vectors of a folder (``vectors/index.json``), or none when the folder has no vectors.

    A fixture is one record checked against one schema. A vector is checked against a RULE that no
    schema can state (``rules.py``): that a digest is the digest of its effect, that the lines of an
    approval are the lines of that effect, that a sequence of calls is answered the way the protocol
    says. Each entry names its file, its rule and what it shows.
    """
    path = folder / "vectors" / "index.json"
    if not path.is_file():
        return []
    index = read_json(path)
    entries = index.get("vectors") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        raise ConformanceError(f"{path}: no 'vectors' list")
    for entry in entries:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) and entry[key]
                                                  for key in ("file", "rule", "shows")):
            raise ConformanceError(f"{path}: an entry needs 'file', 'rule' and 'shows'")
        if entry["rule"] not in VECTOR_RULES:
            raise ConformanceError(f"{entry['file']}: no rule named {entry['rule']!r}")
    return entries


def vector_files(folder: Path) -> set[str]:
    root = folder / "vectors"
    return {path.relative_to(root).as_posix() for path in root.rglob("*.json") if path.name != "index.json"} if root.is_dir() else set()


def approvals_in(value: Any) -> Iterator[dict]:
    """Every approval record inside a JSON value: on its own, in an event's payload, in a method's answer."""
    if isinstance(value, dict):
        if {"approval_id", "effect", "digest", "rendered"} <= value.keys():
            yield value
        for item in value.values():
            yield from approvals_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from approvals_in(item)


def _misfit(schemas: SchemaSet, ref: str, instance: Any) -> str | None:
    errors = leaf_errors(schemas.validator(ref), instance)
    if not errors:
        return None
    keyword, location, message = errors[0]
    return f"{keyword} at {location or '/'}: {message[:160]}"


def check_vector(schemas: SchemaSet, entry: dict) -> str | None:
    """None when the vector agrees with its rule, else what does not."""
    return judge_vector(schemas, entry["rule"], read_json(schemas.folder / "vectors" / entry["file"]))


def judge_vector(schemas: SchemaSet, rule: str, vector: dict) -> str | None:
    """The same for a vector that is already read, so that a test can hand over one it has changed."""
    if rule == "call.profile":
        # One tool call as an engine wrote it, and the text that is bound for it, or the reason it is refused.
        expect = vector["expect"]
        try:
            call = rules.call_of(vector["tool"], vector["input_text"], vector["cwd"])
        except rules.Refused as refusal:
            return None if expect.get("refused") == refusal.code else f"refused as '{refusal.code}', expected {expect}"
        text = rules.text_of(call)
        if "refused" in expect:
            return f"taken as {text[:80]}, expected the refusal '{expect['refused']}'"
        if text != expect["call_text"]:
            return f"the call's text is {text[:200]!r}, expected {expect['call_text'][:200]!r}"
        effect = {"kind": "engine_tool", "engine": "claude_code", "session_id": None, "run_id": "run-vector", **call}
        misfit = _misfit(schemas, "approval.v1.json#/$defs/engine_tool_effect", effect)
        return f"the call does not fit an engine-tool effect: {misfit}" if misfit else None
    if rule == "approval.binding":
        # One approval record of the right shape, and exactly what does not agree inside it.
        record = vector["record"]
        misfit = _misfit(schemas, "approval.v1.json", record)
        if misfit:
            return f"the record does not fit approval.v1: {misfit}"
        found = rules.approval_problems(record)
        if found != vector["expect"]:
            return f"the rule finds {found}, the vector says {vector['expect']}"
        if "effect_text" in vector:      # the bytes the digest is taken over, written out
            try:
                text = rules.effect_bytes(record["digest_form"], record["effect"]).decode("utf-8")
            except rules.Refused as refusal:
                return f"the effect has no canonical bytes ({refusal.code}), and the vector gives some"
            if text != vector["effect_text"]:
                return "the effect's canonical text differs from the vector's"
        return None
    if rule == "approval.lifecycle":
        # One request, and calls on it one after another: each answer and each record after it.
        model = rules.ApprovalModel(vector["record"])
        records = [("the starting record", vector["record"])]
        for number, step in enumerate(vector["steps"], 1):
            request = step["request"]
            answer = model.call(step["principal"], request["method"], request["params"], step["at"])
            if answer != step["answer"]:
                return f"step {number}: the rule answers {answer}, the vector says {step['answer']}"
            if model.record != step["record"]:
                return f"step {number}: the record after the call is not the vector's"
            misfit = _misfit(schemas, "methods.v1.json#/$defs/exchange", dict(request, answer=answer))
            if misfit:
                return f"step {number}: the call and its answer do not fit the method table: {misfit}"
            records.append((f"step {number}", step["record"]))
        for where, record in records:
            misfit = _misfit(schemas, "approval.v1.json", record)
            if misfit:
                return f"{where}: the record does not fit approval.v1: {misfit}"
            found = rules.approval_problems(record)
            if found:
                return f"{where}: the record does not agree with itself: {found}"
        return None
    if rule == "transcript.projection":
        # A log from its first event, and the lines of its transcript: where a line is not what its event says.
        for number, event in enumerate(vector["events"], 1):
            misfit = _misfit(schemas, "event.v1.json", event)
            if misfit:
                return f"event {number} does not fit event.v1: {misfit}"
        for number, line in enumerate(vector["lines"], 1):
            misfit = _misfit(schemas, "transcript.line.v1.json", line)
            if misfit:
                return f"line {number} does not fit transcript.line.v1: {misfit}"
        found = rules.transcript_problems(vector["events"], vector["lines"])
        return None if found == vector["expect"] else f"the rule finds {found}, the vector says {vector['expect']}"
    if rule == "queue.delivery":
        # One queued message, and what happens to it step by step: the item after each, or the refusal.
        # A step may carry the event of the log that belongs to it. An acceptance always does: the only
        # thing that makes an item 'delivered' is an input.accepted event that names its delivery.
        item = vector["item"]
        records = [("the starting item", item)]
        for number, step in enumerate(vector["steps"], 1):
            event = step.get("event")
            if event is not None:
                misfit = _misfit(schemas, "event.v1.json", event)
                if misfit:
                    return f"step {number}: the event does not fit event.v1: {misfit}"
            if step["happening"] == "accepted":
                facts = step.get("facts") or {}
                if event is None or event["type"] != "input.accepted" or event["turn_id"] != facts.get("turn_id") or any(
                        event["payload"][key] != facts.get(key) for key in ("delivery_id", "turn_id")):
                    return f"step {number}: an acceptance is an input.accepted event that names the same delivery and turn"
            try:
                after = rules.queue_step(item, step["happening"], step.get("facts"))
            except rules.Refused as refusal:
                if step.get("refused") != refusal.code:
                    return f"step {number}: refused as '{refusal.code}', the vector says {step.get('refused') or step.get('item')}"
                continue
            if "refused" in step:
                return f"step {number}: taken, the vector says refused '{step['refused']}'"
            if after != step["item"]:
                return f"step {number}: the item after is {after}, the vector says {step['item']}"
            item = after
            records.append((f"step {number}", after))
        for where, record in records:
            misfit = _misfit(schemas, "queue.item.v1.json", record)
            if misfit:
                return f"{where}: the item does not fit queue.item.v1: {misfit}"
        return None
    if rule == "task.derivation":
        # A run's view or a session's turn, its log, and the Task derived from them.
        for number, event in enumerate(vector["events"], 1):
            misfit = _misfit(schemas, "event.v1.json", event)
            if misfit:
                return f"event {number} does not fit event.v1: {misfit}"
        misfit = _misfit(schemas, "mcp.task.v1.json", vector["task"])
        if misfit:
            return f"the task does not fit mcp.task.v1: {misfit}"
        if vector["source"] == "run":
            misfit = _misfit(schemas, "run.view.v1.json", vector["view"])
            wanted = None if misfit else rules.task_of_run(vector["view"], vector["events"], vector["pending_approval_ids"])
        else:
            misfit = _misfit(schemas, "session.v1.json", vector["session"])
            wanted = None if misfit else rules.task_of_turn(vector["session"], vector["turn_id"], vector["events"],
                                                            vector["pending_approval_ids"])
        if misfit:
            return f"the source record does not fit its schema: {misfit}"
        found = rules.task_problems(wanted, vector["task"])
        return None if found == vector["expect"] else f"the rule finds {found}, the vector says {vector['expect']}"
    return f"no rule named {rule!r}"


def folders() -> list[Path]:
    return [REPO / name for name in SCHEMA_FOLDERS if (REPO / name).is_dir()]


def main() -> int:
    """One line for every fixture and every vector. This is NOT everything: the checks that the published
    files agree with each other (``tests.py``) run only with ``python -m unittest discover -s conformance``.
    """
    failures = total = 0
    for folder in folders():
        schemas = SchemaSet(folder)
        name = folder.relative_to(REPO).as_posix()
        for entry in load_index(folder):
            total += 1
            problem = check_entry(schemas, entry)
            failures += problem is not None
            print(f"{'FAIL' if problem else 'ok  '} {name}/fixtures/{entry['file']}" + (f": {problem}" if problem else ""))
            if problem is None and entry["file"].startswith("good/"):
                for record in approvals_in(read_json(folder / "fixtures" / entry["file"])):
                    found = rules.approval_problems(record)
                    if found:
                        failures += 1
                        print(f"FAIL {name}/fixtures/{entry['file']}: an approval in it does not agree with itself: {found}")
        for entry in load_vectors(folder):
            total += 1
            problem = check_vector(schemas, entry)
            failures += problem is not None
            print(f"{'FAIL' if problem else 'ok  '} {name}/vectors/{entry['file']}" + (f": {problem}" if problem else ""))
    print(f"{total} fixtures and vectors, {failures} failed. The consistency checks are not run by this command: "
          "python -m unittest discover -s conformance")
    return 1 if failures or not total else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConformanceError as exc:
        print(f"conformance files are inconsistent: {exc}", file=sys.stderr)
        raise SystemExit(2)
