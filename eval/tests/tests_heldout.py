"""The held-out guard: denial for dev and optimizer paths, no path in the repository, one caller."""
from __future__ import annotations

import ast
import json
import os
import unittest
from pathlib import Path

from eval import REPO_ROOT, heldout, runner
from eval import suite as suite_mod
from eval.tests import make_suite, outside_folder, scratch_bases, tiny_task

SKIP_DIRS = {".git", "__pycache__", ".ruff_cache", "node_modules", ".venv", "venv"}


def python_files(root: Path):
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if name.endswith(".py") or (Path(dirpath).name == "eval" and name == "run"):
                yield Path(dirpath) / name


def names_used(path: Path) -> set:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return set()
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.alias):
            used.add(node.name.rsplit(".", 1)[-1])
    return used


class GuardTests(unittest.TestCase):
    def setUp(self):
        heldout._declared = None

    def tearDown(self):
        heldout._declared = None

    def held_out_suite(self, folder: Path, tmp: Path, xdg: Path) -> Path:
        make_suite(folder, {"h-1": tiny_task("h-1", ["solve"])}, split="held_out", suite_id="pilot-t",
                   tmp_base=tmp, xdg_base=xdg)
        return folder

    def test_dev_and_optimize_purposes_are_denied(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            self.held_out_suite(folder, tmp, xdg)
            for purpose in ("dev", "optimize", "tune", ""):
                with self.subTest(purpose):
                    with self.assertRaises(heldout.HeldOutDenied):
                        heldout.open_suite(folder, purpose=purpose)
            self.assertEqual(heldout.open_suite(folder, purpose="score").split, "held_out")

    def test_a_process_declared_dev_or_optimize_can_never_open_it(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            self.held_out_suite(folder, tmp, xdg)
            for mode in ("dev", "optimize"):
                with self.subTest(mode):
                    heldout._declared = None
                    heldout.declare(mode)
                    with self.assertRaises(heldout.HeldOutDenied):
                        heldout.open_suite(folder, purpose="score")
                    with self.assertRaises(heldout.HeldOutDenied):
                        heldout.declare("score")

    def test_the_repository_is_never_a_held_out_source(self):
        for path in (REPO_ROOT, REPO_ROOT / "eval" / "devset", REPO_ROOT / "core"):
            with self.subTest(str(path)):
                with self.assertRaises(heldout.HeldOutDenied):
                    heldout.open_suite(path, purpose="score")

    def test_a_symlink_into_the_repository_is_refused(self):
        with outside_folder() as folder:
            link = folder / "looks-outside"
            link.symlink_to(REPO_ROOT / "eval" / "devset")
            with self.assertRaises(heldout.HeldOutDenied):
                heldout.open_suite(link, purpose="score")

    def test_relative_and_missing_paths_are_refused(self):
        with self.assertRaises(heldout.HeldOutDenied):
            heldout.open_suite("eval/devset", purpose="score")
        with self.assertRaises(heldout.HeldOutDenied):
            heldout.open_suite("/nonexistent/eval-held-out", purpose="score")

    def test_the_dev_loader_refuses_a_held_out_manifest(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            self.held_out_suite(folder, tmp, xdg)
            with self.assertRaises(suite_mod.HeldOutRefused):
                suite_mod.load_dev(folder / "suite.json")

    def test_the_guard_opens_held_out_suites_only(self):
        with self.assertRaises(suite_mod.SuiteError):
            suite_mod._load_held_out(runner.DEV_SUITE)

    def test_the_runner_takes_held_out_tasks_only_from_a_path_given_at_run_time(self):
        os.environ.pop(runner.TASKS_ENV, None)
        with self.assertRaises(runner.RunRefused):
            runner.load_suite("pilot-t", None)
        with scratch_bases() as (tmp, xdg), outside_folder() as folder, outside_folder() as out:
            self.held_out_suite(folder, tmp, xdg)
            heldout._declared = None
            code = runner.main(["--suite", "pilot-t", "--tasks", str(folder), "--arms", "direct", "--k", "1",
                                "--out", str(out), "--tmp-base", str(tmp), "--xdg-base", str(xdg)])
            self.assertEqual(code, 0)
            records = list((out / "pilot-t").glob("*.json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(record["label"], "shakedown")   # the stand-in on a held-out suite is never a measurement
            self.assertEqual(record["suite"]["split"], "held_out")
            # The same process, now a dev process, is refused the held-out suite.
            heldout._declared = None
            self.assertEqual(runner.main(["--suite", "dev", "--arms", "recursion", "--k", "1"]), 2)
            self.assertEqual(heldout.declared(), "dev")
            code = runner.main(["--suite", "pilot-t", "--tasks", str(folder), "--arms", "direct", "--k", "1",
                                "--out", str(out), "--tmp-base", str(tmp), "--xdg-base", str(xdg)])
            self.assertEqual(code, 2)

    def test_artifacts_of_a_held_out_run_stay_out_of_the_repository(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder, outside_folder() as out:
            self.held_out_suite(folder, tmp, xdg)
            code = runner.main(["--suite", "pilot-t", "--tasks", str(folder), "--arms", "direct", "--k", "1",
                                "--out", str(out), "--artifacts", str(REPO_ROOT / "eval" / "artifacts"),
                                "--tmp-base", str(tmp), "--xdg-base", str(xdg)])
            self.assertEqual(code, 2)
            self.assertFalse((REPO_ROOT / "eval" / "artifacts").exists())


class OneCallerTests(unittest.TestCase):
    """Across every Python file in the repository: who may reach the held-out loaders."""

    @classmethod
    def setUpClass(cls):
        cls.uses = {path.relative_to(REPO_ROOT).as_posix(): names_used(path) for path in python_files(REPO_ROOT)}

    def callers(self, name):
        return sorted(p for p, used in self.uses.items() if name in used and not p.startswith("eval/tests/"))

    # A definition is not a reference: ``def open_suite`` in heldout.py is not counted as a caller.
    def test_only_the_scoring_runner_opens_held_out_suites(self):
        self.assertEqual(self.callers("open_suite"), ["eval/runner.py"])

    def test_only_the_guard_reaches_the_suite_modules_held_out_loader(self):
        self.assertEqual(self.callers("_load_held_out"), ["eval/heldout.py"])

    def test_the_scan_sees_the_files_it_must_see(self):
        self.assertIn("eval/runner.py", self.uses)
        self.assertGreater(len(self.uses), 50)


if __name__ == "__main__":
    unittest.main()
