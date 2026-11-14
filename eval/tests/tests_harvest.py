"""The harvest: a task is kept only if its tests fail on the parent and pass on the commit."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

from eval import REPO_ROOT, arms, harvest, heldout, runner
from eval import suite as suite_mod
from eval.tests import outside_folder, scratch_bases

TEST_COMMAND = ["{python}", "-B", "-m", "unittest", "{hidden_modules}"]


class Repo:
    """A throwaway git repository with dated commits, made apart from the operator's git settings."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "--template=", "-b", "main")

    def git(self, *args, date="2026-01-01T00:00:00+0000"):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_NOSYSTEM": "1", "HOME": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid", "GIT_AUTHOR_DATE": date,
               "GIT_COMMITTER_DATE": date}
        return subprocess.run(["git", *args], cwd=self.path, env=env, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout.decode().strip()

    def write(self, files: dict):
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def commit(self, files: dict, message: str, date: str) -> str:
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, date=date)
        return self.git("rev-parse", "HEAD")


OPS = "def add(a, b):\n    return a + b\n"
OPS_MUL = OPS + "\n\ndef mul(a, b):\n    return a * b\n"
TEST_ADD = ("import unittest\n\nfrom calc.ops import add\n\n\nclass AddTests(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")


def unit_test(name, body):
    return f"import unittest\n\nfrom calc import ops\n\n\nclass T(unittest.TestCase):\n    def test_{name}(self):\n        {body}\n"


def build_history(path: Path) -> dict:
    repo = Repo(path)
    shas = {}
    shas["c0"] = repo.commit({"calc/__init__.py": "", "calc/ops.py": OPS, "tests/__init__.py": "",
                              "tests/test_ops.py": TEST_ADD}, "initial", "2026-01-01T00:00:00+0000")
    shas["early"] = repo.commit({"calc/ops.py": OPS + "\n\ndef neg(a):\n    return -a\n",
                                 "tests/test_neg.py": unit_test("neg", "self.assertEqual(ops.neg(2), -2)")},
                                "Add neg (before the not-before date)", "2026-01-15T00:00:00+0000")
    current = OPS + "\n\ndef neg(a):\n    return -a\n"
    current_mul = current + "\n\ndef mul(a, b):\n    return a * b\n"
    shas["mul"] = repo.commit({"calc/ops.py": current_mul,
                               "tests/test_mul.py": unit_test("mul", "self.assertEqual(ops.mul(3, 4), 12)")},
                              "Add mul\n\nMultiply two numbers.\n\nSigned-off-by: Someone <s@invalid>",
                              "2026-03-01T00:00:00+0000")
    shas["tests_only"] = repo.commit({"tests/test_ops.py": TEST_ADD + "\n    def test_add_zero(self):\n"
                                                                      "        self.assertEqual(add(0, 0), 0)\n"},
                                     "More add tests", "2026-03-02T00:00:00+0000")
    shas["already_passes"] = repo.commit({"calc/ops.py": '"""Arithmetic."""\n' + current_mul,
                                          "tests/test_neg.py": unit_test("neg", "self.assertEqual(ops.neg(-2), 2)")},
                                         "Docstring and a neg test", "2026-03-03T00:00:00+0000")
    current_mul = '"""Arithmetic."""\n' + current_mul
    shas["fails_on_commit"] = repo.commit({"calc/ops.py": current_mul + "\n\ndef div(a, b):\n    return a / b\n",
                                           "tests/test_div.py": unit_test("div", "self.assertEqual(ops.div(1, 0), 0)")},
                                          "Add div", "2026-03-04T00:00:00+0000")
    current_div = current_mul + "\n\ndef div(a, b):\n    return a / b\n"
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit({"calc/ops.py": current_div + "\n\ndef sub(a, b):\n    return a - b\n",
                 "tests/test_sub.py": unit_test("sub", "self.assertEqual(ops.sub(5, 3), 2)")},
                "Add sub", "2026-03-05T00:00:00+0000")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "feature", "-m", "Merge the sub feature", date="2026-03-06T00:00:00+0000")
    shas["merge"] = repo.git("rev-parse", "HEAD")
    return shas


class HarvestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._bases = scratch_bases()
        cls.tmp, cls.xdg = cls._bases.__enter__()
        cls._outside = outside_folder()
        cls.folder = cls._outside.__enter__()
        cls.shas = build_history(cls.folder / "upstream")
        cls.repos = cls.folder / "repos.json"
        cls.repos.write_text(json.dumps({"schema": harvest.REPOS_SCHEMA, "repos": [
            {"name": "calc", "path": str(cls.folder / "upstream"), "branch": "main", "test_globs": ["tests/**/test_*.py"],
             "test_command": TEST_COMMAND, "timeout_s": 600, "grading_timeout_s": 120}]}), encoding="utf-8")
        cls.out = cls.folder / "held-out"
        cls.logs = []
        cls.report = harvest.harvest(cls.repos, "2026-02-01", cls.out, suite_id="pilot-h", tmp_base=cls.tmp,
                                     xdg_base=cls.xdg, log=cls.logs.append)

    @classmethod
    def tearDownClass(cls):
        cls._outside.__exit__(None, None, None)
        cls._bases.__exit__(None, None, None)

    def setUp(self):
        heldout._declared = None

    def tearDown(self):
        heldout._declared = None

    def test_kept_only_fail_on_parent_pass_on_commit(self):
        manifest = json.loads((self.out / "suite.json").read_text(encoding="utf-8"))
        kept = {t["commit"] for t in manifest["tasks"]}
        self.assertEqual(kept, {self.shas["mul"], self.shas["merge"]})
        reasons = {r["commit"]: r["reason"] for r in self.report["rejected"]}
        self.assertEqual(reasons[self.shas["tests_only"]], "changes only tests")
        self.assertIn("pass on the parent", reasons[self.shas["already_passes"]])
        self.assertIn("fail on the commit", reasons[self.shas["fails_on_commit"]])
        self.assertNotIn(self.shas["early"], reasons)
        self.assertNotIn(self.shas["early"], kept)

    def test_task_contents(self):
        suite = heldout.open_suite(self.out, purpose="score")
        mul = next(t for t in suite.tasks if t.commit == self.shas["mul"])
        self.assertEqual(mul.prompt, "Add mul\n\nMultiply two numbers.\n")
        self.assertEqual(sorted(mul.hidden), ["tests/test_mul.py"])
        self.assertEqual(sorted(mul.solution), ["calc/ops.py"])
        self.assertIn(b"def mul", mul.solution["calc/ops.py"])
        self.assertEqual(mul.cluster, "calc")
        self.assertTrue(mul.created_at.startswith("2026-03-01"))
        merge = next(t for t in suite.tasks if t.commit == self.shas["merge"])
        self.assertEqual(sorted(merge.hidden), ["tests/test_sub.py"])
        self.assertEqual(merge.prompt, "Merge the sub feature\n")

    def test_the_start_tree_holds_no_solution_and_no_hidden_test(self):
        suite = heldout.open_suite(self.out, purpose="score")
        mul = next(t for t in suite.tasks if t.commit == self.shas["mul"])
        work = self.folder / "inspect-start"
        work.mkdir()
        mul.materialize_start(work)
        self.assertNotIn("def mul", (work / "calc" / "ops.py").read_text(encoding="utf-8"))
        self.assertFalse((work / "tests" / "test_mul.py").exists())
        self.assertFalse((work / ".git").exists())

    def test_harvested_suite_runs_through_the_runner(self):
        suite = heldout.open_suite(self.out, purpose="score")
        outcome = runner.run(suite, [arms.DirectArm("standin")], k=1, label="pilot", out_dir=self.folder / "records",
                             tmp_base=self.tmp, xdg_base=self.xdg, log=lambda _m: None)
        record = json.loads(Path(outcome.records["direct"]).read_text(encoding="utf-8"))
        self.assertEqual({t["grade"]["verdict"] for t in record["trials"]}, {"resolved"})
        self.assertEqual({t["cluster"] for t in record["plan"]["tasks"]}, {"calc"})

    def test_report_records_the_hashes(self):
        _, repos_sha = harvest.load_repos(self.repos)
        self.assertEqual(self.report["repos_sha256"], repos_sha)
        self.assertEqual(self.report["harvest_sha256"], harvest.harvest_sha256())
        manifest = json.loads((self.out / "suite.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["harvest"]["harvest_sha256"], harvest.harvest_sha256())
        self.assertEqual(manifest["split"], "held_out")
        self.assertFalse((self.out / ".staging").exists())

    def test_output_inside_the_repository_is_refused(self):
        with self.assertRaises(heldout.HeldOutDenied):
            harvest.harvest(self.repos, "2026-02-01", REPO_ROOT / "eval" / "held-out", suite_id="x",
                            tmp_base=self.tmp, xdg_base=self.xdg, log=lambda _m: None)
        self.assertFalse((REPO_ROOT / "eval" / "held-out").exists())

    def test_output_folder_must_be_new(self):
        with self.assertRaises(harvest.HarvestError):
            harvest.harvest(self.repos, "2026-02-01", self.out, suite_id="x", tmp_base=self.tmp, xdg_base=self.xdg,
                            log=lambda _m: None)

    def test_dev_loader_refuses_the_harvested_suite(self):
        with self.assertRaises(suite_mod.HeldOutRefused):
            suite_mod.load_dev(self.out / "suite.json")


class HarvestPieceTests(unittest.TestCase):
    def test_glob(self):
        rx = harvest.glob_regex("tests/**/test_*.py")
        self.assertTrue(rx.match("tests/test_a.py"))
        self.assertTrue(rx.match("tests/x/y/test_a.py"))
        self.assertFalse(rx.match("src/test_a.py"))
        self.assertFalse(rx.match("tests/x/helper.py"))

    def test_prompt_drops_only_a_trailer_block(self):
        self.assertEqual(harvest.prompt_from_message("Fix x\n\nBody: kept\nmore\n\nSigned-off-by: a <a@b>\n"),
                         "Fix x\n\nBody: kept\nmore\n")
        self.assertEqual(harvest.prompt_from_message("Fix: y\n"), "Fix: y\n")

    def test_hashes_command_and_repository_list_hash(self):
        with outside_folder() as folder:
            path = folder / "repos.json"
            body = {"schema": harvest.REPOS_SCHEMA, "repos": [{"name": "a", "path": "/x", "branch": "main",
                                                               "test_globs": ["t/*.py"], "test_command": ["x"]}]}
            path.write_text(json.dumps(body), encoding="utf-8")
            _, first = harvest.load_repos(path)
            body["repos"][0]["branch"] = "dev"
            path.write_text(json.dumps(body), encoding="utf-8")
            _, second = harvest.load_repos(path)
            self.assertNotEqual(first, second)
            self.assertEqual(harvest.main(["hashes", "--repos", str(path)]), 0)


if __name__ == "__main__":
    unittest.main()
