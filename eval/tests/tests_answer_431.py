"""Answers to the first reviews of draft #431.

A bad repository setting ends the harvest before any output folder exists. A path this filesystem
encoding cannot hold, and a source working-tree-encoding other than UTF-8, are that candidate's
refusal; the next repository is still harvested. Every repository here is made up in a scratch folder.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path

from eval import REPO_ROOT, harvest
from eval.tests.tests_harvest import Repo
from eval.tests.tests_round_6 import repo_cfg
from eval.tests.tests_round_7 import _Fixture

ANSWER_TEST = (
    "import unittest\nfrom calc import answer\n\n\nclass T(unittest.TestCase):\n"
    "    def test_answer(self):\n        self.assertEqual(answer(), 1)\n"
)


def _end_process(proc: subprocess.Popen) -> None:
    """End a process this test started, by its own process id."""
    if proc.poll() is None:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


class CallerShapeTests(_Fixture):
    def _expect_refusal(self, text: str) -> None:
        self.repos.write_text(text, encoding="utf-8")
        out = self.folder / "out-shape"
        if out.exists():
            import shutil
            shutil.rmtree(out)
        try:
            code = self.cli(out)
        except Exception as exc:  # a traceback is the failure the review recorded
            self.fail(f"raised {type(exc).__name__}: {exc}")
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())

    def _body(self, repos) -> str:
        return json.dumps({"schema": harvest.REPOS_SCHEMA, "repos": repos})

    def test_a_repository_entry_of_the_wrong_shape_writes_nothing(self):
        good = repo_cfg(self.folder / "upstream")
        cases = {
            "entry is not an object": [5],
            "name is not a string": [dict(good, name=5)],
            "path is not a string": [dict(good, path=5)],
            "branch is not a string": [dict(good, branch=5)],
            "test_globs is an int": [dict(good, test_globs=5)],
            "test_globs is one string": [dict(good, test_globs="tests/**/test_*.py")],
            "test_globs holds null": [dict(good, test_globs=[None])],
            "test_globs is empty": [dict(good, test_globs=[])],
        }
        for label, repos in cases.items():
            with self.subTest(label):
                self._expect_refusal(self._body(repos))

    def test_a_bad_glob_list_on_a_later_repository_writes_nothing(self):
        good = dict(repo_cfg(self.folder / "upstream"), name="good")
        bad = dict(repo_cfg(self.folder / "upstream"), name="bad", test_globs="tests/**/test_*.py")
        self._expect_refusal(self._body([good, bad]))

    def test_a_nonfinite_or_unrepresentable_timeout_writes_nothing(self):
        base = repo_cfg(self.folder / "upstream")
        cases = []
        for key, present in (("timeout_s", 600), ("grading_timeout_s", 120)):
            for label, value in (("nan", float("nan")), ("infinity", float("inf")),
                                 ("negative infinity", float("-inf"))):
                cfg = dict(base)
                cfg[key] = value
                cases.append((f"{key} {label}", self._body([cfg])))
            text = self._body([base]).replace(f'"{key}": {present}', f'"{key}": 1e309')
            cases.append((f"{key} 1e309", text))
            cfg = dict(base)
            cfg[key] = 10 ** 400
            cases.append((f"{key} huge", self._body([cfg])))
        for label, text in cases:
            with self.subTest(label):
                self._expect_refusal(text)

    def test_a_nul_in_the_command_writes_nothing(self):
        cfg = dict(repo_cfg(self.folder / "upstream"), test_command=["{python}", "-c", "pass\u0000"])
        self._expect_refusal(self._body([cfg]))

    def test_omitted_and_null_timeouts_stay_accepted(self):
        cfg = repo_cfg(self.folder / "upstream")
        del cfg["timeout_s"]
        del cfg["grading_timeout_s"]
        self.repos.write_text(self._body([cfg]), encoding="utf-8")
        harvest.load_repos(self.repos)
        cfg = repo_cfg(self.folder / "upstream")
        cfg["timeout_s"] = None
        cfg["grading_timeout_s"] = None
        self.repos.write_text(self._body([cfg]), encoding="utf-8")
        harvest.load_repos(self.repos)
        cfg = repo_cfg(self.folder / "upstream")
        cfg["timeout_s"] = 1.5
        cfg["grading_timeout_s"] = 2.5
        self.repos.write_text(self._body([cfg]), encoding="utf-8")
        repos, _sha = harvest.load_repos(self.repos)
        self.assertEqual((repos[0]["timeout_s"], repos[0]["grading_timeout_s"]), (1.5, 2.5))


class BoundaryTests(_Fixture):
    def _child_harvest(self, out: Path) -> tuple:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "0"
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env["LC_CTYPE"] = "C"
        root = str(REPO_ROOT)
        previous = env.get("PYTHONPATH")
        env["PYTHONPATH"] = root if not previous else root + os.pathsep + previous
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "eval.harvest", "run", "--repos", str(self.repos),
             "--not-before", "2026-02-01", "--suite", "h", "--out", str(out), "--reruns", "1",
             "--tmp-base", str(self.tmp), "--xdg-base", str(self.xdg)],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=180)
            except subprocess.TimeoutExpired:
                return -1, b"", b"timed out"
            return proc.returncode, stdout, stderr
        finally:
            _end_process(proc)

    def _grade(self, repo: Repo, expected: int) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "unittest", "tests.test_answer"],
            cwd=repo.path, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            _out, err = proc.communicate(timeout=60)
        finally:
            _end_process(proc)
        self.assertEqual(proc.returncode, expected, err.decode("utf-8", "replace")[-400:])

    def test_a_utf8_name_is_kept_where_the_filesystem_can_encode_it(self):
        marked = Repo(self.folder / "marked")
        marked.commit({"calc.py": "def answer():\n    return 0\n", "tests/__init__.py": ""},
                      "initial", "2026-01-01T00:00:00+0000")
        marked.commit({"calc.py": "def answer():\n    return 1\n", "tests/test_answer.py": ANSWER_TEST,
                       "caf\u00e9.txt": "note\n"},
                      "utf-8 file name", "2026-03-01T00:00:00+0000")
        self.write_repos(dict(repo_cfg(self.folder / "marked"), name="marked"),
                         repo_cfg(self.folder / "upstream"))
        report = harvest.harvest(self.repos, "2026-02-01", self.folder / "out-utf8", suite_id="h", reruns=1,
                                 tmp_base=self.tmp, xdg_base=self.xdg, log=lambda _m: None)
        self.assertEqual(report["kept"], 3)
        self.assertFalse(any(item["repo"] == "marked" for item in report["rejected"]))
        code, _stdout, stderr = self._child_harvest(self.folder / "out-locale")
        self.assertEqual(code, 0, stderr.decode("utf-8", "replace")[-500:])
        saved = json.loads((self.folder / "out-locale" / "harvest-report.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["kept"], 2)
        marked_reasons = [item["reason"] for item in saved["rejected"] if item["repo"] == "marked"]
        self.assertEqual(len(marked_reasons), 1)
        self.assertIn("cannot be written", marked_reasons[0])

    def test_a_working_tree_encoding_is_one_rejection_and_the_next_repository_is_kept(self):
        repo = Repo(self.folder / "encoded")
        repo.commit({"calc.py": "def answer():\n    return 0\n", "tests/__init__.py": ""},
                    "initial", "2026-01-01T00:00:00+0000")
        (repo.path / "calc.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
        (repo.path / ".gitattributes").write_text("*.ps1 working-tree-encoding=UTF-16LE\n", encoding="utf-8")
        (repo.path / "tests").mkdir(exist_ok=True)
        (repo.path / "tests" / "test_answer.py").write_text(ANSWER_TEST, encoding="utf-8")
        (repo.path / "sample.ps1").write_bytes("hello".encode("utf-16-le"))
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "answer and encoded script", date="2026-03-01T00:00:00+0000")
        self.assertEqual(repo.git("cat-file", "-p", "HEAD:sample.ps1"), "hello")
        head = repo.git("rev-parse", "HEAD")
        try:
            self._grade(repo, 0)
            repo.git("checkout", "-q", "HEAD~1")
            self._grade(repo, 1)
        finally:
            repo.git("checkout", "-q", head)
        self.write_repos(dict(repo_cfg(self.folder / "encoded"), name="encoded"),
                         repo_cfg(self.folder / "upstream"))
        report = harvest.harvest(self.repos, "2026-02-01", self.folder / "out-attr", suite_id="h", reruns=1,
                                 tmp_base=self.tmp, xdg_base=self.xdg, log=lambda _m: None)
        self.assertEqual(report["kept"], 2)
        reasons = [item["reason"] for item in report["rejected"] if item["repo"] == "encoded"]
        self.assertEqual(len(reasons), 1)
        self.assertIn("working-tree-encoding", reasons[0])
        self.assertIn("UTF-16LE", reasons[0])

    def test_a_utf8_working_tree_encoding_is_not_a_refusal(self):
        repo = Repo(self.folder / "plain-attr")
        repo.commit({"calc.py": "def answer():\n    return 0\n", ".gitattributes":
                     "*.txt working-tree-encoding=UTF-8\n", "note.txt": "a\n"},
                    "initial", "2026-01-01T00:00:00+0000")
        files = harvest.tree_files(repo.path, repo.git("rev-parse", "HEAD"))
        self.assertIn(".gitattributes", files)


if __name__ == "__main__":
    unittest.main()
