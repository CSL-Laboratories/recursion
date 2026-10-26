#!/usr/bin/env python3
"""X1 inventory floor for the Recursion legacy deletion boundary (CSL-495, WP14).

Reads docs/v3/x1/RECURSION-X1-INVENTORY.md and fails when the disposition table, the oracle
register or the pinned counts drift from the tree. It runs inside the existing `linux`,
`windows` and `macos` jobs through tools/ci_gate.py discovery; no job name or workflow changes.

Deletion is gated: while X1_DELETION_LANDED is False the boundary must still exist in full.
The deletion PR (after the Nov 10 rehearsal) flips the flag; the test then stays red until every
port oracle has a successor carrier that exists, is run by the gate and no longer references the
boundary.
"""
from __future__ import annotations

import fnmatch
import importlib.util
import re
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DOC = "docs/v3/x1/RECURSION-X1-INVENTORY.md"

X1_DELETION_LANDED = False

BOUNDARY_CODE = ("scripts/v3/harness", "scripts/v3/planning")
LEGACY_TEST_DIRS = ("scripts/tests/v3/harness", "scripts/tests/v3/planning")
KEEP_PATHS = ("scripts/v3/route_adapter",)

DISPOSITIONS = {"port", "drop", "keep"}

# Pinned floors (M1_MIN_PROGRAMS style). Raise when the inventory grows; lowering one is a
# reviewed decision, not a drive-by edit.
FLOORS = {
    "rows": 16,
    "legacy_test_files": 10,
    "support_modules": 2,
    "planning_fixtures": 23,
    "harness_stream_fixtures": 5,
    "oracles": 12,
    "port_oracles": 10,
    "satisfied_oracles": 2,
    "harness_code_files": 20,
    "planning_code_files": 3,
}

HARNESS_STREAM_FIXTURES = (
    "scripts/tests/v3/harness/fixtures/claude-stream-json.txt",
    "scripts/tests/v3/harness/fixtures/codex-cumulative.txt",
    "scripts/tests/v3/harness/fixtures/codex-exec-jsonl.txt",
    "scripts/tests/v3/harness/fixtures/grok-review.txt",
    "scripts/tests/v3/harness/fixtures/grok-streaming-json.txt",
)
SUPPORT_MODULES = (
    "scripts/tests/v3/planning/materialize_fixtures.py",
    "scripts/tests/v3/planning/reduce_reference.py",
)

BOUNDARY_REFERENCE = re.compile(
    r"scripts\.v3\.(?:harness|planning)\b"
    r"|scripts/v3/(?:harness|planning)\b"
    r"|\"scripts\"\s*/\s*\"v3\"\s*/\s*\"(?:harness|planning)\""
)
PATH_CELL = re.compile(r"`([^`]+)`")
ORACLE_ID = re.compile(r"\bO-[PH]\d+\b")
SKIP_DIRS = {"__pycache__", ".ruff_cache"}


@dataclass(frozen=True)
class Row:
    row: str
    patterns: tuple[str, ...]
    files: int
    disposition: str
    oracles: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Oracle:
    oracle: str
    legacy: tuple[str, ...]
    successor: str | None


def _table_after(text: str, marker: str) -> list[list[str]]:
    lines = text.splitlines()
    try:
        start = lines.index(marker)
    except ValueError:
        raise AssertionError(f"missing marker {marker}") from None
    rows: list[list[str]] = []
    for line in lines[start + 1 :]:
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(cells)
    if not rows:
        raise AssertionError(f"empty table after {marker}")
    return rows[1:]


def parse_rows(text: str) -> list[Row]:
    rows = []
    for cells in _table_after(text, "<!-- x1-disposition-table -->"):
        if len(cells) != 6:
            raise AssertionError(f"disposition row needs 6 cells: {cells}")
        name, paths, files, disposition, oracle, reason = cells
        rows.append(
            Row(
                row=name,
                patterns=tuple(PATH_CELL.findall(paths)),
                files=int(files),
                disposition=disposition,
                oracles=tuple(ORACLE_ID.findall(oracle)),
                reason=reason,
            )
        )
    return rows


def parse_oracles(text: str) -> list[Oracle]:
    oracles = []
    for cells in _table_after(text, "<!-- x1-oracle-register -->"):
        if len(cells) != 4:
            raise AssertionError(f"oracle row needs 4 cells: {cells}")
        name, _contract, legacy, successor = cells
        found = PATH_CELL.findall(successor)
        oracles.append(Oracle(name, tuple(PATH_CELL.findall(legacy)), found[0] if found else None))
    return oracles


def _files_under(root: Path, rel_dir: str) -> list[str]:
    base = root / rel_dir
    if not base.is_dir():
        return []
    found = []
    for path in sorted(base.rglob("*")):
        if path.is_file() and not (set(path.relative_to(root).parts) & SKIP_DIRS):
            found.append(path.relative_to(root).as_posix())
    return found


def _is_test_name(name: str) -> bool:
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith(
        (".test.py", "_test.py", "_tests.py")
    )


def _matches(patterns: tuple[str, ...], path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def references_boundary(root: Path, rel: str) -> bool:
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(BOUNDARY_REFERENCE.search(text))


def oracle_status(root: Path, oracle: Oracle, run_paths: set[str]) -> str:
    if oracle.successor is None:
        return "pending"
    path = root / oracle.successor
    if not path.is_file() or oracle.successor not in run_paths:
        return "pending"
    if references_boundary(root, oracle.successor):
        return "pending"
    return "ported"


def collect_errors(
    root: Path,
    doc_text: str,
    run_paths: set[str],
    *,
    landed: bool,
    floors: dict[str, int],
) -> list[str]:
    errors: list[str] = []
    rows = parse_rows(doc_text)
    oracles = parse_oracles(doc_text)
    oracle_ids = [oracle.oracle for oracle in oracles]
    if len(set(oracle_ids)) != len(oracle_ids):
        errors.append("duplicate oracle IDs in the register")
    if len({row.row for row in rows}) != len(rows):
        errors.append("duplicate row names in the disposition table")

    disk = [path for rel in LEGACY_TEST_DIRS for path in _files_under(root, rel)]
    for path in disk:
        hits = [row.row for row in rows if _matches(row.patterns, path)]
        if len(hits) != 1:
            errors.append(f"{path}: expected exactly one row, found {hits or 'none'}")

    for row in rows:
        if row.disposition not in DISPOSITIONS:
            errors.append(f"{row.row}: disposition {row.disposition!r} is not port, drop or keep")
        if not row.reason:
            errors.append(f"{row.row}: empty reason")
        if row.disposition in {"port", "keep"} and not row.oracles:
            errors.append(f"{row.row}: a {row.disposition} row needs an oracle")
        for oracle_id in row.oracles:
            if oracle_id not in oracle_ids:
                errors.append(f"{row.row}: unknown oracle {oracle_id}")
        matched = [path for path in disk if _matches(row.patterns, path)]
        if landed and row.disposition == "drop":
            ok = len(matched) == 0
        elif landed and row.disposition == "port":
            ok = len(matched) <= row.files
        else:
            ok = len(matched) == row.files
        if not ok:
            rule = "0 after deletion" if landed and row.disposition == "drop" else f"declared {row.files}"
            errors.append(f"{row.row}: matches {len(matched)} files, {rule}")

    ported_in_rows = {oid for row in rows if row.disposition == "port" for oid in row.oracles}
    for oracle in oracles:
        if not oracle.legacy:
            errors.append(f"{oracle.oracle}: no legacy carrier")
        for carrier in oracle.legacy:
            if not landed and not (root / carrier).is_file():
                errors.append(f"{oracle.oracle}: legacy carrier missing: {carrier}")

    port_oracles = [
        oracle for oracle in oracles if oracle.oracle in ported_in_rows
    ]
    statuses = {oracle.oracle: oracle_status(root, oracle, run_paths) for oracle in oracles}
    satisfied = [oid for oid, status in statuses.items() if status == "ported"]

    boundary_code = {
        rel: [p for p in _files_under(root, rel) if p.endswith(".py")] for rel in BOUNDARY_CODE
    }
    keep_hit = [
        f"{keep} overlaps {boundary}"
        for keep in KEEP_PATHS
        for boundary in BOUNDARY_CODE
        if keep.startswith(boundary + "/") or boundary.startswith(keep + "/") or keep == boundary
    ]
    errors.extend(keep_hit)

    if landed:
        for rel, files in boundary_code.items():
            if files:
                errors.append(f"deletion landed but {rel} still has {len(files)} .py files")
        pending = sorted(oid for oid, status in statuses.items() if status != "ported")
        if pending:
            errors.append(f"deletion landed but oracles are not ported: {', '.join(pending)}")
        for keep in KEEP_PATHS:
            if not (root / keep).is_dir():
                errors.append(f"deletion landed but keep-path is missing: {keep}")
    else:
        if len(boundary_code["scripts/v3/harness"]) < floors["harness_code_files"]:
            errors.append(
                f"harness code files {len(boundary_code['scripts/v3/harness'])} < "
                f"{floors['harness_code_files']}: deletion is not allowed before the Nov 10 rehearsal"
            )
        if len(boundary_code["scripts/v3/planning"]) < floors["planning_code_files"]:
            errors.append(
                f"planning code files {len(boundary_code['scripts/v3/planning'])} < "
                f"{floors['planning_code_files']}: deletion is not allowed before the Nov 10 rehearsal"
            )
        legacy_tests = [p for p in disk if _is_test_name(Path(p).name)]
        for path in legacy_tests:
            if path not in run_paths:
                errors.append(f"{path}: not run by tools/ci_gate.py")

    counts = {
        "rows": len(rows),
        "legacy_test_files": len([p for p in disk if _is_test_name(Path(p).name)]),
        "support_modules": len([p for p in SUPPORT_MODULES if p in disk]),
        "planning_fixtures": len(
            [p for p in disk if fnmatch.fnmatchcase(p, "scripts/tests/v3/planning/fixtures/F-M1A-*.json")]
        ),
        "harness_stream_fixtures": len([p for p in HARNESS_STREAM_FIXTURES if p in disk]),
        "oracles": len(oracles),
        "port_oracles": len(port_oracles),
        "satisfied_oracles": len(satisfied),
    }
    for key, count in counts.items():
        if landed and key in {"legacy_test_files", "support_modules", "planning_fixtures", "harness_stream_fixtures"}:
            continue
        if count < floors[key]:
            errors.append(f"floor {key}: {count} < {floors[key]}")
    return errors


def load_ci_gate():
    spec = importlib.util.spec_from_file_location("x1_ci_gate", ROOT / "tools" / "ci_gate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gate_run_paths() -> set[str]:
    gate = load_ci_gate()
    run = {path for path in gate.glob_suite_tests(ROOT) if path not in gate.TEST_EXCLUDE}
    for program in gate.contract_programs(sorted(run)):
        run.add(program[0])
    return run


class InventoryFloorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = (ROOT / DOC).read_text(encoding="utf-8")
        cls.run_paths = gate_run_paths()

    def test_repository_matches_the_inventory(self) -> None:
        errors = collect_errors(
            ROOT, self.doc, self.run_paths, landed=X1_DELETION_LANDED, floors=FLOORS
        )
        self.assertEqual(errors, [], "\n".join(errors))

    def test_this_floor_is_run_by_the_gate(self) -> None:
        self.assertIn("scripts/tests/v3/x1/inventory.test.py", self.run_paths)

    def test_oracle_status_report(self) -> None:
        oracles = parse_oracles(self.doc)
        statuses = {o.oracle: oracle_status(ROOT, o, self.run_paths) for o in oracles}
        print(
            "X1_ORACLES "
            + " ".join(f"{oid}={status}" for oid, status in sorted(statuses.items())),
            flush=True,
        )
        self.assertEqual(len(statuses), len(oracles))

    def test_summarized_test_names_exist(self) -> None:
        section = self.doc.split("## 6. Mixed files", 1)[1].split("## 7.", 1)[0]
        current = None
        for line in section.splitlines():
            head = re.match(r"`(\w+\.test\.py)`:", line)
            if head:
                current = ROOT / "scripts/tests/v3/harness" / head.group(1)
            if current is None:
                continue
            source = current.read_text(encoding="utf-8")
            for name in re.findall(r"`(test_\w+)`", line):
                self.assertIn(f"def {name}(", source, f"{name} not in {current.name}")


class MutationTests(unittest.TestCase):
    """The floor must turn red on the edits it exists to catch."""

    DOC_TEXT = "\n".join(
        [
            "<!-- x1-disposition-table -->",
            "| Row | Path or glob | Files | Disposition | Oracle | Reason |",
            "| --- | --- | --- | --- | --- | --- |",
            "| A | `scripts/tests/v3/planning/a.test.py` | 1 | port | O-P1 | reason |",
            "| B | `scripts/tests/v3/planning/b.test.py` | 1 | drop | none | reason |",
            "",
            "<!-- x1-oracle-register -->",
            "| Oracle | Contract | Legacy carrier | Successor |",
            "| --- | --- | --- | --- |",
            "| O-P1 | c | `scripts/tests/v3/planning/a.test.py` | `scripts/tests/v3/planning/a.test.py` |",
            "",
        ]
    )
    ZERO_FLOORS = {key: 0 for key in FLOORS}

    def make_tree(self, *, a_body: str = "x = 1\n", extra: dict[str, str] | None = None) -> Path:
        root = Path(tempfile.mkdtemp(prefix="x1-floor-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        files = {
            "scripts/tests/v3/planning/a.test.py": a_body,
            "scripts/tests/v3/planning/b.test.py": "x = 1\n",
            "scripts/v3/harness/__init__.py": "",
            "scripts/v3/planning/__init__.py": "",
            **(extra or {}),
        }
        for rel, body in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        return root

    def errors(self, root: Path, *, landed: bool = False, run: set[str] | None = None) -> list[str]:
        run_paths = run if run is not None else {
            "scripts/tests/v3/planning/a.test.py",
            "scripts/tests/v3/planning/b.test.py",
        }
        return collect_errors(root, self.DOC_TEXT, run_paths, landed=landed, floors=self.ZERO_FLOORS)

    def test_clean_tree_is_green(self) -> None:
        self.assertEqual(self.errors(self.make_tree()), [])

    def test_unlisted_file_is_red(self) -> None:
        root = self.make_tree(extra={"scripts/tests/v3/harness/new.test.py": "x = 1\n"})
        self.assertTrue(any("new.test.py" in e for e in self.errors(root)))

    def test_deleted_legacy_test_is_red(self) -> None:
        root = self.make_tree()
        (root / "scripts/tests/v3/planning/b.test.py").unlink()
        self.assertTrue(any(e.startswith("B:") for e in self.errors(root)))

    def test_floor_drop_is_red(self) -> None:
        root = self.make_tree()
        floors = {**self.ZERO_FLOORS, "rows": 3}
        errors = collect_errors(
            root, self.DOC_TEXT, {"scripts/tests/v3/planning/a.test.py", "scripts/tests/v3/planning/b.test.py"},
            landed=False, floors=floors,
        )
        self.assertTrue(any("floor rows" in e for e in errors))

    def test_early_boundary_deletion_is_red(self) -> None:
        root = self.make_tree()
        floors = {**self.ZERO_FLOORS, "harness_code_files": 2}
        errors = collect_errors(
            root, self.DOC_TEXT, {"scripts/tests/v3/planning/a.test.py", "scripts/tests/v3/planning/b.test.py"},
            landed=False, floors=floors,
        )
        self.assertTrue(any("before the Nov 10 rehearsal" in e for e in errors))

    def test_test_not_run_by_gate_is_red(self) -> None:
        root = self.make_tree()
        errors = self.errors(root, run={"scripts/tests/v3/planning/a.test.py"})
        self.assertTrue(any("not run by tools/ci_gate.py" in e for e in errors))

    def test_landed_with_boundary_and_pending_oracle_is_red(self) -> None:
        root = self.make_tree(a_body="from scripts.v3.planning.replay.kernel import genesis\n")
        errors = self.errors(root, landed=True)
        self.assertTrue(any("still has" in e for e in errors))
        self.assertTrue(any("oracles are not ported" in e for e in errors))

    def test_landed_green_once_ported_and_boundary_gone(self) -> None:
        root = self.make_tree(extra={"scripts/v3/route_adapter/__init__.py": ""})
        for rel in ("scripts/v3/harness/__init__.py", "scripts/v3/planning/__init__.py"):
            (root / rel).unlink()
        (root / "scripts/tests/v3/planning/b.test.py").unlink()
        self.assertEqual(self.errors(root, landed=True), [])

    def test_boundary_reference_detection(self) -> None:
        self.assertTrue(BOUNDARY_REFERENCE.search("from scripts.v3.harness.cli import main"))
        self.assertTrue(BOUNDARY_REFERENCE.search('ROOT / "scripts" / "v3" / "planning" / "x.py"'))
        self.assertTrue(BOUNDARY_REFERENCE.search("scripts/v3/planning/replay/kernel.py"))
        self.assertFalse(BOUNDARY_REFERENCE.search("scripts.v3.route_adapter.fake"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
