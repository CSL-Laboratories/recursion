#!/usr/bin/env python3
"""Check the desktop's golden requests against a checkout of SPINE's draft schemas (pull request #368).

    python -X utf8 -B desktop/tests/sync_spine.py <checkout of #368>            # check, print one line each
    python -X utf8 -B desktop/tests/sync_spine.py <checkout of #368> --write    # rewrite spine-rows.json

It needs ``jsonschema`` (hosted CI's interpreter has it; the free-threaded one does not) and reads nothing
but the checkout's ``schemas/control/v1`` folder and this folder's fixtures. It is not a test: the schemas
are not on this branch's base, so the checks under ``desktop/tests`` read the rows this script recorded in
``fixtures/spine-rows.json`` instead, and this script is how those rows are made and kept true.

For every action of ``fixtures/actions.json`` whose source is a SPINE fixture it checks that

* the fixture file is the one recorded (its SHA-256);
* the action's method is the fixture's, and its params equal the fixture's except in the keys the action's
  ``deltas`` name, each with its reason;
* the params validate against the method's params schema in ``methods.v1.json``.

An action that fails only where its ``schema_gap`` says it will is reported ``GAP``, not ``FAIL``. Every
action, whatever its source, must name a method whose row is in the table, may be called by the page, and
is not of the manage scope. The exit status is 1 when anything is ``FAIL``, or when ``spine-rows.json``
differs from what this checkout gives (without ``--write``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
ACTIONS = FIXTURES / "actions.json"
ROWS = FIXTURES / "spine-rows.json"
SCHEMA_DIR = "schemas/control/v1"
ROW_FIELDS = ("lane", "scope", "mutating", "idempotency", "precondition", "page_allowed", "beta")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _registry(schema_dir: Path):
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    resources = []
    for path in sorted(schema_dir.glob("*.json")):
        doc = _load(path)
        if "$id" in doc:
            resources.append((doc["$id"], Resource(contents=doc, specification=DRAFT202012)))
    return Registry().with_resources(resources)


def _params_errors(registry, methods_id: str, method: str, params: dict) -> list[str]:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator({"$ref": f"{methods_id}#/$defs/params.{method}"}, registry=registry)
    return sorted(f"{'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.message}" for e in validator.iter_errors(params))


def build_rows(checkout: Path, head: str, methods_used: list[str]) -> dict:
    schema_dir = checkout / SCHEMA_DIR
    table = _load(schema_dir / "methods.table.v1.json")
    methods = _load(schema_dir / "methods.v1.json")
    approval = _load(schema_dir / "approval.v1.json")
    by_name = {row["name"]: row for row in table["methods"]}
    rows = {}
    for name in sorted(set(methods_used)):
        row = by_name.get(name)
        if row is None:
            rows[name] = None
            continue
        params = methods["$defs"][f"params.{name}"]
        rows[name] = {
            **{field: row[field] for field in ROW_FIELDS},
            "params": {
                "required": sorted(params.get("required", [])),
                "properties": sorted(params.get("properties", {})),
                "additionalProperties": params.get("additionalProperties", True),
            },
        }
    return {
        "about": "Rows of SPINE's draft method table for the methods the desktop's actions use, as sync_spine.py read "
                 "them from pull request #368 at spine_head. Made by that script with --write; not edited by hand.",
        "spine_head": head,
        "table_version": table["table_version"],
        "sources": {name: _sha256(schema_dir / name)
                    for name in ("methods.table.v1.json", "methods.v1.json", "approval.v1.json")},
        "surfaces": {
            "approval": approval["$defs"]["surface"]["enum"],
            "surface.prefs": methods["$defs"]["params.surface.prefs.get"]["properties"]["surface"]["enum"],
        },
        "rows": rows,
    }


def _checkout_head(checkout: Path) -> str | None:
    import subprocess

    try:
        out = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=False)
    except OSError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def check(checkout: Path, write: bool) -> int:
    actions = _load(ACTIONS)
    head = actions["spine_head"]
    found = _checkout_head(checkout)
    if found != head:
        print(f"FAIL the checkout is at {found}, the actions name spine_head {head}: check out that head")
        return 1
    schema_dir = checkout / SCHEMA_DIR
    fixture_dir = checkout / actions["spine_fixture_dir"]
    methods_id = _load(schema_dir / "methods.v1.json")["$id"]
    registry = _registry(schema_dir)
    rows = build_rows(checkout, head, [a["method"] for a in actions["actions"]])
    failed = 0
    for action in actions["actions"]:
        name, method, params = action["action"], action["method"], action["params"]
        problems = []
        row = rows["rows"].get(method)
        if row is None:
            problems.append(f"{method} has no row in the table")
        else:
            if not row["page_allowed"]:
                problems.append(f"{method} is not allowed to the page")
            if row["scope"] == "manage":
                problems.append(f"{method} is of the manage scope")
        source = action["source"]
        if source["kind"] == "spine":
            path = fixture_dir / source["file"]
            if _sha256(path) != source["sha256"]:
                problems.append(f"{source['file']} is not the recorded file")
            fixture = _load(path)
            if fixture.get("method") != method:
                problems.append(f"the fixture's method is {fixture.get('method')}")
            theirs = fixture.get("params", {})
            deltas = action.get("deltas", {})
            for key in sorted(set(theirs) | set(params)):
                if key in deltas:
                    continue
                if theirs.get(key, "<absent>") != params.get(key, "<absent>"):
                    problems.append(f"params.{key} differs from the fixture and no delta names it")
            for key in deltas:
                if theirs.get(key, "<absent>") == params.get(key, "<absent>"):
                    problems.append(f"delta {key} is named but the value is the fixture's")
        schema_problems = _params_errors(registry, methods_id, method, params) if row is not None else []
        if problems:
            verdict = "FAIL"
        elif schema_problems:
            verdict = "GAP" if action.get("schema_gap") else "FAIL"
        else:
            verdict = "OK"
        if action.get("schema_gap") and not schema_problems:
            verdict = "FAIL"
            problems.append("a schema gap is declared but the params validate: the gap has closed, update the action")
        failed += verdict == "FAIL"
        print(f"{verdict:4} {name:20} {method:20} " + "; ".join(problems + schema_problems))
    recorded = _load(ROWS) if ROWS.exists() else None
    if write:
        ROWS.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {ROWS.relative_to(HERE.parent.parent)}")
    elif recorded != rows:
        failed += 1
        print("FAIL spine-rows.json differs from this checkout (run with --write after reading the difference)")
    else:
        print("OK   spine-rows.json matches this checkout")
    print(f"table {rows['table_version']} at {head[:8]}: {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    return check(args.checkout, args.write)


if __name__ == "__main__":
    sys.exit(main())
