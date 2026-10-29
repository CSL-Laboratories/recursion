#!/usr/bin/env python3
"""X1 inventory floor for the Recursion legacy deletion boundary (CSL-495, WP14).

Reads docs/v3/x1/RECURSION-X1-INVENTORY.md and fails when the disposition table, the oracle
register or the pinned counts drift from the tree. It runs inside the existing `linux`,
`windows` and `macos` jobs through tools/ci_gate.py discovery; no job name or workflow changes.

Deletion is gated: while X1_DELETION_LANDED is False the boundary must still exist in full.
The deletion PR (after the Nov 10 rehearsal) flips the flag; the test then stays red until every
port oracle has a successor carrier that exists, is run by the gate and no longer references the
boundary.

CSL-578 (Oct 29): Lucas had the legacy CLI harness deleted ahead of the rehearsal and dropped its
five oracles (O-H1 to O-H5) without a port. The boundary is now `scripts/v3/planning/**` only.
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

BOUNDARY_CODE = ("scripts/v3/planning",)
LEGACY_TEST_DIRS = ("scripts/tests/v3/planning",)
KEEP_PATHS = ("scripts/v3/route_adapter",)
# CSL-495 comment 611d6aa4: "the receipt tests" are these files. Outside the boundary; kept.
RECEIPT_TESTS = (
    "tools/test_claude_fixer_guard.py",
    "tools/test_claude_fixer_publish.py",
    "tools/test_claude_fixer_receipts.py",
)

DISPOSITIONS = {"port", "drop", "keep"}

# Pinned floors (M1_MIN_PROGRAMS style). Raise when the inventory grows; lowering one is a
# reviewed decision, not a drive-by edit.
FLOORS = {
    "rows": 12,
    "legacy_test_files": 8,
    "support_modules": 2,
    "planning_fixtures": 23,
    "oracles": 7,
    "port_oracles": 5,
    "satisfied_oracles": 2,
}

# The exact boundary code set at the pre-x1 tag (base 2f1fc17e), less the harness that CSL-578
# deleted. While X1_DELETION_LANDED is False the tree must match it exactly: a count cannot tell
# a removed module from an added one.
PINNED_BOUNDARY_FILES = (
    "scripts/v3/planning/replay/kernel.py",
    "scripts/v3/planning/validation/canonical.py",
    "scripts/v3/planning/validation/contracts.py",
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
    tests: tuple[str, ...]


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
        if len(cells) != 5:
            raise AssertionError(f"oracle row needs 5 cells: {cells}")
        name, _contract, legacy, successor, tests = cells
        found = PATH_CELL.findall(successor)
        oracles.append(
            Oracle(
                name,
                tuple(PATH_CELL.findall(legacy)),
                found[0] if found else None,
                tuple(PATH_CELL.findall(tests)),
            )
        )
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
    if not oracle.tests:
        return "pending"
    source = (root / oracle.successor).read_text(encoding="utf-8")
    for name in oracle.tests:
        if not re.search(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b", source, re.MULTILINE):
            return "pending"
    return "ported"


def collect_errors(
    root: Path,
    doc_text: str,
    run_paths: set[str],
    *,
    landed: bool,
    floors: dict[str, int],
    pinned: tuple[str, ...] = PINNED_BOUNDARY_FILES,
    receipt_tests: tuple[str, ...] = RECEIPT_TESTS,
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
    for rel in receipt_tests:
        if any(rel == boundary or rel.startswith(boundary + "/") for boundary in BOUNDARY_CODE):
            errors.append(f"{rel}: receipt test overlaps the deletion boundary")
        if not (root / rel).is_file():
            errors.append(f"{rel}: receipt test is a keep-path and is missing")

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
        current_code = {p for files in boundary_code.values() for p in files}
        for path in sorted(set(pinned) - current_code):
            errors.append(f"{path}: pinned boundary file is missing; deletion is not allowed before the Nov 10 rehearsal")
        for path in sorted(current_code - set(pinned)):
            errors.append(f"{path}: boundary file is not in PINNED_BOUNDARY_FILES; add it deliberately")
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
        "oracles": len(oracles),
        "port_oracles": len(port_oracles),
        "satisfied_oracles": len(satisfied),
    }
    for key, count in counts.items():
        if landed and key in {"legacy_test_files", "support_modules", "planning_fixtures"}:
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

    def test_receipt_tests_pin_matches_the_fixer_files(self) -> None:
        found = sorted(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "tools").glob("test_claude_fixer_*")
            if path.is_file()
        )
        self.assertEqual(found, sorted(RECEIPT_TESTS))
        for rel in RECEIPT_TESTS:
            self.assertTrue((ROOT / rel).is_file(), rel)
            self.assertFalse(any(rel.startswith(boundary + "/") for boundary in BOUNDARY_CODE))

    def test_pinned_boundary_matches_the_tree_before_deletion(self) -> None:
        if X1_DELETION_LANDED:
            self.skipTest("deletion landed; the pin applies only before deletion")
        current = {
            path
            for rel in BOUNDARY_CODE
            for path in _files_under(ROOT, rel)
            if path.endswith(".py")
        }
        self.assertEqual(sorted(current), sorted(PINNED_BOUNDARY_FILES))


class MutationTests(unittest.TestCase):
    """The floor must turn red on the edits it exists to catch."""

    A = "scripts/tests/v3/planning/a.test.py"
    B = "scripts/tests/v3/planning/b.test.py"
    H = "scripts/tests/v3/planning/c.test.py"
    PIN = ("scripts/v3/planning/__init__.py", "scripts/v3/planning/replay/__init__.py")

    DOC_TEXT = "\n".join(
        [
            "<!-- x1-disposition-table -->",
            "| Row | Path or glob | Files | Disposition | Oracle | Reason |",
            "| --- | --- | --- | --- | --- | --- |",
            "| A | `scripts/tests/v3/planning/a.test.py` | 1 | port | O-P1 | reason |",
            "| B | `scripts/tests/v3/planning/b.test.py` | 1 | drop | none | reason |",
            "| C | `scripts/tests/v3/planning/c.test.py` | 1 | drop | none | reason |",
            "",
            "<!-- x1-oracle-register -->",
            "| Oracle | Contract | Legacy carrier | Successor | Successor tests |",
            "| --- | --- | --- | --- | --- |",
            "| O-P1 | c | `scripts/tests/v3/planning/a.test.py` | `scripts/tests/v3/planning/a.test.py` | `run_a` |",
            "",
            "## 7. Floors",
            "",
        ]
    )
    ZERO_FLOORS = {key: 0 for key in FLOORS}

    def make_tree(
        self,
        *,
        a_body: str = "def run_a():\n    return 1\n",
        extra: dict[str, str] | None = None,
    ) -> Path:
        root = Path(tempfile.mkdtemp(prefix="x1-floor-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        files = {
            self.A: a_body,
            self.B: "x = 1\n",
            self.H: "def test_alpha(self):\n    pass\n",
            "scripts/v3/planning/__init__.py": "",
            "scripts/v3/planning/replay/__init__.py": "",
            **(extra or {}),
        }
        for rel, body in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        return root

    def errors(
        self,
        root: Path,
        *,
        landed: bool = False,
        run: set[str] | None = None,
        floors: dict[str, int] | None = None,
        pinned: tuple[str, ...] | None = None,
        receipt_tests: tuple[str, ...] = (),
    ) -> list[str]:
        run_paths = run if run is not None else {self.A, self.B, self.H}
        return collect_errors(
            root,
            self.DOC_TEXT,
            run_paths,
            landed=landed,
            floors=floors or self.ZERO_FLOORS,
            pinned=self.PIN if pinned is None else pinned,
            receipt_tests=receipt_tests,
        )

    def test_clean_tree_is_green(self) -> None:
        self.assertEqual(self.errors(self.make_tree()), [])

    def test_missing_receipt_test_is_red(self) -> None:
        errors = self.errors(self.make_tree(), receipt_tests=("tools/test_claude_fixer_receipts.py",))
        self.assertTrue(any("receipt test is a keep-path and is missing" in e for e in errors), errors)

    def test_unlisted_file_is_red(self) -> None:
        root = self.make_tree(extra={"scripts/tests/v3/planning/new.test.py": "x = 1\n"})
        self.assertTrue(any("new.test.py" in e for e in self.errors(root)))

    def test_deleted_legacy_test_is_red(self) -> None:
        root = self.make_tree()
        (root / self.B).unlink()
        self.assertTrue(any(e.startswith("B:") for e in self.errors(root)))

    def test_floor_drop_is_red(self) -> None:
        root = self.make_tree()
        errors = self.errors(root, floors={**self.ZERO_FLOORS, "rows": 4})
        self.assertTrue(any("floor rows" in e for e in errors))

    def test_removed_boundary_module_is_red_even_if_another_is_added(self) -> None:
        root = self.make_tree(extra={"scripts/v3/planning/new.py": ""})
        (root / "scripts/v3/planning/replay/__init__.py").unlink()
        errors = self.errors(root)
        self.assertTrue(any("pinned boundary file is missing" in e for e in errors), errors)
        self.assertTrue(any("not in PINNED_BOUNDARY_FILES" in e for e in errors), errors)

    def test_unpinned_boundary_addition_is_red(self) -> None:
        root = self.make_tree(extra={"scripts/v3/planning/extra.py": ""})
        self.assertTrue(any("scripts/v3/planning/extra.py" in e for e in self.errors(root)))

    def test_test_not_run_by_gate_is_red(self) -> None:
        errors = self.errors(self.make_tree(), run={self.A, self.B})
        self.assertTrue(any("not run by tools/ci_gate.py" in e for e in errors))

    def test_landed_with_boundary_and_pending_oracle_is_red(self) -> None:
        root = self.make_tree(a_body="from scripts.v3.planning.replay.kernel import genesis\n")
        errors = self.errors(root, landed=True)
        self.assertTrue(any("still has" in e for e in errors))
        self.assertTrue(any("oracles are not ported" in e for e in errors))

    def test_landed_without_legacy_files_is_green_once_ported(self) -> None:
        root = self.make_tree(extra={"scripts/v3/route_adapter/__init__.py": ""})
        for rel in (*self.PIN, self.B, self.H):
            (root / rel).unlink()
        self.assertEqual(self.errors(root, landed=True), [])

    def test_successor_without_named_test_is_not_ported(self) -> None:
        root = self.make_tree(a_body="def other():\n    return 1\n")
        oracle = parse_oracles(self.DOC_TEXT)[0]
        self.assertEqual(oracle_status(root, oracle, {self.A}), "pending")
        landed = self.errors(root, landed=True)
        self.assertTrue(any("oracles are not ported" in e for e in landed))

    def test_boundary_reference_detection(self) -> None:
        self.assertTrue(BOUNDARY_REFERENCE.search("from scripts.v3.harness.cli import main"))
        self.assertTrue(BOUNDARY_REFERENCE.search('ROOT / "scripts" / "v3" / "planning" / "x.py"'))
        self.assertTrue(BOUNDARY_REFERENCE.search("scripts/v3/planning/replay/kernel.py"))
        self.assertFalse(BOUNDARY_REFERENCE.search("scripts.v3.route_adapter.fake"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
