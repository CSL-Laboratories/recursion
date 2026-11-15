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

An action whose only schema error is the one its ``schema_gap`` names is reported ``GAP``, not ``FAIL``
(``verdict`` below): the gap names where, which rule and which value, it must be one of ``KNOWN_GAPS``, and
the error must be that rule refusing that value at that place with the value missing from what the schema
allows. Any other error fails, beside the gap or alone, and so does a declared gap the schema no longer
shows. Every action, whatever its source, must name a method whose row is in the table, may be called by
the page, and is not of the manage scope.

``verdict``, ``source_problems`` and ``row_problems`` need neither ``jsonschema`` nor a checkout, so
``check_sync_spine.py`` holds them to cases on any interpreter.

It also keeps ``fixtures/answers.json``: the answers of the SPINE fixtures named in ``ANSWERS`` below, each
with its file and SHA-256, which the desktop's views are drawn from in ``desktop/tests/js``. The exit status
is 1 when anything is ``FAIL``, or when ``spine-rows.json`` or ``answers.json`` differs from what this checkout
gives (without ``--write``).
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
ANSWERS_FILE = FIXTURES / "answers.json"
SCHEMA_DIR = "schemas/control/v1"
ROW_FIELDS = ("lane", "scope", "mutating", "idempotency", "precondition", "page_allowed", "beta")
# (action, SPINE fixture): answers the desktop's views are drawn from. The c* files were captured from the
# daemon's own answers; the h* files were written by hand for methods the daemon does not have yet.
ANSWERS = (
    ("runs.refresh", "c09-run-list-one-page.json"),
    ("run.open", "c06-run-status.json"),
    ("run.stop", "c12-run-stop-pending.json"),
    ("run.stop", "c13-run-stop-stopped.json"),
    ("run.stop", "c14-run-stop-of-no-run.json"),
    ("run.start", "c01-run-start-started.json"),
    ("run.start", "c04-run-start-no-route-for-the-engine.json"),
    ("routes.refresh", "c15-route-list.json"),
    ("plugins.refresh", "c16-plugin-list.json"),
    ("plugins.refresh", "h51-plugin-list-with-a-loaded-and-a-refused-plugin.json"),
    ("sessions.refresh", "h27-session-list-ok.json"),
    ("session.open", "h28-session-status-ok.json"),
    ("session.start", "h20-session-start-ok.json"),
    ("session.turn", "h01-session-turn-ok.json"),
    ("session.turn", "h03-a-stale-revision.json"),
    ("session.interrupt", "h23-session-interrupt-ok.json"),
    ("session.stop", "h13-session-stop-of-a-subtree-still-pending.json"),
    ("transcript.read", "h11-events-read-as-a-transcript.json"),
    ("transcript.follow", "h50-events-follow-as-a-transcript.json"),
    ("transcript.follow", "h12-events-follow-that-timed-out.json"),
    ("approvals.refresh", "h10-approval-list-returns-summaries.json"),
    ("approval.open", "h07-approval-open.json"),
    ("approval.answer", "h31-approval-answer-ok.json"),
    ("approval.answer", "h09-a-second-answer.json"),
    ("prefs.read", "h43-surface-prefs-get-ok.json"),
    ("diff.read", "h36-workspace-diff-ok.json"),
    ("diff.read", "h05-a-method-the-daemon-has-not-built-yet.json"),
    ("run.stop", "h16-a-refusal-by-the-authorizer.json"),
    ("run.start", "h52-run-start-unconfirmed.json"),
    ("transcript.follow", "h62-a-read-asked-of-a-daemon-that-is-closing.json"),
    ("transcript.read", "h65-a-read-of-a-log-nobody-knows.json"),
    ("session.interrupt", "h55-interrupt-on-a-driver-that-cannot-interrupt.json"),
)


def build_answers(checkout: Path, head: str, fixture_dir_name: str, actions: dict) -> dict:
    fixture_dir = checkout / fixture_dir_name
    methods = {a["action"]: a["method"] for a in actions["actions"]}
    answers = []
    for action, name in ANSWERS:
        path = fixture_dir / name
        fixture = _load(path)
        answers.append({"action": action, "file": name, "sha256": _sha256(path),
                        "method": fixture.get("method", methods[action]), "answer": fixture["answer"]})
    return {
        "about": "Answers of SPINE's draft fixtures (pull request #368 at spine_head), for the desktop's views to be "
                 "drawn from. Each names the action whose view draws it and the fixture file it is copied from, "
                 "unedited. Made by sync_spine.py with --write; not edited by hand.",
        "spine_head": head,
        "answers": answers,
    }


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


def _params_errors(registry, methods_id: str, method: str, params: dict) -> list[dict]:
    """Each schema error as {at, rule, value, allowed, message}: the place in the params, the keyword that
    refused, the value it refused and what the keyword allows."""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator({"$ref": f"{methods_id}#/$defs/params.{method}"}, registry=registry)
    errors = [{"at": "/".join(map(str, e.absolute_path)) or "(top)", "rule": e.validator, "value": e.instance,
               "allowed": e.validator_value, "message": e.message} for e in validator.iter_errors(params)]
    return sorted(errors, key=lambda e: (e["at"], e["rule"], e["message"]))


# The only schema failure an action may declare: approval.v1's surface list has no "desktop" (lane DESKTOP
# questions.md, Q3). Each is (at, rule, value).
KNOWN_GAPS = (("surface", "enum", "desktop"),)


def _gap_key(gap) -> tuple | None:
    if not isinstance(gap, dict):
        return None
    return (gap.get("at"), gap.get("rule"), gap.get("value"))


def gap_waives(gap, error: dict) -> bool:
    """True only when ``error`` is the failure ``gap`` declares: a known gap, the same place, rule and value,
    and the value missing from what the schema allows there."""
    key = _gap_key(gap)
    return (key in KNOWN_GAPS and (error["at"], error["rule"], error["value"]) == key
            and isinstance(error["allowed"], list) and key[2] not in error["allowed"])


def verdict(action: dict, problems: list[str], errors: list[dict]) -> tuple[str, list[str]]:
    """OK, GAP or FAIL for one action, and what it says. ``problems`` are the fixture's and the row's,
    ``errors`` the schema's (``_params_errors``)."""
    gap = action.get("schema_gap")
    waived = [e for e in errors if gap is not None and gap_waives(gap, e)]
    said = list(problems) + [f"{e['at']}: {e['message']}" for e in errors if e not in waived]
    if gap is not None:
        if _gap_key(gap) not in KNOWN_GAPS:
            said.append(f"the declared schema gap {_gap_key(gap)} is not one of {KNOWN_GAPS}")
        elif not waived:
            said.append("a schema gap is declared but the schema no longer refuses it: the gap has ended, update the action")
    shown = [f"{e['at']}: {e['message']} (the declared gap)" for e in waived]
    if said:
        return "FAIL", said + shown
    return ("GAP", shown) if waived else ("OK", [])


def row_problems(method: str, row: dict | None) -> list[str]:
    if row is None:
        return [f"{method} has no row in the table"]
    problems = []
    if not row["page_allowed"]:
        problems.append(f"{method} is not allowed to the page")
    if row["scope"] == "manage":
        problems.append(f"{method} is of the manage scope")
    return problems


def source_problems(action: dict, fixture_dir: Path) -> list[str]:
    """For an action taken from a SPINE fixture: the recorded file, its method, and its params except the
    named deltas, each delta really differing."""
    source = action["source"]
    if source["kind"] != "spine":
        return []
    path = fixture_dir / source["file"]
    if not path.is_file():
        return [f"{source['file']} is not in the checkout"]
    problems = []
    if _sha256(path) != source["sha256"]:
        problems.append(f"{source['file']} is not the recorded file")
    fixture = _load(path)
    method, params = action["method"], action["params"]
    if fixture.get("method") != method:
        problems.append(f"the fixture's method is {fixture.get('method')}")
    theirs = fixture.get("params", {})
    deltas = action.get("deltas", {})
    for key in sorted(set(theirs) | set(params)):
        if key not in deltas and theirs.get(key, "<absent>") != params.get(key, "<absent>"):
            problems.append(f"params.{key} differs from the fixture and no delta names it")
    for key in deltas:
        if theirs.get(key, "<absent>") == params.get(key, "<absent>"):
            problems.append(f"delta {key} is named but the value is the fixture's")
    return problems


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
        row = rows["rows"].get(method)
        problems = row_problems(method, row) + source_problems(action, fixture_dir)
        errors = _params_errors(registry, methods_id, method, params) if row is not None else []
        found, said = verdict(action, problems, errors)
        failed += found == "FAIL"
        print(f"{found:4} {name:20} {method:20} " + "; ".join(said))
    answers = build_answers(checkout, head, actions["spine_fixture_dir"], actions)
    for answer in answers["answers"]:
        if answer["method"] != next(a["method"] for a in actions["actions"] if a["action"] == answer["action"]):
            failed += 1
            print(f"FAIL {answer['file']} answers {answer['method']}, not the method of {answer['action']}")
    for path, made in ((ROWS, rows), (ANSWERS_FILE, answers)):
        recorded = _load(path) if path.exists() else None
        if write:
            text = json.dumps(made, indent=2, sort_keys=path is ROWS, ensure_ascii=True) + "\n"
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {path.relative_to(HERE.parent.parent)}")
        elif recorded != made:
            failed += 1
            print(f"FAIL {path.name} differs from this checkout (run with --write after reading the difference)")
        else:
            print(f"OK   {path.name} matches this checkout")
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
