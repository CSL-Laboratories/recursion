"""Answers still open on draft #468 after the repository-setting reply.

An unhashable task id or replacement id must be a record error, not a TypeError.
A repos file or an issues file that cannot be read must be a refusal, and the output
folder must not exist. A task file that is missing or not UTF-8 must be a refusal.
A harvest must not share an output folder that already exists.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import unittest
from pathlib import Path

from eval import harvest, heldout, runner
from eval.tests import make_suite, outside_folder, scratch_bases, tiny_task
from eval.tests.tests_record import good
from eval.tests.tests_round_7 import _Fixture
from eval import record as record_mod


class UnhashableIdTests(unittest.TestCase):
    def test_an_unhashable_task_id_is_an_error(self):
        for bad in ([], {}):
            with self.subTest(kind=type(bad).__name__):
                record = good()
                record["trials"][0]["task_id"] = bad
                try:
                    problems = record_mod.validate(record)
                except TypeError as exc:
                    self.fail(f"raised TypeError: {exc}")
                self.assertTrue(problems)

    def test_an_unhashable_task_id_on_a_replacement_is_an_error(self):
        for bad in ([], {}):
            with self.subTest(kind=type(bad).__name__):
                record = good()
                record["trials"][3]["task_id"] = bad
                try:
                    problems = record_mod.validate(record)
                except TypeError as exc:
                    self.fail(f"raised TypeError: {exc}")
                self.assertTrue(problems)

    def test_an_unhashable_replacement_id_is_an_error(self):
        for bad in ([], {}):
            with self.subTest(kind=type(bad).__name__):
                record = good()
                record["trials"][3]["attempt"]["replacement_for"] = bad
                try:
                    problems = record_mod.validate(record)
                except TypeError as exc:
                    self.fail(f"raised TypeError: {exc}")
                self.assertTrue(problems)


class HarvestInputTests(_Fixture):
    def _run(self, out: Path, extra: list | None = None):
        argv = ["run", "--repos", str(self.repos), "--not-before", "2026-02-01", "--suite", "h",
                "--out", str(out), "--reruns", "1", "--tmp-base", str(self.tmp), "--xdg-base", str(self.xdg)]
        if extra:
            argv.extend(extra)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = harvest.main(argv)
        return code, err.getvalue()

    def test_a_repos_file_that_cannot_be_read_is_refused_before_output(self):
        out = self.folder / "out-repos"
        cases = {
            "truncated": self.folder / "bad.json",
            "not utf-8": self.folder / "bad-utf8.json",
            "missing": self.folder / "missing.json",
            "directory": self.folder / "repos-dir",
        }
        cases["truncated"].write_bytes(b'{"schema": "recursion-eval-repos/1", "repos": [')
        cases["not utf-8"].write_bytes(b'{"schema": "x", "repos": [\xff]}')
        cases["directory"].mkdir()
        for label, path in cases.items():
            with self.subTest(label):
                self.repos = path
                if out.exists():
                    raise AssertionError("a previous case created the output folder")
                try:
                    code, text = self._run(out)
                except (OSError, ValueError) as exc:
                    self.fail(f"raised {type(exc).__name__}: {exc}")
                self.assertEqual(code, 2, text)
                self.assertIn("refused:", text)
                self.assertFalse(out.exists(), text)

    def test_a_bad_issues_file_is_refused_before_output(self):
        self.write_repos()
        out = self.folder / "out-issues"
        issues = self.folder / "issues.json"
        cases = {
            "truncated": b"{",
            "not utf-8": b"{\xff}",
            "not an object": b"[]",
        }
        for label, payload in cases.items():
            with self.subTest(label):
                if out.exists():
                    shutil.rmtree(out)
                issues.write_bytes(payload)
                try:
                    code, text = self._run(out, ["--issues", str(issues)])
                except (OSError, ValueError, AttributeError) as exc:
                    self.fail(f"raised {type(exc).__name__}: {exc}")
                self.assertEqual(code, 2, text)
                self.assertIn("refused:", text)
                self.assertFalse(out.exists(), text)
        if out.exists():
            shutil.rmtree(out)
        missing = self.folder / "no-issues.json"
        try:
            code, text = self._run(out, ["--issues", str(missing)])
        except OSError as exc:
            self.fail(f"raised {type(exc).__name__}: {exc}")
        self.assertEqual(code, 2, text)
        self.assertFalse(out.exists(), text)

    def test_an_existing_output_folder_is_refused(self):
        self.write_repos()
        out = self.folder / "out-exists"
        out.mkdir()
        try:
            code, text = self._run(out)
        except OSError as exc:
            self.fail(f"raised {type(exc).__name__}: {exc}")
        self.assertEqual(code, 2, text)
        self.assertIn("not empty", text)
        self.assertEqual(list(out.iterdir()), [])


class RunnerInputTests(unittest.TestCase):
    def setUp(self):
        heldout._declared = None

    def tearDown(self):
        heldout._declared = None

    def _main(self, folder: Path):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = runner.main(["--suite", "pilot-t", "--tasks", str(folder), "--arms", "direct",
                                "--engine", "standin", "--k", "1", "--out", str(folder / "run-out")])
        return code, err.getvalue()

    def test_a_missing_or_non_utf8_task_file_is_refused(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            make_suite(folder, {"h-1": tiny_task("h-1", ["solve"])}, split="held_out", suite_id="pilot-t",
                       tmp_base=tmp, xdg_base=xdg)
            task = folder / "tasks" / "h-1.txtar"
            original = task.read_bytes()
            cases = {
                "not utf-8": lambda: task.write_bytes(original + b"\xff"),
                "missing": task.unlink,
            }
            for label, break_it in cases.items():
                with self.subTest(label):
                    if not task.exists():
                        task.write_bytes(original)
                    break_it()
                    try:
                        code, text = self._main(folder)
                    except (OSError, UnicodeDecodeError) as exc:
                        self.fail(f"raised {type(exc).__name__}: {exc}")
                    self.assertEqual(code, 2, text)
                    self.assertIn("refused:", text)
                    self.assertFalse((folder / "run-out").exists(), text)


if __name__ == "__main__":
    unittest.main()
