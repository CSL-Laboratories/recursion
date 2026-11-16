"""Three more findings of the same round on #381 (Nov 15).

* Devin, eval/harvest.py line 114: ``rev-list --since`` limits the walk instead of filtering, so where
  committer dates run backward an eligible commit is left out of the harvest.
* Devin, eval/runner.py line 568: for a held-out suite on a real engine the default label was pilot,
  which check_plan refuses on a suite that is not frozen (a fresh harvest), so the default could not run.
* The GitHub Codex reviewer, eval/tasks.py line 160: a commit that replaces a file with a folder (deletes
  ``tool``, adds ``tool/main.py``) could not be laid out, because the additions came before the
  deletions; the error ended the whole harvest instead of being one candidate's rejection.

Every repository here is a made-up one in a scratch folder outside the repository; no engine is started
except the stand-in.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from eval import harvest, runner, standin_engine, tasks, validate
from eval.tests import outside_folder, scratch_bases
from eval.tests.tests_harvest import Repo

TEST_COMMAND = ["{python}", "-B", "-m", "unittest", "{hidden_modules}"]


def repo_cfg(path: Path) -> dict:
    return {"name": "calc", "path": str(path), "branch": "main", "test_globs": ["tests/**/test_*.py"],
            "test_command": TEST_COMMAND, "timeout_s": 600, "grading_timeout_s": 120}


class BackwardDatesTests(unittest.TestCase):
    def test_every_first_parent_commit_on_or_after_the_date_is_a_candidate(self):
        with outside_folder() as folder:
            repo = Repo(folder / "upstream")
            jan1 = repo.commit({"a.txt": "1\n"}, "one", "2026-01-01T00:00:00+0000")
            mar1 = repo.commit({"a.txt": "2\n"}, "two", "2026-03-01T00:00:00+0000")
            jan15 = repo.commit({"a.txt": "3\n"}, "three", "2026-01-15T00:00:00+0000")
            self.assertEqual(harvest.candidate_commits(folder / "upstream", "main", "2026-02-01"), [mar1])
            # The boundary: a commit at 00:00 UTC of the date is on it, and order stays oldest first.
            self.assertEqual(harvest.candidate_commits(folder / "upstream", "main", "2026-01-15"), [mar1, jan15])
            self.assertEqual(harvest.candidate_commits(folder / "upstream", "main", "2026-01-01"), [jan1, mar1, jan15])
            self.assertEqual(harvest.candidate_commits(folder / "upstream", "main", "2026-03-02"), [])


class DefaultLabelTests(unittest.TestCase):
    """The CLI's default label on a held-out suite with a real engine, checked by the real check_plan."""

    def main(self, frozen: bool) -> tuple:
        suite = SimpleNamespace(split="held_out", frozen=frozen, arms={})
        arm = SimpleNamespace(name="direct", engine="claude_code")
        seen = {}

        def fake_run(suite_, arms_, *, k, attempts=None, replacements=2, label="dev", seed=0, artifacts=None, **_):
            seen["label"] = label
            runner.check_plan(suite_, arms_, k=k, attempts=attempts if attempts is not None else k,
                              replacements=replacements, label=label, artifacts=artifacts, seed=seed)
            return runner.Outcome()

        with outside_folder() as folder, \
                mock.patch.object(runner, "load_suite", return_value=suite), \
                mock.patch.object(runner, "make_arms", return_value=[arm]), \
                mock.patch.object(runner, "run", side_effect=fake_run):
            code = runner.main(["--suite", "h", "--tasks", str(folder), "--arms", "direct", "--engine", "claude_code",
                                "--artifacts", str(folder / "artifacts")])
        return code, seen.get("label")

    def test_a_suite_not_frozen_defaults_to_a_label_check_plan_accepts(self):
        self.assertEqual(self.main(frozen=False), (0, "shakedown"))

    def test_a_frozen_suite_still_defaults_to_pilot(self):
        self.assertEqual(self.main(frozen=True), (0, "pilot"))

    def test_an_explicit_pilot_on_a_suite_not_frozen_is_still_refused(self):
        suite = SimpleNamespace(split="held_out", frozen=False, arms={})
        arm = SimpleNamespace(name="direct", engine="claude_code")
        with self.assertRaisesRegex(runner.RunRefused, "a pilot runs a frozen suite"):
            runner.check_plan(suite, [arm], k=5, attempts=5, replacements=2, label="pilot", artifacts=Path("/x"))


OLD_TOOL = "def main():\n    return 0\n"
TOOL_TEST = ("import unittest\n\nfrom tool.main import main\n\n\nclass T(unittest.TestCase):\n"
             "    def test_main(self):\n        self.assertEqual(main(), 7)\n")
FLAT_TEST = ("import unittest\nfrom pathlib import Path\n\n\nclass T(unittest.TestCase):\n"
             "    def test_flat(self):\n        self.assertEqual(Path('tool').read_text(), 'flat\\n')\n")


def transitions(path: Path) -> dict:
    """c1 replaces the file ``tool`` with a folder; c2 replaces the folder ``tool`` with a file again."""
    repo = Repo(path)
    shas = {"c0": repo.commit({"tests/__init__.py": "", "tool": OLD_TOOL}, "initial", "2026-01-01T00:00:00+0000")}
    (path / "tool").unlink()
    shas["to_dir"] = repo.commit({"tool/__init__.py": "", "tool/main.py": "def main():\n    return 7\n",
                                  "tests/test_tool.py": TOOL_TEST}, "Make tool a package", "2026-03-01T00:00:00+0000")
    for name in ("tool/__init__.py", "tool/main.py"):
        (path / name).unlink()
    (path / "tool").rmdir()
    shas["to_file"] = repo.commit({"tool": "flat\n", "tests/test_flat.py": FLAT_TEST}, "Make tool a file again",
                                  "2026-03-02T00:00:00+0000")
    return shas


class FileAndFolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._outside = outside_folder()
        cls.folder = cls._outside.__enter__()
        cls._bases = scratch_bases()
        cls.tmp, cls.xdg = cls._bases.__enter__()
        cls.upstream = cls.folder / "upstream"
        cls.shas = transitions(cls.upstream)

    @classmethod
    def tearDownClass(cls):
        cls._bases.__exit__(None, None, None)
        cls._outside.__exit__(None, None, None)

    def harvested(self, commit: str, name: str):
        candidate, reason = harvest.build_candidate(repo_cfg(self.upstream), commit, {})
        self.assertIsNone(reason)
        folder = self.folder / name
        harvest.write_task(folder, candidate)
        task = tasks.load_dir(folder)
        task.parent_tree = candidate["parent_tree"]
        return task

    def test_a_file_replaced_by_a_folder_validates(self):
        task = self.harvested(self.shas["to_dir"], "to-dir")
        self.assertIn("tool", task.solution)
        self.assertIsNone(task.solution["tool"])
        checked = validate.validate(task, reruns=1, tmp_base=self.tmp, xdg_base=self.xdg)
        self.assertEqual((checked.ok, checked.reason), (True, None))

    def test_a_folder_replaced_by_a_file_validates(self):
        task = self.harvested(self.shas["to_file"], "to-file")
        checked = validate.validate(task, reruns=1, tmp_base=self.tmp, xdg_base=self.xdg)
        self.assertEqual((checked.ok, checked.reason), (True, None))

    def test_apply_files_in_either_order(self):
        package = {"tool/__init__.py": b"", "tool/main.py": b"X = 1\n"}
        for name, files in (("additions-first", {**package, "tool": None}), ("deletions-first", {"tool": None, **package})):
            with self.subTest(name):
                work = self.folder / f"apply-dir-{name}"
                work.mkdir()
                (work / "tool").write_text("old\n", encoding="utf-8")
                tasks.apply_files(work, files)
                self.assertEqual((work / "tool" / "main.py").read_bytes(), b"X = 1\n")
        flat = {"tool": b"flat\n"}
        gone = {"tool/__init__.py": None, "tool/main.py": None}
        for name, files in (("additions-first", {**flat, **gone}), ("deletions-first", {**gone, **flat})):
            with self.subTest(name):
                work = self.folder / f"apply-file-{name}"
                (work / "tool").mkdir(parents=True)
                for part in ("__init__.py", "main.py"):
                    (work / "tool" / part).write_text("", encoding="utf-8")
                tasks.apply_files(work, files)
                self.assertEqual((work / "tool").read_bytes(), b"flat\n")

    def test_hidden_tests_land_inside_the_work_tree(self):
        # What an engine may leave where a hidden test's folder must be: a file, or a link out of the tree.
        outside = self.folder / "outside-target"
        outside.mkdir()
        task = tasks.Task(id="t", prompt="", test_command=["x"], hidden={"tests/test_a.py": b"A\n", "more/test_b.py": b"B\n"},
                          solution={}, sha256="0" * 64)
        work = self.folder / "hidden-way"
        work.mkdir()
        (work / "tests").write_text("a file\n", encoding="utf-8")
        os.symlink(outside, work / "more")
        task.write_hidden(work)
        self.assertEqual((work / "tests" / "test_a.py").read_bytes(), b"A\n")
        self.assertFalse((work / "more").is_symlink())
        self.assertEqual((work / "more" / "test_b.py").read_bytes(), b"B\n")
        self.assertEqual(list(outside.iterdir()), [])

    def test_nothing_reaches_through_a_link(self):
        # A link where a folder of the task's paths is: a deletion leaves what it points at alone, and a
        # write replaces the link with a real folder inside the tree (tasks.apply_files and the stand-in).
        for who in ("apply_files", "stand-in"):
            with self.subTest(who):
                outside = self.folder / f"outside-{who}"
                outside.mkdir()
                (outside / "x.py").write_text("keep\n", encoding="utf-8")
                work = self.folder / f"through-link-{who}"
                work.mkdir()
                os.symlink(outside, work / "pkg")
                os.symlink(outside, work / "lib")
                plan = {"pkg/x.py": None, "lib/new.py": "inside\n"}
                if who == "apply_files":
                    tasks.apply_files(work, plan)
                else:
                    before = os.getcwd()
                    os.chdir(work)
                    try:
                        standin_engine.write_files(plan)
                    finally:
                        os.chdir(before)
                self.assertEqual(sorted(p.name for p in outside.iterdir()), ["x.py"])
                self.assertEqual((outside / "x.py").read_text(encoding="utf-8"), "keep\n")
                self.assertFalse((work / "lib").is_symlink())
                self.assertEqual((work / "lib" / "new.py").read_text(encoding="utf-8"), "inside\n")

    def test_the_stand_in_engine_lays_out_either_change(self):
        for name, start, plan, check in (
                ("to-dir", lambda w: (w / "tool").write_text("old\n"),
                 {"tool/main.py": "X = 1\n", "tool": None}, lambda w: (w / "tool" / "main.py").read_text() == "X = 1\n"),
                ("to-file", lambda w: ((w / "tool").mkdir(), (w / "tool" / "main.py").write_text("")),
                 {"tool": "flat\n", "tool/main.py": None}, lambda w: (w / "tool").read_text() == "flat\n")):
            with self.subTest(name):
                work = self.folder / f"standin-{name}"
                work.mkdir()
                start(work)
                before = os.getcwd()
                os.chdir(work)
                try:
                    standin_engine.write_files(plan)
                finally:
                    os.chdir(before)
                self.assertTrue(check(work))


class OneBadCandidateTests(unittest.TestCase):
    def test_an_error_in_one_candidate_is_its_rejection(self):
        with outside_folder() as folder, scratch_bases() as (tmp, xdg):
            shas = transitions(folder / "upstream")
            repos = folder / "repos.json"
            repos.write_text(json.dumps({"schema": harvest.REPOS_SCHEMA, "repos": [repo_cfg(folder / "upstream")]}),
                             encoding="utf-8")
            real = validate.validate
            calls = []

            def flaky(task, **kwargs):
                calls.append(task.id)
                if len(calls) == 1:
                    raise FileExistsError(17, "File exists", "tool")
                return real(task, **kwargs)

            with mock.patch.object(harvest.validate_mod, "validate", side_effect=flaky):
                report = harvest.harvest(repos, "2026-02-01", folder / "held-out", suite_id="h", reruns=1,
                                         tmp_base=tmp, xdg_base=xdg, log=lambda _m: None)
            self.assertEqual(len(calls), 2)
            self.assertEqual(report["kept"], 1)
            self.assertEqual([(r["commit"], r["reason"].split(":")[0]) for r in report["rejected"]],
                             [(shas["to_dir"], "the task could not be built or checked")])
            self.assertIn("FileExistsError", report["rejected"][0]["reason"])
            self.assertFalse((folder / "held-out" / ".staging").exists())


if __name__ == "__main__":
    unittest.main()
