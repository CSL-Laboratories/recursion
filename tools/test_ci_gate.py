#!/usr/bin/env python3
"""Unit tests for tools/ci_gate.py path filtering and docs-only public-scope."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load_ci_gate():
    spec = importlib.util.spec_from_file_location("ci_gate", HERE / "ci_gate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_gate = load_ci_gate()

GIT_IDENTITY = {
    **os.environ,
    "GIT_AUTHOR_NAME": "ci-gate-test",
    "GIT_AUTHOR_EMAIL": "ci-gate-test@example.com",
    "GIT_COMMITTER_NAME": "ci-gate-test",
    "GIT_COMMITTER_EMAIL": "ci-gate-test@example.com",
}


def git(repo: Path, *args: str, extra_config: list[str] | None = None) -> str:
    cmd = ["git", "-c", "core.longpaths=true"]
    if extra_config:
        cmd.extend(extra_config)
    cmd.extend(args)
    return subprocess.check_output(
        cmd, cwd=repo, env=GIT_IDENTITY, encoding="utf-8", errors="replace"
    ).strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class TempRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_root = ci_gate.ROOT
        self.repo = Path(tempfile.mkdtemp(prefix="ci-gate-"))
        ci_gate.ROOT = self.repo
        git(self.repo, "init", "-b", "master")
        git(self.repo, "config", "user.email", "ci-gate-test@example.com")
        git(self.repo, "config", "user.name", "ci-gate-test")
        # Force the quoting Git would apply without core.quotePath=false.
        git(self.repo, "config", "core.quotePath", "true")
        write(self.repo / "README.md", "seed\n")
        self.seed = commit(self.repo, "seed")

    def tearDown(self) -> None:
        ci_gate.ROOT = self._orig_root
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_rename_source_into_docs_is_not_docs_only(self) -> None:
        write(self.repo / "tools" / "gate_sample.py", "x = 1\n")
        before = commit(self.repo, "add source")
        (self.repo / "docs").mkdir(parents=True, exist_ok=True)
        git(self.repo, "mv", "tools/gate_sample.py", "docs/gate_sample.md")
        commit(self.repo, "rename source into docs")

        default_names = git(self.repo, "diff", "--name-only", before, "HEAD")
        self.assertEqual(default_names, "docs/gate_sample.md")

        paths = ci_gate.changed_paths(before)
        self.assertIn("tools/gate_sample.py", paths)
        self.assertIn("docs/gate_sample.md", paths)
        self.assertFalse(ci_gate.docs_only(before))

    def test_docs_to_docs_rename_is_docs_only(self) -> None:
        write(self.repo / "docs" / "a.md", "note\n")
        before = commit(self.repo, "add docs")
        git(self.repo, "mv", "docs/a.md", "docs/b.md")
        commit(self.repo, "rename docs")
        paths = ci_gate.changed_paths(before)
        self.assertIn("docs/a.md", paths)
        self.assertIn("docs/b.md", paths)
        self.assertTrue(ci_gate.docs_only(before))

    def test_unicode_source_path_is_not_docs_only(self) -> None:
        unicode_rel = "tools/naïve.py"
        write(self.repo / unicode_rel, "x = 1\n")
        commit(self.repo, "add unicode source")

        quoted = git(self.repo, "-c", "core.quotePath=true", "diff", "--name-only", self.seed, "HEAD")
        self.assertTrue(quoted.startswith('"') or "\\" in quoted)

        paths = ci_gate.changed_paths(self.seed)
        self.assertEqual(paths, [unicode_rel])
        self.assertFalse(any(path.startswith('"') or "\\" in path for path in paths))
        self.assertTrue(ci_gate.is_gate_path(paths[0]))
        self.assertFalse(ci_gate.docs_only(self.seed))


class LightweightControlTests(unittest.TestCase):
    def test_run_lightweight_invokes_public_scope(self) -> None:
        with mock.patch.object(ci_gate, "whitespace_check") as whitespace, mock.patch.object(
            ci_gate, "run_program"
        ) as run_program:
            ci_gate.run_lightweight()
            whitespace.assert_called_once_with()
            run_program.assert_called_once_with(["tools/check_public_scope.py"])

    def test_docs_only_main_runs_public_scope_not_suite(self) -> None:
        with mock.patch.object(ci_gate, "decide_run_full", return_value=False), mock.patch.object(
            ci_gate, "run_lightweight"
        ) as lightweight, mock.patch.object(ci_gate, "run_suite") as suite, mock.patch.object(
            ci_gate, "emit_output"
        ), mock.patch.object(sys, "argv", ["ci_gate.py"]):
            self.assertEqual(ci_gate.main(), 0)
            lightweight.assert_called_once_with()
            suite.assert_not_called()

    def test_paths_only_skips_lightweight_and_suite(self) -> None:
        with mock.patch.object(ci_gate, "decide_run_full", return_value=False), mock.patch.object(
            ci_gate, "run_lightweight"
        ) as lightweight, mock.patch.object(ci_gate, "run_suite") as suite, mock.patch.object(
            ci_gate, "emit_output"
        ), mock.patch.object(sys, "argv", ["ci_gate.py", "--paths-only"]):
            self.assertEqual(ci_gate.main(), 0)
            lightweight.assert_not_called()
            suite.assert_not_called()

    def test_is_gate_path_rejects_quoted_unicode(self) -> None:
        self.assertTrue(ci_gate.is_gate_path("tools/naïve.py"))
        self.assertFalse(ci_gate.is_gate_path('"tools/na\\303\\257ve.py"'))

    def test_git_nul_survives_invalid_utf8_bytes(self) -> None:
        payload = b"docs/note-\xff.md\0tools/ok.py\0"
        with mock.patch.object(ci_gate.subprocess, "check_output", return_value=payload):
            paths = ci_gate.git_nul("diff", "-z", "--name-only")
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], b"docs/note-\xff.md".decode("utf-8", errors="surrogateescape"))
        self.assertEqual(paths[1], "tools/ok.py")
        self.assertFalse(ci_gate.is_gate_path(paths[0]))
        self.assertTrue(ci_gate.is_gate_path(paths[1]))


class TestInventoryTests(unittest.TestCase):
    def test_repo_test_files_are_run_or_excluded(self) -> None:
        run = ci_gate.assert_tests_accounted()
        discovered = set(ci_gate.discover_all_test_files())
        exclude = set(ci_gate.TEST_EXCLUDE)
        self.assertEqual(discovered, set(run) | exclude)
        self.assertFalse(set(run) & exclude)
        for path in ci_gate.REQUIRED_SUITE_TESTS:
            self.assertIn(path, run)
        for path in exclude:
            self.assertTrue((ROOT / path).is_file(), path)
            self.assertNotIn(path, run)

    def test_unaccounted_test_file_fails_guard(self) -> None:
        run, unaccounted, stale, missing = ci_gate.classify_tests(
            discovered=["tools/test_ci_gate.py", "orphan/test_orphan.py"],
            globbed=["tools/test_ci_gate.py"],
            exclude=set(),
            required=set(),
        )
        self.assertEqual(run, ["tools/test_ci_gate.py"])
        self.assertEqual(unaccounted, ["orphan/test_orphan.py"])
        self.assertEqual(stale, [])
        self.assertEqual(missing, [])

    def test_excluded_test_is_not_run(self) -> None:
        run, unaccounted, stale, missing = ci_gate.classify_tests(
            discovered=["tools/test_ci_gate.py", "tools/test_slow.py"],
            globbed=["tools/test_ci_gate.py", "tools/test_slow.py"],
            exclude={"tools/test_slow.py"},
            required={"tools/test_ci_gate.py"},
        )
        self.assertEqual(run, ["tools/test_ci_gate.py"])
        self.assertEqual(unaccounted, [])
        self.assertEqual(stale, [])
        self.assertEqual(missing, [])

    def test_stale_exclude_and_missing_required(self) -> None:
        run, unaccounted, stale, missing = ci_gate.classify_tests(
            discovered=["tools/test_ci_gate.py"],
            globbed=["tools/test_ci_gate.py"],
            exclude={"gone/test_gone.py"},
            required={"tools/test_ci_gate.py", "tools/test_missing.py"},
        )
        self.assertEqual(run, ["tools/test_ci_gate.py"])
        self.assertEqual(unaccounted, [])
        self.assertEqual(stale, ["gone/test_gone.py"])
        self.assertEqual(missing, ["tools/test_missing.py"])

    def test_new_tools_test_is_globbed(self) -> None:
        repo = Path(tempfile.mkdtemp(prefix="ci-gate-disc-"))
        try:
            write(repo / "tools" / "test_new.py", "x = 1\n")
            globbed = ci_gate.glob_suite_tests(repo)
            discovered = ci_gate.discover_all_test_files(repo)
            self.assertIn("tools/test_new.py", globbed)
            self.assertIn("tools/test_new.py", discovered)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_orphan_test_is_discovered_but_not_globbed(self) -> None:
        repo = Path(tempfile.mkdtemp(prefix="ci-gate-disc-"))
        try:
            write(repo / "orphan" / "test_orphan.py", "x = 1\n")
            globbed = ci_gate.glob_suite_tests(repo)
            discovered = ci_gate.discover_all_test_files(repo)
            self.assertNotIn("orphan/test_orphan.py", globbed)
            self.assertIn("orphan/test_orphan.py", discovered)
            _, unaccounted, _, _ = ci_gate.classify_tests(
                discovered=discovered,
                globbed=globbed,
                exclude=set(),
                required=set(),
            )
            self.assertEqual(unaccounted, ["orphan/test_orphan.py"])
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_extra_suite_preserves_experiment_order(self) -> None:
        tests = [
            "tools/test_ci_gate.py",
            "scripts/tests/v3/planning/schema.test.py",
            "experiments/v3/delegated-q-coupling-20260914/test_contract.py",
            "experiments/v3/recovery-ledger-20260914/test_ledger.py",
            "experiments/v3/new/test_added.py",
        ]
        extras = ci_gate.extra_suite_programs(tests)
        self.assertEqual(
            extras,
            [
                ("experiments/v3/recovery-ledger-20260914/test_ledger.py",),
                ("experiments/v3/delegated-q-coupling-20260914/test_contract.py",),
                ("experiments/v3/new/test_added.py",),
            ],
        )


if __name__ == "__main__":
    if Path.cwd().resolve() != ROOT.resolve():
        raise SystemExit(f"run from the repository root ({ROOT})")
    result = unittest.main(verbosity=2, exit=False)
    raise SystemExit(0 if result.result.wasSuccessful() else 1)
