"""The dev set: 12 pinned tasks that validate, stored so the repository's gates stay green."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest

from eval import REPO_ROOT, dev, runner
from eval import suite as suite_mod
from eval.tests import scratch_bases


def load_gate():
    spec = importlib.util.spec_from_file_location("eval_view_of_ci_gate", REPO_ROOT / "tools" / "ci_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DevSetTests(unittest.TestCase):
    def test_twelve_pinned_tasks(self):
        suite = suite_mod.load_dev(runner.DEV_SUITE)
        self.assertEqual(len(suite.tasks), 12)
        self.assertEqual(len({t.id for t in suite.tasks}), 12)
        self.assertEqual(suite.split, "dev")
        for task in suite.tasks:
            self.assertTrue(task.hidden)
            self.assertTrue(task.solution)
            self.assertTrue(task.prompt.strip())

    def test_every_task_fails_on_its_start_and_passes_with_its_solution(self):
        with scratch_bases() as (tmp, xdg):
            problems = dev.check(tmp, xdg, log=lambda _m: None)
        self.assertEqual(problems, [])

    def test_a_changed_task_file_no_longer_matches_its_pin(self):
        import json
        import shutil
        from eval.tests import outside_folder
        with outside_folder() as folder:
            shutil.copytree(runner.DEV_SUITE.parent, folder / "devset")
            path = folder / "devset" / "tasks" / "dev-01-slugify.txtar"
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaises(suite_mod.SuiteError):
                suite_mod.load_dev(folder / "devset" / "suite.json")
            json.loads((folder / "devset" / "suite.json").read_text(encoding="utf-8"))


class RepositoryGateTests(unittest.TestCase):
    """eval/ must not break the repository's own frozen checks."""

    def test_no_test_file_outside_the_gate_globs(self):
        gate = load_gate()
        run = gate.assert_tests_accounted(REPO_ROOT)
        self.assertFalse([p for p in run if p.startswith("eval/")])

    def test_public_scope_check_passes(self):
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "tools/check_public_scope.py"], cwd=REPO_ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace")[-2000:])


if __name__ == "__main__":
    unittest.main()
