"""The list of packages under the core, and the test that keeps it true.

core/CORE-PACKAGES.toml has one row for every package that may stand under core/recursion_core/. A package is
placed in the core by a line of the pinned product contract (the row of a lane of its section 7, or one line inside
a numbered section) or by a decision cited by role and date. This test is red when:

  1. a package (a folder, with or without __init__.py, or a single module) stands under the core and no row
     names it, or a rule of the [rules] table skips something it may not skip: an ignored folder that holds a
     module, a test folder that holds anything but tests, a module named as an other file;
  2. a row of the sections core, for_owner or leaving names a package that does not stand, a rule entry matches
     nothing under the core, or a leaving row is past its leave_by while the file says that is red;
  3. a row of the section expected names a package that stands, or one the hooks seam no longer names, or the
     hooks seam names a top package that neither stands nor has an expected row, or the seam is not a literal
     tuple of strings;
  4. a row or a cite is malformed, or a cite carries a digest (the digests of the contract's lines are kept
     outside this repository);
  5. a cite is not in the pinned bytes of the product contract. This runs only when CSL_RESEARCH_VAULT names a
     local clone of the research repository AND CSL_CORE_LIST_DIGESTS names the file of digests kept outside
     this repository; it never touches the network. When neither is set, the printed state line says that the
     cites were NOT checked; when one is set without the other, this check is red;
  6. the number of for_owner rows differs from for_owner_max in the file;
  7. the scan scanned nothing: the core folder, the pin file or the hooks file is missing, or no package
     was found, or a row matches more than one entry.
  8. a heading of the private contract, or a run of [sweep] run_words words of it, stands in any text file of this
     repository (the files and folders of [sweep] leave_out and the clone itself are left out). This runs where
     CSL_RESEARCH_VAULT names the clone; where it is unset the test says it did NOT run, on every run. A message
     names the file, the line and a count, and never a word of the contract.

Each check is also run on a small tree this test writes in its own temporary folder, and shown red there.
This test imports nothing of recursion_core.
"""
from __future__ import annotations

import ast
import datetime
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parent.parent.parent

LIST_FILE = "core/CORE-PACKAGES.toml"
CORE_DIR = "core/recursion_core"
HOOKS_FILE = "core/recursion_core/control/hooks.py"
PIN_FILE = "contract/PRODUCT.pin.json"
VAULT_ENV = "CSL_RESEARCH_VAULT"
DIGESTS_ENV = "CSL_CORE_LIST_DIGESTS"

SECTIONS = ("core", "for_owner", "leaving", "expected")
# summary, rules and package are read here; call and dynamic_allow are reserved for the import guard.
TOP_TABLES = {"summary", "rules", "sweep", "package", "call", "dynamic_allow"}
SWEEP_KEYS = ("run_words", "scan", "leave_out")
ROW_KEYS = {"name", "section", "what", "placed_by", "builds_it", "moves_to", "leave_by", "decided_out"}
CITE_KEYS = {"kind", "ref", "contract"}
DECISION_KEYS = {"kind", "ref"}
DIGEST_KEYS = {"name", "field", "kind", "ref", "line_sha256"}
LANE_REF = re.compile(r"[A-Z][0-9]{1,2}")
SECTION_REF = re.compile(r"[0-9]{1,2}(?:\.[0-9]{1,2})*")
DECISION_REF = re.compile(r"(the owner|the founder), ([0-9]{4})-([0-9]{2})-([0-9]{2})")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SHA256 = re.compile(r"[0-9a-f]{64}")
IMPORTABLE_SUFFIXES = (".py", ".pyc", ".pyo", ".pyd", ".so")
TEST_MODULE_FUNCTIONS = ("setUpModule", "tearDownModule", "load_tests")
# A bound on each git or Python child this test starts; it belongs to the test, not to a person's settings.
CHILD_TIMEOUT_SECONDS = 60


def fold(line: str) -> str:
    return " ".join(line.split())


def digest(line: str) -> str:
    return hashlib.sha256(fold(line).encode("utf-8")).hexdigest()


def _is_date(value) -> bool:
    if not isinstance(value, str) or not DATE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------------------------------------------
# The scan. Every function returns a list of (check number, message).


DEFAULT_RULES = {"ignored_folders": ["__pycache__"], "test_folders": [], "other_files": []}
RULE_KEYS = tuple(DEFAULT_RULES)


def rules_of(data):
    """The rules the list itself states for what is not a package (the [rules] table)."""
    table = data.get("rules") if isinstance(data, dict) else None
    rules = {key: [] for key in RULE_KEYS}
    if isinstance(table, dict):
        for key in RULE_KEYS:
            value = table.get(key)
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                rules[key] = list(value)
    else:
        rules["ignored_folders"] = list(DEFAULT_RULES["ignored_folders"])
    return rules


def importable(path: Path) -> bool:
    return path.suffix in IMPORTABLE_SUFFIXES


def _base_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_main_guard(test) -> bool:
    return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")


def _describe(node) -> str:
    if isinstance(node, ast.ClassDef):
        return f"the class {node.name}"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"the function {node.name}"
    return f"a statement ({type(node).__name__})"


def not_a_test(path: Path):
    """Why a module of a test folder is not test code, or None. Read fail closed."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as error:
        return f"cannot be read as Python ({type(error).__name__})"
    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if path.name == "__init__.py":
        return f"holds {_describe(body[0])} at line {body[0].lineno}, and an __init__.py of a test folder holds " \
               "at most a docstring" if body else None
    tests = 0
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] \
            if isinstance(node, ast.AnnAssign) else None
        if targets is not None and all(isinstance(t, ast.Name) for t in targets):
            continue
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            continue
        if isinstance(node, ast.ClassDef) and any(_base_name(b).endswith("TestCase") for b in node.bases):
            tests += 1
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name.startswith("test_") or node.name in TEST_MODULE_FUNCTIONS):
            tests += node.name.startswith("test_")
            continue
        return f"holds {_describe(node)} at line {node.lineno}, which is not test code"
    return None if tests else "defines no test"


def holds_only_tests(folder: Path):
    """What in a test folder is not a test (a sentence each), read fail closed."""
    stray = []
    for child in sorted(folder.rglob("*")):
        relative = child.relative_to(folder).as_posix()
        if "__pycache__" in child.relative_to(folder).parts:
            continue
        if child.is_symlink():
            stray.append(f"{relative}, which is a link")
        elif child.is_dir():
            continue
        elif not (child.suffix == ".py" and (child.name == "__init__.py" or child.name.startswith("test_"))):
            stray.append(f"{relative}, which is not a test module")
        else:
            why = not_a_test(child)
            if why:
                stray.append(f"{relative}, which {why}")
    return stray


def modules_in(folder: Path):
    """The modules (or links) anywhere inside a folder, as paths relative to it."""
    return [child.relative_to(folder).as_posix() for child in sorted(folder.rglob("*"))
            if child.is_symlink() or (child.is_file() and importable(child))]


def scan_core(root: Path, rules=None):
    """The entries directly under core/recursion_core/: (name, kind) pairs, kind 'folder' or 'module'.

    Every entry is classified or red: a package folder or a module (each needs a row), an ignored folder that holds
    no module, a test folder that holds only tests, a file the rules name. Anything else is red.
    """
    rules = rules or {key: list(DEFAULT_RULES[key]) for key in RULE_KEYS}
    problems = []
    core = root / CORE_DIR
    if not core.is_dir():
        return [], [], [(7, f"The scan found no folder at {CORE_DIR}, so it scanned nothing. Point the scan at the "
                            "repository root.")]
    entries = []
    test_folders = []
    matched = {key: set() for key in RULE_KEYS}
    try:
        children = sorted(core.iterdir())
    except OSError as error:
        return [], [], [(7, f"The scan could not read {CORE_DIR} ({error}), so it scanned nothing.")]
    for child in children:
        name = child.name
        where = f"core/recursion_core/{name}"
        try:
            if child.is_symlink():
                problems.append((1, f"{where} is a link, so the scan cannot classify it. Remove it."))
            elif child.is_dir():
                if name in rules["ignored_folders"]:
                    matched["ignored_folders"].add(name)
                    held = modules_in(child)
                    if name == "__pycache__":
                        held = [path for path in held if not path.endswith(".pyc")]
                    if held:
                        problems.append((1, f"{where} is an ignored folder in the [rules] table of {LIST_FILE}, "
                                            f"but holds {held[0]}, a module. A folder is skipped only when it "
                                            "holds no module: give it a row, or take its name out of "
                                            "ignored_folders."))
                    continue
                if name in rules["test_folders"]:
                    matched["test_folders"].add(name)
                    stray = holds_only_tests(child)
                    if stray:
                        problems.append((1, f"{where} is named a test folder in the [rules] table of {LIST_FILE}, "
                                            f"but holds {stray[0]}. Move it out, or give the folder a row and "
                                            "take its name out of test_folders."))
                    else:
                        test_folders.append(name)
                    continue
                entries.append((name, "folder"))
            elif child.is_file() and child.suffix == ".py":
                if name != "__init__.py":
                    entries.append((child.stem, "module"))
            elif child.is_file() and name in rules["other_files"]:
                matched["other_files"].add(name)
                if importable(child):
                    problems.append((1, f"{where} is a module, and the [rules] table of {LIST_FILE} names it under "
                                        "other_files. A module needs a row: give it one, or move it out of the "
                                        "core."))
            else:
                problems.append((1, f"{where} is neither a folder nor a .py module, so the scan cannot classify "
                                    f"it. Remove it, or list it under other_files in the [rules] table of "
                                    f"{LIST_FILE}."))
        except OSError as error:
            problems.append((7, f"The scan could not read {where} ({error}), so it cannot classify it."))
    for key in RULE_KEYS:
        for name in rules[key]:
            if name not in matched[key] and not (key == "ignored_folders" and name == "__pycache__"):
                problems.append((2, f"{LIST_FILE}: rules.{key} names {name}, but nothing of that name stands under "
                                    "the core. Remove it from the rule."))
    if not entries:
        problems.append((7, f"The scan found no package under {CORE_DIR}, so it scanned nothing. Check the "
                            "scan root."))
    names = [name for name, _ in entries]
    for name in sorted(set(names)):
        if names.count(name) > 1:
            problems.append((7, f"{CORE_DIR}/{name} stands twice (a folder and a module). Keep one."))
    return entries, test_folders, problems


def read_registrants(root: Path):
    """The names in REGISTRANTS of the hooks seam, read with ast, literal strings only."""
    path = root / HOOKS_FILE
    if not path.is_file():
        return [], [(7, f"The hooks file {HOOKS_FILE} was not found, so the names the core presumes were not read.")]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    value = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "REGISTRANTS"
                                                for t in node.targets):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "REGISTRANTS":
            value = node.value
    bad = (3, f"REGISTRANTS in {HOOKS_FILE} is not a literal tuple of strings, so the names the core presumes "
              "cannot be read. Write it as a literal tuple of strings, or change this test with it.")
    if value is None:
        return [], [bad]
    if not isinstance(value, (ast.Tuple, ast.List)):
        return [], [bad]
    names = []
    for element in value.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return [], [bad]
        names.append(element.value)
    return names, []


def registrant_tops(names):
    tops = []
    for name in names:
        parts = name.split(".")
        if len(parts) >= 2 and parts[0] == "recursion_core" and parts[1] not in tops:
            tops.append(parts[1])
    return tops


def read_list(root: Path):
    path = root / LIST_FILE
    if not path.is_file():
        return None, [(7, f"{LIST_FILE} is not there. Add it: one row for every package under the core.")]
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return None, [(4, f"{LIST_FILE} is not valid TOML ({error}). Correct the file.")]
    if SHA256.search(text):
        return data, [(4, f"{LIST_FILE} holds a run of 64 hexadecimal digits, which is the form of a digest of a line "
                          f"of the private contract. The digests are kept in the file {DIGESTS_ENV} names. Remove "
                          "it.")]
    return data, []


def read_pin(root: Path):
    path = root / PIN_FILE
    if not path.is_file():
        return None, [(7, f"The pin file {PIN_FILE} was not found, so the cited contract cannot be named.")]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except ValueError:
        return None, [(7, f"The pin file {PIN_FILE} is not valid JSON.")]


def check_cite(row_name: str, cite, where: str):
    """The form of one cite."""
    problems = []
    tag = f"{LIST_FILE}, row {row_name}, {where}"
    if not isinstance(cite, dict):
        return [(4, f"{tag}: a cite is a table with kind and ref. Write it as one.")]
    kind = cite.get("kind")
    ref = cite.get("ref")
    if kind not in ("lane", "section", "decision"):
        return [(4, f"{tag}: kind must be lane, section or decision. Set it.")]
    if not isinstance(ref, str):
        return [(4, f"{tag}: ref must be a string. Set it.")]
    extra = set(cite) - (DECISION_KEYS if kind == "decision" else CITE_KEYS)
    held = sorted(key for key in extra if "sha" in key.lower() or "digest" in key.lower())
    if held:
        problems.append((4, f"{tag}: a cite in this file carries no digest; the digests of the lines of the private "
                            f"contract are kept outside this repository, in the file {DIGESTS_ENV} names. Remove "
                            f"{held}."))
    extra -= set(held)
    if kind == "decision":
        if extra:
            problems.append((4, f"{tag}: a decision cite holds only kind and ref; remove {sorted(extra)}."))
        match = DECISION_REF.fullmatch(ref)
        if not match or not _is_date(f"{match.group(2)}-{match.group(3)}-{match.group(4)}"):
            problems.append((4, f"{tag}: a decision is cited by role and date only, in the form "
                                "'the owner, 2026-11-24'. Rewrite ref."))
        return problems
    if extra:
        problems.append((4, f"{tag}: unknown field {sorted(extra)} in a cite. Remove it."))
    if kind == "lane" and not LANE_REF.fullmatch(ref):
        problems.append((4, f"{tag}: ref {ref!r} is not a lane id of section 7 of the contract (a letter and "
                            "a number, such as F3). Rewrite ref."))
    if kind == "section" and not SECTION_REF.fullmatch(ref):
        problems.append((4, f"{tag}: ref {ref!r} is not a section number (such as 3.3). Rewrite ref."))
    if cite.get("contract") != "pinned":
        problems.append((4, f"{tag}: contract must be 'pinned': only the pinned bytes are checkable. Set it."))
    return problems


def check_form(data):
    """The form of the file: the summary, every row, every cite."""
    problems = []
    summary = data.get("summary")
    if not isinstance(summary, dict) or isinstance(summary.get("for_owner_max"), bool) \
            or not isinstance(summary.get("for_owner_max"), int) or summary["for_owner_max"] < 0:
        problems.append((4, f"{LIST_FILE} has no [summary] with for_owner_max as a whole number. Add it."))
    elif summary.get("leave_by_passed", "red") not in ("red", "print"):
        problems.append((4, f"{LIST_FILE}: [summary] leave_by_passed must be 'red' or 'print'. Set it."))
    unknown = sorted(set(data) - TOP_TABLES)
    if unknown:
        problems.append((4, f"{LIST_FILE}: unknown table {unknown}. The file holds {', '.join(sorted(TOP_TABLES))} "
                            "only. Remove it."))
    if data.get("sweep") is not None:
        problems += sweep_form(data)[3]
    table = data.get("rules")
    if table is not None:
        if not isinstance(table, dict) or set(table) - set(RULE_KEYS):
            problems.append((4, f"{LIST_FILE}: [rules] holds only {', '.join(RULE_KEYS)}. Remove the rest."))
        else:
            for key, value in table.items():
                if not isinstance(value, list) or not all(isinstance(v, str) and v and "/" not in v
                                                          for v in value):
                    problems.append((4, f"{LIST_FILE}: rules.{key} is a list of plain names. Write it as one."))
    rows = data.get("package")
    if not isinstance(rows, list):
        return problems + [(4, f"{LIST_FILE} has no [[package]] rows. Add one row for every package.")]
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row.get("name"):
            problems.append((4, f"{LIST_FILE}: row number {index + 1} has no name. Add one."))
            continue
        name = row["name"]
        tag = f"{LIST_FILE}, row {name}"
        if name in seen:
            problems.append((7, f"{tag}: two rows name {name}. Keep one."))
        seen.add(name)
        extra = set(row) - ROW_KEYS
        if extra:
            problems.append((4, f"{tag}: unknown field {sorted(extra)}. Remove it."))
        section = row.get("section")
        if section == "not_product":
            problems.append((4, f"{tag}: the section not_product is not used. A folder under the core that is no "
                                "product package is named in the [rules] table (test_folders), where the test "
                                "checks that it holds only tests; any other folder needs a placing row."))
            continue
        if section not in SECTIONS:
            problems.append((4, f"{tag}: section must be one of {', '.join(SECTIONS)}. Set it."))
            continue
        what = row.get("what")
        if not isinstance(what, str) or not what.strip() or "\n" in what:
            problems.append((4, f"{tag}: what is one plain sentence saying what the package is. Write it."))
        for key in ("placed_by", "builds_it"):
            value = row.get(key)
            if value is not None and not isinstance(value, list):
                problems.append((4, f"{tag}: {key} is a list of cites. Write it as one."))
        placed = row.get("placed_by") if isinstance(row.get("placed_by"), list) else []
        builds = row.get("builds_it") if isinstance(row.get("builds_it"), list) else []
        if section == "core" and not placed:
            problems.append((4, f"{tag}: a core row needs a cite in placed_by (a lane or a section of the pinned "
                                "contract, or a decision by role and date). Add one, or move the row to "
                                "for_owner."))
        if section == "for_owner":
            if placed:
                problems.append((4, f"{tag}: a for_owner row has no placing line; remove placed_by, or move the "
                                    "row to core."))
            if not builds:
                problems.append((4, f"{tag}: a for_owner row needs builds_it, the lane that builds the package."))
            if any(isinstance(cite, dict) and cite.get("kind") == "decision" for cite in builds):
                problems.append((4, f"{tag}: builds_it names the lane or section that builds the package, and a "
                                    "decision names neither. Cite the lane or the section; the role and date of "
                                    "the decision that brought the package here go in a comment beside the row."))
        if section == "leaving":
            if not placed:
                problems.append((4, f"{tag}: a leaving row needs the cite of the decision that moves it out."))
            if not isinstance(row.get("moves_to"), str) or not row.get("moves_to"):
                problems.append((4, f"{tag}: a leaving row needs moves_to. Add it."))
            if not _is_date(row.get("leave_by")):
                problems.append((4, f"{tag}: a leaving row needs leave_by as a date (YYYY-MM-DD). Add it."))
        if section != "leaving" and ("moves_to" in row or "leave_by" in row):
            problems.append((4, f"{tag}: moves_to and leave_by belong to the section leaving only. Remove them."))
        if "decided_out" in row:
            if section != "expected" or row["decided_out"] is not True:
                problems.append((4, f"{tag}: decided_out is true on an expected row only. Remove it."))
            elif not any(isinstance(c, dict) and c.get("kind") == "decision" for c in placed):
                problems.append((4, f"{tag}: decided_out needs the cite of the decision in placed_by, by role and "
                                    "date. Add it, or remove decided_out."))
        for where, cites in (("placed_by", placed), ("builds_it", builds)):
            for number, cite in enumerate(cites, 1):
                problems.extend(check_cite(name, cite, f"{where} {number}"))
    return problems


def passed_rows(data, today):
    rows = [r for r in data.get("package", []) if isinstance(r, dict) and isinstance(r.get("name"), str)]
    return [(r["name"], r["leave_by"]) for r in rows if r.get("section") == "leaving" and _is_date(r.get("leave_by"))
            and datetime.date.fromisoformat(r["leave_by"]) < today]


def check_rows(entries, data, tops, today):
    """Checks 1, 2, 3 and 6 of the rows against what stands."""
    problems = []
    rows = [r for r in data.get("package", []) if isinstance(r, dict) and isinstance(r.get("name"), str)]
    standing = {name for name, _ in entries}
    by_section = {s: [r["name"] for r in rows if r.get("section") == s] for s in SECTIONS}
    named = {r["name"] for r in rows}
    for name in sorted(standing - named):
        problems.append((1, f"core/recursion_core/{name} stands under the core and {LIST_FILE} has no row for "
                            "it. Add a row that cites the lane or section of the pinned contract that places it "
                            "in the core, or put it in the section for the owner, or move it out of the core."))
    for section in ("core", "for_owner", "leaving"):
        for name in by_section[section]:
            if name not in standing:
                problems.append((2, f"{LIST_FILE} has a row for {name}, but no such package stands under the "
                                    "core. Remove the row, or move it to the section expected if the core still "
                                    "names it."))
    for name in by_section["expected"]:
        if name in standing:
            problems.append((3, f"{LIST_FILE}: the row for {name} is in the section expected, but the package "
                                f"stands under the core. Give the row its section (core, for_owner or "
                                "leaving)."))
        elif name not in tops:
            problems.append((3, f"{LIST_FILE}: the row for {name} is in the section expected, but REGISTRANTS "
                                f"in {HOOKS_FILE} no longer names it. Remove the row."))
    for top in tops:
        if top not in standing and top not in by_section["expected"]:
            problems.append((3, f"REGISTRANTS in {HOOKS_FILE} names recursion_core.{top}, which does not stand "
                                f"and has no row in the section expected of {LIST_FILE}. Add that row, or "
                                "remove the name from REGISTRANTS."))
    waiting = len(by_section["for_owner"])
    summary = data.get("summary")
    maximum = summary.get("for_owner_max") if isinstance(summary, dict) else None
    if isinstance(maximum, int) and not isinstance(maximum, bool):
        if waiting > maximum:
            problems.append((6, f"{waiting} packages wait for the owner's placing, and the file says {maximum}. "
                                f"Set for_owner_max to {waiting} only if a new waiting package is meant; "
                                "otherwise place the package with a cite."))
        elif waiting < maximum:
            problems.append((6, f"{waiting} packages wait for the owner's placing, and the file says {maximum}. "
                                f"Lower for_owner_max to {waiting}."))
    if isinstance(summary, dict) and summary.get("leave_by_passed", "red") == "red":
        for name, date in passed_rows(data, today):
            problems.append((2, f"{LIST_FILE}: the row for {name} was to leave the core by {date}. Move the "
                                "package out and remove the row, or give the row a later leave_by."))
    return problems


def cite_inputs(environ):
    """The clone and the file of digests for check 5: (vault, digests, note, problems)."""
    vault, digests = environ.get(VAULT_ENV) or None, environ.get(DIGESTS_ENV) or None
    decisions = "a decision cite is checked for its form only and no line is read for it"
    swept = ("the public tree is swept for words of the contract (check 8)" if vault else
             f"the public tree was NOT swept for words of the contract (check 8), which needs {VAULT_ENV}")
    if vault and digests:
        return vault, digests, (f"the lane and section cites are read against the pinned contract in the clone "
                                f"{VAULT_ENV} names, with the digests of the file {DIGESTS_ENV} names (check 5); "
                                f"{decisions}; {swept}"), []
    if not vault and not digests:
        return None, None, (f"the cites were NOT checked against the pinned contract, only their form: {VAULT_ENV} "
                            f"and {DIGESTS_ENV} are unset, so no cited line was read; {decisions}; {swept}"), []
    missing = DIGESTS_ENV if vault else VAULT_ENV
    given = VAULT_ENV if vault else DIGESTS_ENV
    return None, None, (f"the cites were NOT checked against the pinned contract: {given} is set and {missing} is "
                        f"unset, so check 5 is red; {decisions}; {swept}"), [
        (5, f"{given} is set and {missing} is unset, so the cites cannot be read against the pinned contract. Set "
            f"both ({VAULT_ENV}: a local clone of the research repository; {DIGESTS_ENV}: the file of digests kept "
            "outside this repository), or neither.")]


def by_decision_alone(row):
    """True when every cite that places the row (placed_by) is a decision: no line of the contract is read for it.
    A lane under builds_it says who builds the package, not that it is placed, so it does not count here."""
    cites = row.get("placed_by") if isinstance(row.get("placed_by"), list) else []
    return bool(cites) and all(isinstance(c, dict) and c.get("kind") == "decision" for c in cites)


def state_line(entries, data, test_folders=(), today=None, note=None):
    rows = [r for r in data.get("package", []) if isinstance(r, dict)]
    count = lambda s: [r.get("name") for r in rows if r.get("section") == s]
    decided = [r.get("name") for r in rows if r.get("section") == "core" and by_decision_alone(r)]
    waiting = count("for_owner")
    presumed = count("expected")
    out = [r.get("name") for r in rows if r.get("section") == "expected" and r.get("decided_out") is True]
    line = (f"Under the core: {len(entries)} packages; placed {len(count('core'))} ({len(decided)} by a decision "
            f"alone: {', '.join(decided) or 'none'}); waiting for the owner "
            f"{len(waiting)} ({', '.join(waiting) or 'none'}); leaving {len(count('leaving'))}; presumed by the "
            f"hooks {len(presumed)}")
    if out:
        line += f" (decided to leave: {', '.join(out)})"
    passed = passed_rows(data, today or datetime.date.today())
    if passed:
        line += "; past its leave_by: " + ", ".join(f"{name} ({date})" for name, date in passed)
    if test_folders:
        line += f"; test folders by rule: {', '.join(test_folders)}"
    if note:
        line += f"; {note}"
    return line + "."


def analyse(root: Path, today=None):
    """The offline checks on one tree. Returns (problems, state line or None)."""
    today = today or datetime.date.today()
    problems = []
    data, found = read_list(root)
    entries, test_folders, scanned = scan_core(root, rules_of(data) if data is not None else None)
    problems += scanned
    registrants, found_hooks = read_registrants(root)
    problems += found_hooks
    _, found_pin = read_pin(root)
    problems += found_pin
    problems += found
    if data is None:
        return problems, None
    problems += check_form(data)
    for name in test_folders:
        if name in {r.get("name") for r in data.get("package", []) if isinstance(r, dict)}:
            problems.append((7, f"{LIST_FILE}: {name} is both a row and a test folder of the [rules] table. "
                                "Keep one."))
    problems += check_rows(entries, data, registrant_tops(registrants), today)
    if isinstance(data.get("sweep"), dict):
        _, scan, leave_out, _ = sweep_form(data)
        for key, entries_of in (("scan", scan), ("leave_out", leave_out)):
            for left in entries_of:
                if not (root / left).exists():
                    problems.append((2, f"{LIST_FILE}: sweep.{key} names {left}, but nothing of that name stands in "
                                        "the tree. Remove it from the table."))
                elif not holds_a_swept_file(root, left):
                    problems.append((2, f"{LIST_FILE}: sweep.{key} names {left}, but no file the sweep reads stands "
                                        "there (an empty folder, a link to a folder, or a folder the sweep always "
                                        f"skips: {', '.join(SWEEP_SKIPPED_FOLDERS)}). Name a file, or a folder that "
                                        "holds one."))
    return problems, state_line(entries, data, test_folders, today)


# ---------------------------------------------------------------------------------------------------------------
# The vault check: the cites against the pinned bytes, with the digests of the file kept outside this repository.


def pinned_text(vault: str, pin: dict):
    blob = subprocess.run(["git", "-C", vault, "show", f"{pin['commit']}:{pin['path']}"], check=True,
                          capture_output=True, timeout=CHILD_TIMEOUT_SECONDS).stdout
    return blob, hashlib.sha256(blob).hexdigest()


def read_digests(path: Path):
    if not path.is_file():
        return None, [(5, f"{DIGESTS_ENV} names no file. Point it at the file of digests kept outside this "
                          "repository.")]
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")), []
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        return None, [(5, f"The file {DIGESTS_ENV} names is not valid TOML ({error}). Correct it.")]


def digest_table(companion):
    """(name, field, kind, ref) to line_sha256, from the file of digests, and its problems."""
    rows = companion.get("digest") if isinstance(companion, dict) else None
    if not isinstance(rows, list):
        return {}, [(5, f"The file of digests has no [[digest]] rows. Write one for every lane or section cite of "
                        f"{LIST_FILE}.")]
    table, problems = {}, []
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != DIGEST_KEYS or not all(isinstance(row[k], str) for k in row) \
                or not SHA256.fullmatch(row["line_sha256"]):
            problems.append((5, f"The file of digests, row {number}: a row holds name, field, kind, ref and "
                                "line_sha256 (64 lower-case hex digits). Write it so."))
            continue
        key = (row["name"], row["field"], row["kind"], row["ref"])
        if key in table:
            problems.append((5, f"The file of digests has two rows for {key[0]}, {key[1]}, {key[3]}. Keep one."))
            continue
        table[key] = row["line_sha256"]
    return table, problems


def cites_of(data):
    """The lane and section cites of the list: (name, field, kind, ref)."""
    found = []
    for row in data.get("package", []):
        if not isinstance(row, dict):
            continue
        for field in ("placed_by", "builds_it"):
            for cite in row.get(field) or []:
                if isinstance(cite, dict) and cite.get("kind") in ("lane", "section"):
                    found.append((row.get("name"), field, cite["kind"], cite.get("ref")))
    return found


def lane_rows(lines, ref):
    """The rows of section 7 that begin with the lane id."""
    start = next((i for i, l in enumerate(lines) if re.match(r"## 7\.\s", l)), None)
    stop = next((i for i, l in enumerate(lines) if re.match(r"## 8\.\s", l)), None)
    if start is None or stop is None:
        return []
    pattern = re.compile(r"\| \*\*" + re.escape(ref) + r"[ *]")
    return [l for l in lines[start:stop] if pattern.match(l)]


def section_body(lines, ref):
    """The lines inside the one section of that number, its heading left out: (first, stop), or None and the
    number of headings found."""
    pattern = re.compile(r"(#{2,3}) " + re.escape(ref) + r"\.?\s")
    heads = [i for i, l in enumerate(lines) if pattern.match(l)]
    if len(heads) != 1:
        return None, len(heads)
    head = heads[0]
    level = len(pattern.match(lines[head]).group(1))
    ends = re.compile(r"#{1,%d} " % level)
    stop = next((j for j in range(head + 1, len(lines)) if ends.match(lines[j])), len(lines))
    return (head + 1, stop), 1


def check_vault(data, pin, vault: str, companion, verify_digests: bool = True):
    blob, sha = pinned_text(vault, pin)
    if sha != pin["sha256"]:
        return [(5, f"The pinned bytes read from {vault} do not match the SHA-256 of {PIN_FILE}. Use a clone that "
                    "holds the pinned commit.")]
    lines = blob.decode("utf-8").split("\n")
    table, problems = digest_table(companion)
    cites = cites_of(data)
    for key in sorted(set(table) - set(cites)):
        problems.append((5, f"The file of digests has a row for {key[0]}, {key[1]}, {key[3]}, and {LIST_FILE} has no "
                            "such cite. Remove the row."))
    changed = ("and that line of the pinned contract has changed since the row was written (or is not there). "
               "Read the line again and write the row again.")
    for name, field, kind, ref in cites:
        tag = f"The row for {name} cites {ref} in {field}"
        want = table.get((name, field, kind, ref))
        if want is None:
            problems.append((5, f"{tag}, and the file of digests has no digest for that cite. Read the placing line "
                                "in the pinned contract and write its digest there."))
            continue
        if kind == "lane":
            found = lane_rows(lines, ref)
            if len(found) != 1:
                problems.append((5, f"{tag}, {changed}" if not found else
                                 f"{tag}, and section 7 of the pinned contract has {len(found)} rows for that lane, "
                                 "not one. Read the section again and write the row again."))
            elif verify_digests and digest(found[0]) != want:
                problems.append((5, f"{tag}, {changed}"))
            continue
        body, heads = section_body(lines, ref)
        if body is None:
            problems.append((5, f"{tag}, {changed}" if not heads else
                             f"{tag}, and the pinned contract has {heads} headings of that number, not one. Read "
                             "the section again and write the row again."))
            continue
        if not verify_digests:
            continue
        found = [l for l in lines[body[0]:body[1]] if digest(l) == want]
        if not found:
            problems.append((5, f"{tag}, {changed}"))
        elif len(found) > 1:
            problems.append((5, f"{tag}, and {len(found)} lines inside that section have the digest given for it, "
                                "so the cite does not name one line. Cite a line that stands once."))
    return problems


# ---------------------------------------------------------------------------------------------------------------
# Stand-in trees for the tests of the test.

HOOKS = "REGISTRANTS = (\n    'recursion_core.alpha.hooks',\n    'recursion_core.beta.hooks',\n    'recursion_core.later.hooks',\n)\n"
F3_ROW = "| **F3 Quillfern lane** | In the quillfern core: one |"
D1_ROW = "| **D1 Zorbwick lane** | one zorbwick |"
PLACING = "- The quillfern core drives the snarfles."
PRODUCT = "\n".join([
    "# Quillfern title", "", "## 3. Quillfern part", "", "### 3.3 Quillfern section", "", PLACING,
    "- Frimble knobs turn.", "",
    "### 3.4 Quillfern other", "", "- The quillfern core drives the snarfles too.", "",
    "## 7. Quillfern lanes", "", "| Lane | Quillfern what |", "|---|---|", F3_ROW, D1_ROW, "", "## 8. Quillfern next", ""])
TEST_BODY = "import unittest\n\n\nclass Case(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n"
DECISION = '{ kind = "decision", ref = "the owner, 2026-11-24" }'


def cite_text(kind, ref, extra=""):
    return f'{{ kind = "{kind}", ref = "{ref}", contract = "pinned"{extra} }}'


def list_text(max_count=1, extra="", alpha_cite=None, beta_row=None, later_row=None):
    alpha_cite = alpha_cite or cite_text("lane", "F3")
    beta_row = beta_row if beta_row is not None else f'builds_it = [{cite_text("lane", "D1")}]'
    later_row = later_row or ""
    return (f"[summary]\nfor_owner_max = {max_count}\n\n"
            f'[[package]]\nname = "alpha"\nsection = "core"\nwhat = "One."\nplaced_by = [{alpha_cite}]\n\n'
            f'[[package]]\nname = "beta"\nsection = "for_owner"\nwhat = "Two."\n{beta_row}\n\n'
            f'[[package]]\nname = "later"\nsection = "expected"\nwhat = "Three."\n{later_row}\n\n'
            f'[[package]]\nname = "control"\nsection = "core"\nwhat = "Holds the seam."\nplaced_by = [{DECISION}]\n'
            + extra)


def digests_text(rows):
    return "".join(f'[[digest]]\nname = "{n}"\nfield = "{f}"\nkind = "{k}"\nref = "{r}"\nline_sha256 = "{s}"\n\n'
                   for n, f, k, r, s in rows)


STAND_IN_DIGESTS = [("alpha", "placed_by", "lane", "F3", digest(F3_ROW)),
                    ("beta", "builds_it", "lane", "D1", digest(D1_ROW))]


def make_tree(base: Path, list_body=None, hooks=HOOKS, folders=("alpha", "beta", "control"), modules=(), pin=True,
              with_core=True):
    if with_core:
        core = base / CORE_DIR
        core.mkdir(parents=True)
        (core / "__init__.py").write_text("", encoding="utf-8")
        for name in folders:
            (core / name).mkdir()
            (core / name / "__init__.py").write_text("", encoding="utf-8")
        for name in modules:
            (core / f"{name}.py").write_text("", encoding="utf-8")
        if hooks is not None:
            (base / HOOKS_FILE).parent.mkdir(parents=True, exist_ok=True)
            (base / HOOKS_FILE).write_text(hooks, encoding="utf-8")
    if pin:
        (base / PIN_FILE).parent.mkdir(parents=True)
        (base / PIN_FILE).write_text(json.dumps({"commit": "0" * 40, "path": "contracts/PRODUCT.md",
                                                 "sha256": "0" * 64}), encoding="utf-8")
    if list_body is not None:
        (base / LIST_FILE).parent.mkdir(parents=True, exist_ok=True)
        (base / LIST_FILE).write_text(list_body, encoding="utf-8")
    return base


def run_git(args, cwd):
    subprocess.run(["git", "-c", "user.name=stand-in", "-c", "user.email=stand-in@example.invalid",
                    "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                   timeout=CHILD_TIMEOUT_SECONDS)


def make_vault(base: Path, product: str):
    base.mkdir()
    run_git(["init", "-q"], base)
    (base / "contracts").mkdir()
    (base / "contracts" / "PRODUCT.md").write_bytes(product.encode("utf-8"))
    run_git(["add", "."], base)
    run_git(["commit", "-q", "-m", "stand-in"], base)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=base, check=True, capture_output=True,
                            text=True, timeout=CHILD_TIMEOUT_SECONDS).stdout.strip()
    return {"commit": commit, "path": "contracts/PRODUCT.md",
            "sha256": hashlib.sha256(product.encode("utf-8")).hexdigest()}


# ---------------------------------------------------------------------------------------------------------------
# Check 8: no heading and no run of words of the private contract stands in the public tree. A message gives the
# file, the line and a count, never a word of the contract.

WORD = re.compile(r"[a-z0-9]+")
URL = re.compile(r"https?://\S+")
SWEEP_ROOT = "."
HEADING_LINE = re.compile(r"#{1,6}\s+\S")
# The shipped default of [sweep] run_words in the list file: a run of this many words of the contract is red.
SWEEP_RUN_WORDS = 6
SWEEP_SKIPPED_FOLDERS = (".git", "__pycache__", "node_modules")


def public_text_files(root: Path, skip=()):
    """(path, text) of every file under root that reads as UTF-8; the .git folder, caches and skip are left out."""
    skip = [Path(s).resolve() for s in skip]
    for folder, names, files in os.walk(root):
        names[:] = sorted(n for n in names if n not in SWEEP_SKIPPED_FOLDERS
                          and not any(Path(folder, n).resolve() == s for s in skip))
        for file in sorted(files):
            path = Path(folder, file)
            try:
                yield path, path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue


def words_of(text: str):
    """(word, line number) for every token of a text, links left out; a token with a digit is None, which breaks
    a run, so that cite fields and numbers never make one."""
    return [(w if w.isalpha() else None, number) for number, line in enumerate(text.splitlines(), 1)
            for w in WORD.findall(URL.sub(" ", line).lower())]


def runs_of(words, run_words: int):
    """(run, line of its first word) for every window of run_words words, none broken."""
    for i in range(len(words) - run_words + 1):
        run = tuple(w for w, _ in words[i:i + run_words])
        if None not in run:
            yield run, words[i][1]


def contract_marks(text: str, run_words: int = SWEEP_RUN_WORDS):
    """The headings (folded) and the runs of run_words words of the contract, each with its line number."""
    headings, runs = {}, {}
    for number, line in enumerate(text.splitlines(), 1):
        if HEADING_LINE.match(line):
            headings.setdefault(fold(line), number)
    for run, number in runs_of(words_of(text), run_words):
        runs.setdefault(run, number)
    return headings, runs


SWEEP_FORM = ("[sweep] holds run_words (a whole number, 2 or more), scan and leave_out (lists of paths inside the "
              "repository, written from its root) only.")


def kind_of(value) -> str:
    """The kind of a value read from the list, in the words a message uses."""
    if isinstance(value, bool):
        return "a truth value"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, (list, tuple)):
        return "a list"
    if isinstance(value, dict):
        return "a table"
    if isinstance(value, (datetime.date, datetime.time)):
        return "a date or a time"
    return f"a value of the kind {type(value).__name__}"


def within(where: str, entry: str) -> bool:
    """Whether a path stands at or under an entry; both are in their normal form, and "." is the whole tree."""
    return entry == SWEEP_ROOT or where == entry or where.startswith(entry + "/")


def sweep_entries(key: str, value):
    """(entries, problems) of scan or leave_out: each entry in its one normal form, the POSIX normal form of the path
    (./a, a/, a//b and a/b/.. all name a), and a check 4 problem for each value the sweep cannot use. Entries are
    checked and compared only in this form, so no spelling of an entry makes the sweep read less than it names.
    entries is None when the value is not a list at all."""
    def bad(why):
        return (4, f"{LIST_FILE}: [sweep] {key} {why}. {SWEEP_FORM} Correct it.")
    if not isinstance(value, (list, tuple)):
        return None, [bad(f"is {kind_of(value)}, and it must be a list of paths, such as [\"core/tests\"]")]
    entries, first, problems = [], {}, []
    for entry in value:
        if not isinstance(entry, str):
            problems.append(bad(f"holds {kind_of(entry)}, and every entry must be a path written as a string"))
        elif not entry.strip():
            problems.append(bad('holds an empty entry; name a file or a folder, or "." for the whole tree'))
        elif entry.startswith("/"):
            problems.append(bad(f"holds {entry}, an absolute path; write it from the repository root"))
        elif posixpath.normpath(entry) == ".." or posixpath.normpath(entry).startswith("../"):
            problems.append(bad(f"holds {entry}, which leaves the repository; name a file or a folder inside it"))
        elif posixpath.normpath(entry) in first:
            normal = posixpath.normpath(entry)
            problems.append(bad(f"names {normal} twice (as {first[normal]} and as {entry}); keep one"))
        else:
            first[posixpath.normpath(entry)] = entry
            entries.append(posixpath.normpath(entry))
    if key == "scan" and not value:
        problems.append(bad('is empty, so the sweep would read no file; name the files or folders it reads, or "." '
                            "for the whole tree"))
    return entries, problems


def sweep_form(data):
    """(run_words, scan, leave_out, problems) of the [sweep] table of the list: every entry in its normal form, the
    defaults (the whole tree) for a value that is missing or of the wrong kind, and a check 4 problem for every value
    the sweep cannot use, naming the key and the kind it must be. A wrong value never raises."""
    table = data.get("sweep") if isinstance(data, dict) else None
    if table is None:
        return SWEEP_RUN_WORDS, [SWEEP_ROOT], [], []
    if not isinstance(table, dict):
        return SWEEP_RUN_WORDS, [SWEEP_ROOT], [], [
            (4, f"{LIST_FILE}: [sweep] is {kind_of(table)}, and it must be a table. {SWEEP_FORM} Correct it.")]
    problems = [(4, f"{LIST_FILE}: [sweep] holds {key}, which is no key of it. {SWEEP_FORM} Remove it.")
                for key in sorted(set(table) - set(SWEEP_KEYS))]
    words = table.get("run_words", SWEEP_RUN_WORDS)
    if isinstance(words, bool) or not isinstance(words, int) or words < 2:
        what = str(words) if isinstance(words, int) and not isinstance(words, bool) else kind_of(words)
        problems.append((4, f"{LIST_FILE}: [sweep] run_words is {what}, and it must be a whole number, 2 or more. "
                            f"{SWEEP_FORM} Correct it."))
        words = SWEEP_RUN_WORDS
    scan, found = sweep_entries("scan", table.get("scan", [SWEEP_ROOT]))
    problems += found
    leave_out, found = sweep_entries("leave_out", table.get("leave_out", []))
    problems += found
    scan = [SWEEP_ROOT] if scan is None else scan
    leave_out = [] if leave_out is None else leave_out
    for entry in leave_out:
        for whole in scan:
            if within(whole, entry):
                problems.append((4, f"{LIST_FILE}: [sweep] leave_out names {entry}, which leaves out the whole of "
                                    f"the scan entry {whole}, so the sweep would read nothing of it. {SWEEP_FORM} "
                                    "Take one of the two out."))
    return words, scan, leave_out, problems


def sweep_settings(data):
    """(run_words, scan, leave_out) from the [sweep] table of the list, each entry in its normal form, with the
    defaults (the whole tree) for a value that is missing or of the wrong kind; check 4 names a wrong value."""
    return sweep_form(data)[:3]


def holds_a_swept_file(root: Path, entry: str) -> bool:
    """Whether the sweep reads at least one file at or under an entry in its normal form. The sweep never enters
    the folders it always skips or a link to a folder, so an entry there, or an empty folder, holds none."""
    parts = [] if entry == SWEEP_ROOT else entry.split("/")
    if any(part in SWEEP_SKIPPED_FOLDERS for part in parts) \
            or any(root.joinpath(*parts[:depth]).is_symlink() for depth in range(1, len(parts))):
        return False
    target = root.joinpath(*parts)
    if target.is_file():
        return True
    if target.is_symlink() or not target.is_dir():
        return False
    for _, names, files in os.walk(target):
        names[:] = [name for name in names if name not in SWEEP_SKIPPED_FOLDERS]
        if files:
            return True
    return False


def sweep_public_tree(root: Path, contract: str, skip=(), run_words: int = SWEEP_RUN_WORDS, scan=(SWEEP_ROOT,),
                      leave_out=()):
    """Check 8 on one tree. The contract is given as text; no message holds a word of it. scan names the files or
    folders (paths from the root; "." is the whole tree) that are read, leave_out those of them that are not. Each
    entry is brought to its normal form first; an entry the sweep cannot use is a check 4 problem, and then nothing
    is read."""
    run_words, scan, leave_out, refused = sweep_form({"sweep": {"run_words": run_words, "scan": scan,
                                                               "leave_out": leave_out}})
    if refused:
        return refused
    headings, runs = contract_marks(contract, run_words)
    pattern = re.compile("|".join(r"(?<![#\w])" + re.escape(h) + r"(?!\w)"
                                  for h in sorted(headings, key=len, reverse=True))) if headings else None
    problems = []
    for path, text in public_text_files(root, skip):
        where = path.relative_to(root).as_posix()
        if not any(within(where, entry) for entry in scan) or any(within(where, entry) for entry in leave_out):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            found = pattern.search(fold(line)) if pattern else None
            if found:
                problems.append((8, f"{where} line {number} holds a heading of the private contract (line "
                                    f"{headings[found.group(0)]} of the pinned bytes). Replace it with invented "
                                    "words, and keep the contract's headings out of this repository."))
        hits = [(number, runs[run]) for run, number in runs_of(words_of(text), run_words) if run in runs]
        if hits:
            problems.append((8, f"{where} line {hits[0][0]} holds a run of {run_words} words of the private "
                                f"contract (line {hits[0][1]} of the pinned bytes); {len(hits)} such runs in the "
                                "file. Write it in other words: this repository carries no line of that contract."))
    return problems


# ---------------------------------------------------------------------------------------------------------------
# The real tree.

_REAL = {}


def real():
    if not _REAL:
        problems, line = analyse(REPO)
        _REAL["problems"], _REAL["line"] = problems, line
        data, _ = read_list(REPO)
        _REAL["data"] = data
    return _REAL


def messages(code, problems):
    return [text for number, text in problems if number == code]


class RealTreeTests(unittest.TestCase):
    def test_the_state_of_the_core_is_printed(self):
        line = real()["line"]
        self.assertIsNotNone(line, "the list could not be read, so there is no state to print")
        rows = real()["data"]
        entries, folders, _ = scan_core(REPO, rules_of(rows))
        # On a line of its own, so that it stands apart from the dots of a plain run.
        print("\n" + state_line(entries, rows, folders, note=cite_inputs(os.environ)[2]), file=sys.stderr)

    def test_1_every_package_under_the_core_has_a_row(self):
        self.assertEqual(messages(1, real()["problems"]), [])

    def test_2_every_row_has_a_package(self):
        self.assertEqual(messages(2, real()["problems"]), [])

    def test_3_the_expected_rows_and_the_hooks_seam_agree(self):
        self.assertEqual(messages(3, real()["problems"]), [])

    def test_4_every_row_and_cite_is_well_formed(self):
        self.assertEqual(messages(4, real()["problems"]), [])

    def test_6_the_owner_section_equals_its_count(self):
        self.assertEqual(messages(6, real()["problems"]), [])

    def test_7_the_scan_scanned_something(self):
        self.assertEqual(messages(7, real()["problems"]), [])
        self.assertTrue((REPO / CORE_DIR).is_dir())
        entries, _, _ = scan_core(REPO, rules_of(real()["data"]))
        self.assertGreater(len(entries), 0)

    def test_5_every_cite_is_in_the_pinned_bytes(self):
        vault, digests, note, problems = cite_inputs(os.environ)
        if not problems and vault is None:
            self.skipTest(note)
        self.assertEqual(problems, [])
        pin = json.loads((REPO / PIN_FILE).read_text(encoding="utf-8"))
        data = real()["data"]
        self.assertIsNotNone(data)
        companion, found = read_digests(Path(digests))
        self.assertEqual(found, [])
        self.assertEqual(check_vault(data, pin, vault, companion), [])


    def test_8_no_heading_and_no_run_of_words_of_the_private_contract_stands_in_the_public_tree(self):
        vault = os.environ.get(VAULT_ENV) or None
        if vault is None:
            print(f"\nCheck 8 did NOT run: {VAULT_ENV} is unset, so the public tree was not swept for words of the "
                  "private contract.", file=sys.stderr)
            self.skipTest(f"check 8 did NOT run: {VAULT_ENV} is unset, so the public tree was not swept for words "
                          "of the private contract")
        pin = json.loads((REPO / PIN_FILE).read_text(encoding="utf-8"))
        blob, sha = pinned_text(vault, pin)
        self.assertEqual(sha, pin["sha256"], f"The pinned bytes read from {vault} do not match the SHA-256 of "
                                              f"{PIN_FILE}. Use a clone that holds the pinned commit.")
        run_words, scan, leave_out = sweep_settings(real()["data"])
        problems = sweep_public_tree(REPO, blob.decode("utf-8"), skip=[vault], run_words=run_words, scan=scan,
                                     leave_out=leave_out)
        self.assertEqual(problems, [])


class TestsOfTheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.n = 0

    def tree(self, **kwargs):
        self.n += 1
        return make_tree(self.base / f"t{self.n}", **kwargs)

    def red(self, code, tree, needle, **kwargs):
        problems, _ = analyse(tree, **kwargs)
        found = messages(code, problems)
        self.assertTrue(any(needle in text for text in found), f"check {code} not red with {needle!r}: {problems}")

    def test_the_stand_in_tree_is_green(self):
        problems, line = analyse(self.tree(list_body=list_text()))
        self.assertEqual(problems, [])
        self.assertEqual(line, "Under the core: 3 packages; placed 2 (1 by a decision alone: control); waiting for "
                               "the owner 1 (beta); leaving 0; presumed by the hooks 1.")

    def test_a_decision_alone_places_a_core_row(self):
        # The stand-in row control is placed by a decision cited by role and date, and nothing else.
        tree = self.tree(list_body=list_text(alpha_cite=DECISION))
        self.assertEqual(analyse(tree)[0], [])

    def test_pycache_is_ignored_by_the_default_rule(self):
        tree = self.tree(list_body=list_text())
        (tree / CORE_DIR / "__pycache__").mkdir()
        (tree / CORE_DIR / "__pycache__" / "x.pyc").write_bytes(b"")
        self.assertEqual(analyse(tree)[0], [])

    RULES = '\n[rules]\nignored_folders = ["__pycache__"]\ntest_folders = ["tests"]\nother_files = []\n'

    def with_tests_folder(self, files, rules=RULES, **kwargs):
        tree = self.tree(list_body=list_text(extra=rules), **kwargs)
        folder = tree / CORE_DIR / "tests"
        folder.mkdir()
        for name, body in (files.items() if isinstance(files, dict) else ((f, None) for f in files)):
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if body is None:
                body = TEST_BODY if Path(name).name.startswith("test_") else ""
            target.write_text(body, encoding="utf-8")
        return tree

    def test_a_test_folder_the_rules_name_is_classified_and_counted_apart(self):
        tree = self.with_tests_folder(["__init__.py", "test_a.py", "sub/test_b.py"])
        problems, line = analyse(tree)
        self.assertEqual(problems, [])
        self.assertTrue(line.endswith("; test folders by rule: tests."), line)

    def test_1_a_test_folder_the_rules_do_not_name_is_red(self):
        tree = self.with_tests_folder(["__init__.py", "test_a.py"], rules="")
        self.red(1, tree, "core/recursion_core/tests stands under the core and core/CORE-PACKAGES.toml has no row")

    def test_1_a_test_folder_holding_a_module_that_is_not_a_test_is_red(self):
        tree = self.with_tests_folder(["__init__.py", "test_a.py", "helper.py"])
        self.red(1, tree, "holds helper.py, which is not a test module")
        tree = self.with_tests_folder(["__init__.py", "data.json"])
        self.red(1, tree, "holds data.json, which is not a test module")

    ENGINE = "class Engine:\n    def run(self):\n        return 1\n"

    def test_1_product_code_in_a_tests_folder_is_red(self):
        # With the shipped rules (no test folder) the folder needs a row; named a test folder, its code is read.
        files = {"__init__.py": "", "test_engine.py": self.ENGINE}
        self.red(1, self.with_tests_folder(files, rules=""), "core/recursion_core/tests stands under the core")
        self.red(1, self.with_tests_folder(files), "holds test_engine.py, which holds the class Engine at line 1, "
                                                   "which is not test code")
        files = {"sub/test_x.py": "def run():\n    return 1\n"}
        self.red(1, self.with_tests_folder(files, rules=""), "core/recursion_core/tests stands under the core")
        self.red(1, self.with_tests_folder(files), "holds sub/test_x.py, which holds the function run at line 1")
        self.red(1, self.with_tests_folder({"__init__.py": "VALUE = 1\n", "test_a.py": None}),
                 "holds __init__.py, which holds a statement (Assign) at line 1, and an __init__.py of a test folder")
        self.red(1, self.with_tests_folder({"test_a.py": "import os\n"}), "holds test_a.py, which defines no test")
        self.red(1, self.with_tests_folder({"test_a.py": "def broken(:\n"}), "cannot be read as Python")

    def test_1_a_product_folder_named_as_ignored_is_red(self):
        rules = '\n[rules]\nignored_folders = ["__pycache__", "evil"]\n'
        tree = self.tree(list_body=list_text(extra=rules), folders=("alpha", "beta", "control", "evil"))
        self.red(1, tree, "core/recursion_core/evil is an ignored folder in the [rules] table of "
                          "core/CORE-PACKAGES.toml, but holds __init__.py, a module")
        tree = self.tree(list_body=list_text(extra=rules))
        (tree / CORE_DIR / "evil" / "deep").mkdir(parents=True)
        (tree / CORE_DIR / "evil" / "deep" / "engine.so").write_bytes(b"")
        self.red(1, tree, "but holds deep/engine.so, a module")
        tree = self.tree(list_body=list_text(extra=rules))
        (tree / CORE_DIR / "evil").mkdir()
        (tree / CORE_DIR / "evil" / "notes.txt").write_text("", encoding="utf-8")
        self.assertEqual(analyse(tree)[0], [])

    def test_2_a_rule_entry_that_matches_nothing_is_red(self):
        for rules, needle in (('\n[rules]\ntest_folders = ["tests"]\n', "rules.test_folders names tests"),
                              ('\n[rules]\nignored_folders = ["gone"]\n', "rules.ignored_folders names gone"),
                              ('\n[rules]\nother_files = ["py.typed"]\n', "rules.other_files names py.typed")):
            self.red(2, self.tree(list_body=list_text(extra=rules)), needle)

    def test_1_a_module_named_as_an_other_file_is_red(self):
        tree = self.tree(list_body=list_text(extra='\n[rules]\nother_files = ["fast.cpython-312-x86_64-linux-gnu.so"]\n'))
        (tree / CORE_DIR / "fast.cpython-312-x86_64-linux-gnu.so").write_bytes(b"")
        self.red(1, tree, "is a module, and the [rules] table of core/CORE-PACKAGES.toml names it under other_files")

    def test_4_a_not_product_row_is_red(self):
        extra = '\n[[package]]\nname = "evil"\nsection = "not_product"\nwhat = "Holds nothing, it says."\n'
        tree = self.tree(list_body=list_text(extra=extra), folders=("alpha", "beta", "control", "evil"))
        (tree / CORE_DIR / "evil" / "engine.py").write_text(self.ENGINE, encoding="utf-8")
        self.red(4, tree, "row evil: the section not_product is not used")

    def test_1_a_hidden_folder_is_not_skipped(self):
        tree = self.tree(list_body=list_text())
        (tree / CORE_DIR / ".hidden").mkdir()
        self.red(1, tree, "core/recursion_core/.hidden stands under the core")

    def test_1_a_file_that_is_no_module_is_red_unless_the_rules_name_it(self):
        tree = self.tree(list_body=list_text())
        (tree / CORE_DIR / "py.typed").write_text("", encoding="utf-8")
        self.red(1, tree, "is neither a folder nor a .py module, so the scan cannot classify it")
        tree = self.tree(list_body=list_text(extra='\n[rules]\nother_files = ["py.typed"]\n'))
        (tree / CORE_DIR / "py.typed").write_text("", encoding="utf-8")
        self.assertEqual(analyse(tree)[0], [])

    def test_1_a_link_is_not_followed_and_is_red(self):
        tree = self.tree(list_body=list_text())
        try:
            (tree / CORE_DIR / "linked").symlink_to(tree / CORE_DIR / "alpha", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this host cannot make a link")
        self.red(1, tree, "is a link, so the scan cannot classify it")

    def test_7_a_name_that_is_a_row_and_a_test_folder_is_red(self):
        tree = self.with_tests_folder(["test_a.py"], rules=self.RULES.replace('["tests"]', '["alpha"]'))
        self.red(7, tree, "alpha is both a row and a test folder")

    def test_4_a_malformed_rules_table_is_red(self):
        for rules in ('\n[rules]\ntest_folders = "tests"\n', '\n[rules]\nsurprise = []\n',
                      '\n[rules]\ntest_folders = ["a/b"]\n'):
            self.red(4, self.tree(list_body=list_text(extra=rules)), "[rules]" if "surprise" in rules else
                     "rules.test_folders is a list of plain names")

    def test_4_a_list_file_that_cannot_be_parsed_is_red(self):
        self.red(4, self.tree(list_body="[summary\n"), "is not valid TOML")

    def test_1_a_stray_folder_is_red(self):
        self.red(1, self.tree(list_body=list_text(), folders=("alpha", "beta", "control", "stray")),
                 "core/recursion_core/stray stands under the core and core/CORE-PACKAGES.toml has no row for it. "
                 "Add a row that cites the lane or section")

    def test_1_a_folder_without_init_is_red(self):
        tree = self.tree(list_body=list_text())
        (tree / CORE_DIR / "bare").mkdir()
        self.red(1, tree, "core/recursion_core/bare stands under the core")

    def test_1_a_stray_module_is_red(self):
        self.red(1, self.tree(list_body=list_text(), modules=("lone",)), "core/recursion_core/lone stands")

    def test_2_a_row_without_a_package_is_red(self):
        extra = '\n[[package]]\nname = "ghost"\nsection = "core"\nwhat = "None."\nplaced_by = [' + \
            cite_text("lane", "F3") + "]\n"
        self.red(2, self.tree(list_body=list_text(extra=extra)),
                 "has a row for ghost, but no such package stands under the core")

    LEAVING = '\n[[package]]\nname = "alpha2"\nsection = "leaving"\nwhat = "Out."\nmoves_to = "a plugin"\n' \
              'leave_by = "2026-12-01"\nplaced_by = [' + DECISION + ']\n'

    def test_2_a_leaving_row_past_its_date_is_red(self):
        tree = self.tree(list_body=list_text(extra=self.LEAVING), folders=("alpha", "beta", "control", "alpha2"))
        self.assertEqual(analyse(tree, today=datetime.date(2026, 11, 30))[0], [])
        self.red(2, tree, "was to leave the core by 2026-12-01", today=datetime.date(2026, 12, 2))

    def test_a_leaving_row_past_its_date_is_named_in_the_printed_line_when_it_is_not_red(self):
        text = list_text(extra=self.LEAVING).replace("for_owner_max = 1\n", 'for_owner_max = 1\nleave_by_passed = "print"\n')
        tree = self.tree(list_body=text, folders=("alpha", "beta", "control", "alpha2"))
        problems, line = analyse(tree, today=datetime.date(2026, 12, 2))
        self.assertEqual(problems, [])
        self.assertIn("leaving 1", line)
        self.assertIn("; past its leave_by: alpha2 (2026-12-01)", line)
        self.assertNotIn("past its leave_by", analyse(tree, today=datetime.date(2026, 11, 30))[1])

    def test_3_an_expected_row_that_stands_is_red(self):
        self.red(3, self.tree(list_body=list_text(), folders=("alpha", "beta", "control", "later")),
                 "the row for later is in the section expected, but the package stands")

    def test_3_an_expected_row_the_seam_no_longer_names_is_red(self):
        hooks = "REGISTRANTS = (\n    'recursion_core.alpha.hooks',\n    'recursion_core.beta.hooks',\n)\n"
        self.red(3, self.tree(list_body=list_text(), hooks=hooks), "REGISTRANTS in core/recursion_core/control/"
                 "hooks.py no longer names it")

    def test_3_a_registrant_without_a_row_is_red(self):
        hooks = HOOKS.replace(")\n", "    'recursion_core.fresh.hooks',\n)\n")
        self.red(3, self.tree(list_body=list_text(), hooks=hooks), "names recursion_core.fresh")

    def test_3_a_registrant_that_is_not_a_literal_is_red(self):
        for hooks in ("REGISTRANTS = tuple(['recursion_core.alpha.hooks'])\n",
                      "REGISTRANTS = ('recursion_core.alpha.hooks', NAME)\n",
                      "OTHER = ()\n"):
            self.red(3, self.tree(list_body=list_text(), hooks=hooks), "is not a literal tuple of strings")

    def test_4_a_wrong_ref_is_red(self):
        for ref in ("f3", "F", "Quillfern lane", "3..3"):
            kind = "section" if ref == "3..3" else "lane"
            self.red(4, self.tree(list_body=list_text(alpha_cite=cite_text(kind, ref))), "Rewrite ref")

    def test_4_a_contract_other_than_pinned_is_red(self):
        cite = cite_text("lane", "F3").replace('"pinned"', '"main"')
        self.red(4, self.tree(list_body=list_text(alpha_cite=cite)), "contract must be 'pinned'")

    def test_4_a_digest_in_this_file_is_red(self):
        for extra in (f', row_sha256 = "{digest(F3_ROW)}"', ', line_digest = "abc"'):
            self.red(4, self.tree(list_body=list_text(alpha_cite=cite_text("lane", "F3", extra))),
                     "a cite in this file carries no digest")
        cite = DECISION.replace(" }", ', sha256 = "abc" }')
        self.red(4, self.tree(list_body=list_text(alpha_cite=cite)), "a cite in this file carries no digest")

    def test_4_a_decision_cite_with_anything_but_role_and_date_is_red(self):
        # Invented stand-ins: a made-up first name and a made-up entry number.
        for ref in ("the owner Robin, 2026-11-24", "Robin, 2026-11-24", "the owner, 2026-13-45",
                    "the owner, last week", "entry 999"):
            cite = '{ kind = "decision", ref = "%s" }' % ref
            extra = '\n[[package]]\nname = "x"\nsection = "expected"\nwhat = "X."\nplaced_by = [' + cite + "]\n"
            hooks = HOOKS.replace(")\n", "    'recursion_core.x.hooks',\n)\n")
            self.red(4, self.tree(list_body=list_text(extra=extra), hooks=hooks), "by role and date only")

    def test_4_a_decision_cite_with_a_quoted_text_is_red(self):
        cite = '{ kind = "decision", ref = "the owner, 2026-11-24", quote = "words" }'
        extra = '\n[[package]]\nname = "x"\nsection = "expected"\nwhat = "X."\nplaced_by = [' + cite + "]\n"
        hooks = HOOKS.replace(")\n", "    'recursion_core.x.hooks',\n)\n")
        self.red(4, self.tree(list_body=list_text(extra=extra), hooks=hooks), "holds only kind and ref")

    def test_4_decided_out_without_a_decision_is_red(self):
        later = "decided_out = true\n"
        self.red(4, self.tree(list_body=list_text(later_row=later)), "decided_out needs the cite of the decision")
        self.assertEqual(analyse(self.tree(list_body=list_text(later_row=later + f"placed_by = [{DECISION}]\n")))[0], [])
        self.red(4, self.tree(list_body=list_text().replace('what = "One."', 'what = "One."\ndecided_out = true')),
                 "decided_out is true on an expected row only")

    def test_4_a_core_row_without_a_cite_is_red(self):
        text = list_text().replace('placed_by = [' + cite_text("lane", "F3") + "]\n", "")
        self.red(4, self.tree(list_body=text), "a core row needs a cite")

    def test_4_a_for_owner_row_without_builds_it_or_with_placed_by_is_red(self):
        self.red(4, self.tree(list_body=list_text(beta_row="")), "a for_owner row needs builds_it")
        beta = f'builds_it = [{cite_text("lane", "D1")}]\nplaced_by = [{cite_text("lane", "F3")}]'
        self.red(4, self.tree(list_body=list_text(beta_row=beta)), "a for_owner row has no placing line")

    def test_4_a_decision_in_the_builds_it_of_a_for_owner_row_is_red(self):
        # builds_it names the lane or section that builds the package; a decision names neither.
        needle = "builds_it names the lane or section that builds the package"
        for beta in (f"builds_it = [{DECISION}]", f'builds_it = [{cite_text("lane", "D1")}, {DECISION}]'):
            self.red(4, self.tree(list_body=list_text(beta_row=beta)), needle)
        for kind, ref in (("lane", "D1"), ("section", "3.3")):
            beta = f"builds_it = [{cite_text(kind, ref)}]"
            self.assertEqual(analyse(self.tree(list_body=list_text(beta_row=beta)))[0], [])

    def test_4_a_leaving_row_without_moves_to_or_leave_by_is_red(self):
        for fields, needle in (('leave_by = "2027-01-01"\n', "needs moves_to"), ('moves_to = "a plugin"\n', "needs leave_by")):
            extra = '\n[[package]]\nname = "alpha2"\nsection = "leaving"\nwhat = "Out."\n' + fields + \
                    'placed_by = [' + DECISION + ']\n'
            self.red(4, self.tree(list_body=list_text(extra=extra), folders=("alpha", "beta", "control", "alpha2")), needle)

    def test_4_an_unknown_section_or_field_is_red(self):
        text = list_text().replace('section = "core"', 'section = "kernel"')
        self.red(4, self.tree(list_body=text), "section must be one of")
        text = list_text().replace('what = "One."', 'what = "One."\ncolour = "red"')
        self.red(4, self.tree(list_body=text), "unknown field")

    def test_6_a_raised_count_is_red(self):
        self.red(6, self.tree(list_body=list_text(max_count=0)),
                 "1 packages wait for the owner's placing, and the file says 0. Set for_owner_max to 1")

    def test_6_a_lowered_count_is_red(self):
        self.red(6, self.tree(list_body=list_text(max_count=3)),
                 "1 packages wait for the owner's placing, and the file says 3. Lower for_owner_max to 1")

    def test_7_an_empty_tree_is_red(self):
        tree = self.tree(list_body=list_text(), folders=(), hooks=None)
        self.red(7, tree, "found no package under core/recursion_core, so it scanned nothing")

    def test_7_a_missing_scan_root_is_red(self):
        tree = self.tree(list_body=list_text(), with_core=False)
        self.red(7, tree, "found no folder at core/recursion_core, so it scanned nothing")

    def test_7_a_missing_list_pin_or_hooks_is_red(self):
        self.red(7, self.tree(list_body=None), "core/CORE-PACKAGES.toml is not there")
        self.red(7, self.tree(list_body=list_text(), pin=False), "pin file contract/PRODUCT.pin.json was not found")
        self.red(7, self.tree(list_body=list_text(), hooks=None), "hooks file")

    def test_7_a_folder_and_a_module_of_one_name_is_red(self):
        self.red(7, self.tree(list_body=list_text(), modules=("alpha",)), "stands twice")

    def test_7_two_rows_of_one_name_are_red(self):
        extra = f'\n[[package]]\nname = "alpha"\nsection = "core"\nwhat = "Again."\nplaced_by = [{DECISION}]\n'
        self.red(7, self.tree(list_body=list_text(extra=extra)), "two rows name alpha")

    # The note on the cites, which the printed state line carries on every run.
    def test_the_printed_line_says_when_the_cites_were_not_checked(self):
        _, _, note, problems = cite_inputs({})
        self.assertEqual(problems, [])
        self.assertIn("the cites were NOT checked against the pinned contract, only their form: CSL_RESEARCH_VAULT "
                      "and CSL_CORE_LIST_DIGESTS are unset, so no cited line was read", note)
        for environ in ({VAULT_ENV: "/somewhere"}, {DIGESTS_ENV: "/somewhere"}):
            _, _, note, problems = cite_inputs(environ)
            self.assertIn("were NOT checked", note)
            self.assertEqual([code for code, _ in problems], [5])
        vault, digests, note, problems = cite_inputs({VAULT_ENV: "/a", DIGESTS_ENV: "/b"})
        self.assertEqual((vault, digests, problems), ("/a", "/b", []))
        self.assertNotIn("NOT", note)
        self.assertNotIn("/a", note)
        self.assertIn("the lane and section cites are read against the pinned contract", note)
        self.assertIn("the public tree is swept for words of the contract (check 8)", note)

    def test_the_note_says_that_a_decision_cite_is_checked_for_its_form_only(self):
        for environ in ({}, {VAULT_ENV: "/a"}, {DIGESTS_ENV: "/b"}, {VAULT_ENV: "/a", DIGESTS_ENV: "/b"}):
            note = cite_inputs(environ)[2]
            self.assertIn("a decision cite is checked for its form only and no line is read for it", note, environ)
        self.assertIn("the public tree was NOT swept for words of the contract (check 8)", cite_inputs({})[2])
        self.assertIn("the public tree was NOT swept", cite_inputs({DIGESTS_ENV: "/b"})[2])
        self.assertIn("the public tree is swept", cite_inputs({VAULT_ENV: "/a"})[2])

    def test_the_printed_line_names_the_rows_placed_by_a_decision_alone(self):
        _, line = analyse(self.tree(list_body=list_text()))
        self.assertIn("placed 2 (1 by a decision alone: control)", line)
        _, line = analyse(self.tree(list_body=list_text().replace(DECISION, cite_text("lane", "F3"))))
        self.assertIn("placed 2 (0 by a decision alone: none)", line)
        _, line = analyse(self.tree(list_body=list_text(alpha_cite=DECISION)))
        self.assertIn("placed 2 (2 by a decision alone: alpha, control)", line)
        # A lane under builds_it does not place the row: placed by a decision alone, it is named.
        body = list_text(alpha_cite=DECISION).replace('what = "One."'+chr(10), 'what = "One."'+chr(10)+'builds_it = [' + cite_text("lane", "F3") + ']'+chr(10))
        problems, line = analyse(self.tree(list_body=body))
        self.assertEqual(problems, [])
        self.assertIn("placed 2 (2 by a decision alone: alpha, control)", line)
        # A row with a decision and a line of the contract is read against the contract, so it is not alone.
        _, line = analyse(self.tree(list_body=list_text(alpha_cite=f"{DECISION}, {cite_text('lane', 'F3')}")))
        self.assertIn("placed 2 (1 by a decision alone: control)", line)

    def test_the_note_is_printed_at_the_default_verbosity_of_a_plain_run(self):
        environ = {k: v for k, v in os.environ.items() if k not in (VAULT_ENV, DIGESTS_ENV)}
        run = subprocess.run([sys.executable, "-B", str(HERE), "RealTreeTests.test_the_state_of_the_core_is_printed"],
                             capture_output=True, text=True, env=environ, timeout=CHILD_TIMEOUT_SECONDS)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        self.assertIn("Under the core: ", run.stderr)
        self.assertIn("the cites were NOT checked against the pinned contract", run.stderr)
        self.assertIn("a decision cite is checked for its form only", run.stderr)
        self.assertIn("the public tree was NOT swept for words of the contract", run.stderr)

    def test_check_8_says_loudly_that_it_did_not_run_when_the_clone_is_absent(self):
        environ = {k: v for k, v in os.environ.items() if k not in (VAULT_ENV, DIGESTS_ENV)}
        name = "RealTreeTests.test_8_no_heading_and_no_run_of_words_of_the_private_contract_stands_in_the_public_tree"
        run = subprocess.run([sys.executable, "-B", str(HERE), "-v", name], capture_output=True, text=True,
                             env=environ, timeout=CHILD_TIMEOUT_SECONDS)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        self.assertIn("Check 8 did NOT run", run.stderr)
        self.assertIn("skipped", run.stderr)

    # The vault check, on a stand-in clone of the research repository.
    def vault_case(self, edit=None, verify=True, rows=None, list_body=None):
        self.n += 1
        vault = self.base / f"vault{self.n}"
        pin = make_vault(vault, PRODUCT)
        if edit:
            (vault / "contracts" / "PRODUCT.md").write_bytes(edit(PRODUCT).encode("utf-8"))
            run_git(["commit", "-qam", "edit"], vault)
            pin["commit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=vault, check=True, capture_output=True,
                                           text=True, timeout=CHILD_TIMEOUT_SECONDS).stdout.strip()
            pin["sha256"] = hashlib.sha256(edit(PRODUCT).encode("utf-8")).hexdigest()
        data = tomllib.loads(list_body or list_text())
        companion = tomllib.loads(digests_text(STAND_IN_DIGESTS if rows is None else rows))
        return check_vault(data, pin, str(vault), companion, verify_digests=verify)

    def section_case(self, line_digest, edit=None):
        body = list_text(alpha_cite=cite_text("section", "3.3"))
        rows = [("alpha", "placed_by", "section", "3.3", line_digest), STAND_IN_DIGESTS[1]]
        return self.vault_case(edit=edit, rows=rows, list_body=body)

    def test_5_the_stand_in_clone_is_green(self):
        self.assertEqual(self.vault_case(), [])

    def test_5_a_changed_lane_row_is_red(self):
        problems = self.vault_case(lambda p: p.replace("one zorbwick", "two zorbwicks"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("The row for beta cites D1 in builds_it, and that line of the pinned contract has changed "
                      "since the row was written (or is not there). Read the line again and write the row again.",
                      problems[0][1])

    def test_5_a_missing_lane_and_a_missing_section_are_red(self):
        problems = self.vault_case(lambda p: p.replace("| **F3 Quillfern lane**", "| **F4 Quillfern lane**"))
        self.assertEqual(len([t for n, t in problems if "alpha cites F3" in t]), 1, problems)
        body = list_text(alpha_cite=cite_text("section", "3.9"))
        rows = [("alpha", "placed_by", "section", "3.9", "0" * 64), STAND_IN_DIGESTS[1]]
        problems = self.vault_case(rows=rows, list_body=body)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("alpha cites 3.9", problems[0][1])

    def test_5_a_section_cite_names_one_line_inside_its_section(self):
        self.assertEqual(self.section_case(digest(PLACING)), [])

    def test_5_the_heading_of_a_section_does_not_place(self):
        problems = self.section_case(digest("### 3.3 Quillfern section"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("alpha cites 3.3 in placed_by, and that line of the pinned contract has changed", problems[0][1])

    def test_5_a_line_outside_its_section_does_not_place(self):
        # The same words stand under 3.4 and section 7; only a line inside 3.3 counts.
        problems = self.section_case(digest("- The quillfern core drives the snarfles too."))
        self.assertEqual(len(problems), 1, problems)
        problems = self.section_case(digest(F3_ROW))
        self.assertEqual(len(problems), 1, problems)

    def test_5_a_changed_placing_line_is_red(self):
        problems = self.section_case(digest(PLACING), edit=lambda p: p.replace("drives the snarfles.", "may drive."))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("has changed since the row was written", problems[0][1])

    def test_5_two_lines_of_one_digest_in_a_section_are_red(self):
        problems = self.section_case(digest(PLACING), edit=lambda p: p.replace(PLACING, PLACING + "\n" + PLACING, 1))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("2 lines inside that section have the digest given for it", problems[0][1])

    def test_5_the_file_of_digests_must_match_the_cites(self):
        problems = self.vault_case(rows=STAND_IN_DIGESTS[:1])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("beta cites D1 in builds_it, and the file of digests has no digest for that cite", problems[0][1])
        problems = self.vault_case(rows=STAND_IN_DIGESTS + [("gamma", "placed_by", "lane", "F3", digest(F3_ROW))])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("has a row for gamma, placed_by, F3, and core/CORE-PACKAGES.toml has no such cite", problems[0][1])
        problems = self.vault_case(rows=[r[:4] + ("abc",) for r in STAND_IN_DIGESTS])
        self.assertTrue(problems and all("Write it so" in t or "no digest for that cite" in t for _, t in problems),
                        problems)
        problems = self.vault_case(rows=STAND_IN_DIGESTS + STAND_IN_DIGESTS[:1])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("two rows for alpha", problems[0][1])
        found = read_digests(self.base / "absent.toml")[1]
        self.assertEqual([code for code, _ in found], [5])

    def test_5_a_clone_that_is_not_the_pinned_bytes_is_red(self):
        vault = self.base / "vault-other"
        pin = make_vault(vault, PRODUCT)
        pin["sha256"] = "0" * 64
        problems = check_vault(tomllib.loads(list_text()), pin, str(vault), tomllib.loads(digests_text(STAND_IN_DIGESTS)))
        self.assertEqual(len(problems), 1)
        self.assertIn("do not match the SHA-256", problems[0][1])

    def test_5_the_digest_is_what_makes_a_changed_row_red(self):
        # The digest is the whole check of a row's words: with it skipped a changed row goes green.
        changed = lambda p: p.replace("one zorbwick", "two zorbwicks")
        self.assertNotEqual(self.vault_case(changed), [])
        self.assertEqual(self.vault_case(changed, verify=False), [])
        self.assertEqual(len(self.vault_case(changed, verify=True)), 1)

    # L1, L3 and L4 of the first rounds.
    def test_1_a_module_inside_the_pycache_folder_is_red(self):
        tree = self.tree(list_body=list_text())
        (tree / CORE_DIR / "__pycache__").mkdir()
        (tree / CORE_DIR / "__pycache__" / "x.py").write_text("", encoding="utf-8")
        self.red(1, tree, "is an ignored folder")

    def test_4_a_run_of_64_hex_digits_anywhere_in_the_list_is_red(self):
        run = "0123456789abcdef" * 4
        for extra in (f"\n# {run}\n", f'\n[[package]]\nname = "z"\nsection = "core"\nwhat = "{run}"\n'):
            self.red(4, self.tree(list_body=list_text(extra=extra)), "64 hexadecimal digits")

    def test_4_an_unknown_top_level_table_is_red_and_the_reserved_ones_are_not(self):
        self.red(4, self.tree(list_body=list_text(extra="\n[packages]\nx = 1\n")), "unknown table ['packages']")
        for table in ("call", "dynamic_allow"):
            self.assertEqual(analyse(self.tree(list_body=list_text(extra=f"\n[{table}]\nx = 1\n")))[0], [])

    # Check 8: nothing of the private contract stands in the public tree. The stand-in contract is PRODUCT.
    def sweep(self, files, skip=(), **settings):
        self.n += 1
        root = self.base / f"pub{self.n}"
        root.mkdir()
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        return sweep_public_tree(root, PRODUCT, skip=[root / name for name in skip], **settings)

    def test_8_a_clean_tree_is_green(self):
        self.assertEqual(self.sweep({"a.md": "Only ordinary words stand here, about a plain tree.\n",
                                     "b/c.py": "VALUE = 1\n"}), [])

    def test_8_a_heading_of_the_contract_is_red_and_the_message_holds_none_of_its_words(self):
        problems = self.sweep({"docs/t.py": 'X = 1\nTEXT = "## 3. Quillfern part"\n'})
        self.assertEqual([code for code, _ in problems], [8])
        self.assertIn("docs/t.py line 2 holds a heading of the private contract (line 3 of the pinned bytes)",
                      problems[0][1])
        self.assertNotIn("uillfern", problems[0][1])
        self.assertEqual(self.sweep({"t.md": "### 3.3 Quillfern section\n"})[0][0], 8)
        # A line with more heading marks than the contract's heading is not that heading.
        self.assertEqual(self.sweep({"t.md": "#### 3. Quillfern part\n"}), [])

    def test_8_a_run_of_six_words_of_the_contract_is_red_and_five_are_not(self):
        problems = self.sweep({"n.txt": "so: the Quillfern core drives the snarfles, then\n"})
        self.assertEqual([code for code, _ in problems], [8])
        self.assertIn("n.txt line 1 holds a run of 6 words of the private contract (line 7 of the pinned bytes); "
                      "1 such runs in the file", problems[0][1])
        self.assertNotIn("narfle", problems[0][1])
        self.assertEqual(self.sweep({"n.txt": "x quillfern core drives the snarfles y\n"}), [])

    def test_8_the_words_of_a_link_are_not_a_run(self):
        self.assertEqual(self.sweep({"n.md": "[x](https://example.invalid/the/quillfern/core/drives/the/snarfles)\n"}), [])

    def test_8_the_run_length_and_the_left_out_files_are_settings_of_the_list(self):
        five = {"n.txt": "x quillfern core drives the snarfles y\n"}
        self.assertEqual(self.sweep(five), [])
        self.assertEqual(self.sweep(five, run_words=5)[0][0], 8)
        files = {"AGENTS.md": "### 3.3 Quillfern section\n", "docs/a.md": "### 3.3 Quillfern section\n"}
        self.assertEqual([t.split(" line")[0] for _, t in self.sweep(files)], ["AGENTS.md", "docs/a.md"])
        self.assertEqual([t.split(" line")[0] for _, t in self.sweep(files, leave_out=["AGENTS.md"])], ["docs/a.md"])
        self.assertEqual(self.sweep(files, leave_out=["AGENTS.md", "docs"]), [])
        self.assertEqual(self.sweep({"docsmore/a.md": "### 3.3 Quillfern section\n"}, leave_out=["docs"])[0][0], 8)
        self.assertEqual(sweep_settings({}), (SWEEP_RUN_WORDS, [SWEEP_ROOT], []))
        self.assertEqual(sweep_settings({"sweep": {"run_words": 8, "scan": ["y"], "leave_out": ["x"]}}),
                         (8, ["y"], ["x"]))
        self.assertEqual(self.sweep(files, scan=["docs"])[0][1].split(" line")[0], "docs/a.md")
        self.assertEqual(self.sweep(files, scan=["AGENTS.md", "docs"], leave_out=["docs/a.md"])[0][1].split(" line")[0],
                         "AGENTS.md")
        self.assertEqual(self.sweep(files, scan=["elsewhere"]), [])

    def test_4_the_sweep_table_is_checked_for_its_form(self):
        for table in ('run_words = 1', 'run_words = "six"', 'run_words = true', 'leave_out = ["/etc"]',
                      'leave_out = ["../x"]', 'leave_out = [""]', 'leave_out = "AGENTS.md"', 'scan = [1]',
                      'scan = "."', 'other = 1'):
            self.red(4, self.tree(list_body=list_text(extra=f"\n[sweep]\n{table}\n")), "[sweep] holds run_words")
        tree = self.tree(list_body=list_text(extra='\n[sweep]\nrun_words = 6\nleave_out = ["AGENTS.md"]\n'))
        self.red(2, tree, "sweep.leave_out names AGENTS.md, but nothing of that name stands")
        (tree / "AGENTS.md").write_text("x\n", encoding="utf-8")
        self.assertEqual(analyse(tree)[0], [])
        tree = self.tree(list_body=list_text(extra='\n[sweep]\nscan = ["docs"]\n'))
        self.red(2, tree, "sweep.scan names docs, but nothing of that name stands")

    LEAK = "### 3.3 Quillfern section\n"

    def test_8_a_sweep_entry_is_compared_in_its_normal_form(self):
        files = {"core/tests/leak.txt": self.LEAK, "notes/leak.md": self.LEAK, "core/CORE-PACKAGES.toml": "# x\n"}
        for scan, where in ((["./core/tests"], "core/tests/leak.txt"), (["core/tests/"], "core/tests/leak.txt"),
                            (["core/../core/tests"], "core/tests/leak.txt"), (["core//tests"], "core/tests/leak.txt"),
                            (["./notes/leak.md"], "notes/leak.md"), (["notes/leak.md/"], "notes/leak.md"),
                            (["notes//leak.md"], "notes/leak.md"), (["notes/x/../leak.md"], "notes/leak.md")):
            self.assertEqual([text.split(" line")[0] for _, text in self.sweep(files, scan=scan)], [where], scan)
        self.assertEqual(self.sweep(files, scan=["core"], leave_out=["./core/tests/leak.txt/"]), [])
        self.assertEqual(sweep_settings({"sweep": {"scan": ["./core/tests/", "a//b"], "leave_out": ["c/d/.."]}}),
                         (SWEEP_RUN_WORDS, ["core/tests", "a/b"], ["c"]))

    def test_4_a_sweep_entry_that_cannot_be_compared_is_refused_by_its_name(self):
        for table, needle in (('scan = []', "[sweep] scan is empty, so the sweep would read no file"),
                              ('scan = ["../elsewhere"]', "[sweep] scan holds ../elsewhere, which leaves the repository"),
                              ('scan = ["core/../../x"]', "[sweep] scan holds core/../../x, which leaves the repository"),
                              ('leave_out = ["../x"]', "[sweep] leave_out holds ../x, which leaves the repository"),
                              ('scan = ["/etc"]', "[sweep] scan holds /etc, an absolute path"),
                              ('leave_out = ["/core"]', "[sweep] leave_out holds /core, an absolute path"),
                              ('scan = ["core", "./core/"]', "[sweep] scan names core twice (as core and as ./core/)"),
                              ('scan = ["core"]\nleave_out = ["core/x", "core//x"]', "[sweep] leave_out names core/x twice"),
                              ('leave_out = ["./"]', "[sweep] leave_out names ., which leaves out the whole of the scan "
                                                     "entry ."),
                              ('scan = ["core/tests"]\nleave_out = ["core/"]', "leaves out the whole of the scan entry "
                                                                              "core/tests")):
            self.red(4, self.tree(list_body=list_text(extra=f"\n[sweep]\n{table}\n")), needle)
        # The sweep itself refuses such an entry and reads nothing.
        for scan in ([], ["../x"], ["/etc"], ["a", "./a"]):
            problems = self.sweep({"a/leak.md": self.LEAK}, scan=scan)
            self.assertTrue(problems, scan)
            self.assertEqual({code for code, _ in problems}, {4}, scan)

    def test_2_a_sweep_entry_that_matches_no_file_is_red(self):
        tree = self.tree(list_body=list_text(extra='\n[sweep]\nscan = ["./docs/"]\n'))
        self.red(2, tree, "sweep.scan names docs, but nothing of that name stands")
        (tree / "docs" / "__pycache__").mkdir(parents=True)
        self.red(2, tree, "sweep.scan names docs, but no file the sweep reads stands there")
        (tree / "docs" / "__pycache__" / "a.txt").write_text("x\n", encoding="utf-8")
        self.red(2, tree, "sweep.scan names docs, but no file the sweep reads stands there")
        (tree / "docs" / "a.md").write_text("x\n", encoding="utf-8")
        self.assertEqual(analyse(tree)[0], [])

    def test_4_a_value_of_the_wrong_kind_in_the_sweep_table_is_refused_in_words(self):
        for table, needle in (('scan = true', "[sweep] scan is a truth value, and it must be a list of paths"),
                              ('scan = 1', "[sweep] scan is a number, and it must be a list of paths"),
                              ('scan = "core"', "[sweep] scan is a string, and it must be a list of paths"),
                              ('scan = 2026-11-26', "[sweep] scan is a date or a time, and it must be a list of paths"),
                              ('leave_out = 2.5', "[sweep] leave_out is a number, and it must be a list of paths"),
                              ('leave_out = false', "[sweep] leave_out is a truth value, and it must be a list"),
                              ('scan = ["core", true]', "[sweep] scan holds a truth value, and every entry must be a "
                                                        "path written as a string"),
                              ('leave_out = [1]', "[sweep] leave_out holds a number, and every entry must be a path"),
                              ('scan = [["core"]]', "[sweep] scan holds a list, and every entry must be a path"),
                              ('run_words = "six"', "[sweep] run_words is a string, and it must be a whole number, 2 "
                                                    "or more"),
                              ('run_words = true', "[sweep] run_words is a truth value, and it must be a whole number"),
                              ('run_words = 1', "[sweep] run_words is 1, and it must be a whole number, 2 or more")):
            self.red(4, self.tree(list_body=list_text(extra=f"\n[sweep]\n{table}\n")), needle)
            self.assertEqual(len(sweep_settings(tomllib.loads(f"[sweep]\n{table}\n"))), 3, table)

    def test_8_a_number_or_a_cite_field_breaks_a_run(self):
        self.assertEqual(self.sweep({"n.txt": "the quillfern core 7 drives the snarfles\n"}), [])
        self.assertEqual(self.sweep({"n.txt": "the quillfern core drives the snarfles\n"})[0][0], 8)

    def test_8_a_number_inside_a_run_of_the_contract_does_not_make_it_a_run_of_words(self):
        self.n += 1
        root = self.base / f"pub{self.n}"
        root.mkdir()
        (root / "n.txt").write_text("alpha beta 7 gamma delta\n", encoding="utf-8")
        self.assertEqual(sweep_public_tree(root, "alpha beta 7 gamma delta epsilon\n", run_words=4), [])
        (root / "n.txt").write_text("gamma delta epsilon zeta\n", encoding="utf-8")
        self.assertEqual(sweep_public_tree(root, "alpha beta 7 gamma delta epsilon zeta\n", run_words=4)[0][0], 8)

    def test_8_a_run_wrapped_over_two_lines_is_red(self):
        self.assertEqual(self.sweep({"n.txt": "the quillfern core\ndrives THE snarfles\n"})[0][0], 8)

    def test_8_the_clone_is_left_out_and_so_are_caches(self):
        files = {"vault/p.md": "### 3.3 Quillfern section\n", "__pycache__/p.py": "### 3.3 Quillfern section\n"}
        self.assertEqual(self.sweep(files, skip=["vault"]), [])
        self.assertEqual(self.sweep({"vault/p.md": "### 3.3 Quillfern section\n"})[0][0], 8)

    def test_8_a_file_that_is_not_text_is_not_read(self):
        self.n += 1
        root = self.base / f"pub{self.n}"
        root.mkdir()
        (root / "b.bin").write_bytes(b"\xff\xfe### 3.3 Quillfern section\n")
        self.assertEqual(sweep_public_tree(root, PRODUCT), [])


if __name__ == "__main__":
    unittest.main()
