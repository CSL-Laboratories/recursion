"""The two round-6 reads of #381 at b7abd385 (GPT-6 Astra, GPT-6.1 Sol), all P2.

* Both: a repository's test_command, timeout_s and grading_timeout_s are the caller's; a bad one ends the
  harvest before anything is written, never as candidate rejections or a partial suite.
* Both: a machine Git failure is not one candidate's fault (GitError is out of the per-candidate catch).
* Sol 1, Astra 2: a scratch base that is an existing file, or under one, is refused before anything is written.
* Astra 3: a path that is not UTF-8 is refused at the candidate boundary (one rejection), without a broad catch.
* The guards' type checks (a bool or a float for --reruns or --max-per-repo) and the TarError and
  IsADirectoryError catches get tests of their own (both reads' coverage notes).

Every repository here is a made-up one in a scratch folder outside the repository.
"""
from __future__ import annotations

import json
import os
import tarfile
import unittest
from pathlib import Path
from unittest import mock

from eval import gitutil, harvest, validate
from eval.tests import outside_folder, scratch_bases
from eval.tests.tests_harvest import Repo
from eval.tests.tests_round_6 import repo_cfg, transitions


class _Fixture(unittest.TestCase):
    def setUp(self):
        outside = outside_folder()
        self.folder = outside.__enter__()
        self.addCleanup(outside.__exit__, None, None, None)
        bases = scratch_bases()
        self.tmp, self.xdg = bases.__enter__()
        self.addCleanup(bases.__exit__, None, None, None)
        transitions(self.folder / "upstream")
        self.repos = self.folder / "repos.json"

    def write_repos(self, *configs) -> None:
        configs = configs or (repo_cfg(self.folder / "upstream"),)
        self.repos.write_text(json.dumps({"schema": harvest.REPOS_SCHEMA, "repos": list(configs)}), encoding="utf-8")

    def cli(self, out: Path, *, tmp=None, xdg=None) -> int:
        return harvest.main(["run", "--repos", str(self.repos), "--not-before", "2026-02-01", "--suite", "h",
                             "--out", str(out), "--reruns", "1", "--tmp-base", str(tmp or self.tmp),
                             "--xdg-base", str(xdg or self.xdg)])


class RepositoryConfigTests(_Fixture):
    def test_a_bad_grading_field_ends_the_harvest_before_output(self):
        for override in ({"test_command": []}, {"test_command": "python"}, {"test_command": ["x", ""]},
                         {"timeout_s": 0}, {"timeout_s": True}, {"grading_timeout_s": -1},
                         {"grading_timeout_s": "60"}):
            with self.subTest(override=override):
                self.write_repos(dict(repo_cfg(self.folder / "upstream"), **override))
                out = self.folder / f"out-{len(os.listdir(self.folder))}"
                self.assertEqual(self.cli(out), 2)
                self.assertFalse(out.exists())

    def test_one_bad_repository_is_not_a_partial_harvest(self):
        good = dict(repo_cfg(self.folder / "upstream"), name="good")
        bad = dict(repo_cfg(self.folder / "upstream"), name="bad", grading_timeout_s=-1)
        self.write_repos(good, bad)
        out = self.folder / "out-mixed"
        self.assertEqual(self.cli(out), 2)
        self.assertFalse(out.exists())


class ScratchBaseTests(_Fixture):
    def test_a_file_as_a_base_or_above_one_is_refused_before_output(self):
        blocker = self.folder / "not-a-folder"
        blocker.write_text("fixture", encoding="utf-8")
        dangling = self.folder / "dangling"
        os.symlink(self.folder / "missing", dangling)
        self.write_repos()
        for name, kwargs in (("tmp is a file", {"tmp": blocker}), ("xdg is a file", {"xdg": blocker}),
                             ("tmp under a file", {"tmp": blocker / "nested"}),
                             ("xdg under a file", {"xdg": blocker / "nested" / "deeper"}),
                             ("tmp is a dangling link", {"tmp": dangling})):
            with self.subTest(name):
                out = self.folder / f"out-{name.replace(' ', '-')}"
                self.assertEqual(self.cli(out, **kwargs), 2)
                self.assertFalse(out.exists())

    def test_a_base_that_does_not_exist_yet_is_still_accepted(self):
        self.write_repos()
        out = self.folder / "out-new-bases"
        self.assertEqual(self.cli(out, tmp=self.tmp / "new" / "tmp", xdg=self.xdg / "new" / "xdg"), 0)


class MachineGitTests(_Fixture):
    def test_a_real_git_permission_failure_ends_the_run(self):
        self.write_repos()
        real = gitutil.git
        seen = []

        def read_only_index(args, cwd, **kwargs):
            if args[:3] == ["add", "--all", "--force"] and not seen:
                dotgit = Path(cwd) / ".git"
                mode = dotgit.stat().st_mode & 0o777
                dotgit.chmod(0o500)
                try:
                    return real(args, cwd, **kwargs)
                except gitutil.GitError as exc:
                    seen.append(str(exc))
                    raise
                finally:
                    dotgit.chmod(mode)
            return real(args, cwd, **kwargs)

        out = self.folder / "out-git"
        with mock.patch.object(gitutil, "git", side_effect=read_only_index):
            code = self.cli(out)
        self.assertEqual(len(seen), 1)
        self.assertIn("Permission denied", seen[0])
        self.assertEqual(code, 2)
        self.assertFalse((out / "suite.json").exists())

    def test_a_git_failure_after_a_kept_task_still_ends_the_run(self):
        self.write_repos()
        calls = []

        def fails_late(work, _real=gitutil.stage_task_files):
            calls.append(work)
            if len(calls) < 4:
                return _real(work)
            raise gitutil.GitError("git add --all --force failed (128): an object write error")

        out = self.folder / "out-git-late"
        with mock.patch.object(gitutil, "stage_task_files", side_effect=fails_late):
            self.assertEqual(self.cli(out), 2)
        self.assertFalse((out / "suite.json").exists())


class EncodingTests(_Fixture):
    def test_a_path_that_is_not_utf8_is_one_rejection_and_the_next_repository_is_kept(self):
        legacy = Repo(self.folder / "legacy")
        legacy.commit({"value.txt": "old", "tests/__init__.py": ""}, "initial", "2026-01-01T00:00:00+0000")
        name = os.fsdecode(b"caf\xe9.txt")
        candidate = legacy.commit({"value.txt": "new", name: "fixture", "tests/test_value.py":
                                   "import unittest\nfrom pathlib import Path\n\n\nclass T(unittest.TestCase):\n"
                                   "    def test_value(self):\n        self.assertEqual(Path('value.txt').read_text(), 'new')\n"},
                                  "legacy file name", "2026-03-01T00:00:00+0000")
        self.write_repos(dict(repo_cfg(self.folder / "legacy"), name="legacy"), repo_cfg(self.folder / "upstream"))
        report = harvest.harvest(self.repos, "2026-02-01", self.folder / "out-enc", suite_id="h", reruns=1,
                                 tmp_base=self.tmp, xdg_base=self.xdg, log=lambda _m: None)
        self.assertEqual((report["kept"], len(report["rejected"])), (2, 1))
        rejected = report["rejected"][0]
        self.assertEqual((rejected["repo"], rejected["commit"]), ("legacy", candidate))
        self.assertIn("not UTF-8", rejected["reason"])


class GuardTypeTests(_Fixture):
    def test_reruns_and_max_per_repo_must_be_whole_numbers(self):
        self.write_repos()
        for kwargs in ({"reruns": True}, {"reruns": 1.0}, {"reruns": "1"}, {"max_per_repo": True},
                       {"max_per_repo": 2.0}):
            with self.subTest(kwargs=kwargs):
                args = dict(reruns=1, **kwargs) if "reruns" not in kwargs else dict(kwargs)
                with self.assertRaises(harvest.HarvestError):
                    harvest.harvest(self.repos, "2026-02-01", self.folder / "out-types", suite_id="h",
                                    tmp_base=self.tmp, xdg_base=self.xdg, log=lambda _m: None, **args)
                self.assertFalse((self.folder / "out-types").exists())

    def test_a_bad_start_archive_or_a_folder_where_a_file_must_be_is_one_rejection(self):
        self.write_repos()
        for exc in (tarfile.ReadError("not a tar archive"), IsADirectoryError(21, "Is a directory", "tool")):
            with self.subTest(type(exc).__name__):
                calls = []

                def first_fails(task, _calls=calls, _exc=exc, _real=validate.validate, **kwargs):
                    _calls.append(task.id)
                    if len(_calls) == 1:
                        raise _exc
                    return _real(task, **kwargs)

                with mock.patch.object(harvest.validate_mod, "validate", side_effect=first_fails):
                    report = harvest.harvest(self.repos, "2026-02-01", self.folder / f"out-{type(exc).__name__}",
                                             suite_id="h", reruns=1, tmp_base=self.tmp, xdg_base=self.xdg,
                                             log=lambda _m: None)
                self.assertEqual((report["kept"], len(report["rejected"])), (1, 1))
                self.assertIn(type(exc).__name__, report["rejected"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
