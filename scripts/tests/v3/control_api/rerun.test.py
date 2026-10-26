#!/usr/bin/env python3
"""The frozen-SHA rerun script refuses moving refs and records exact evidence.

Provider-free. Real runs use a throwaway detached worktree and only the quick
steps (the full gate is what the script runs on Nov 9, and it is this gate).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "v3" / "control_api" / "rerun_frozen_sha.sh"
BASH = shutil.which("bash")
IS_LINUX = platform.system() == "Linux"


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def run_script(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, timeout=170)


def have_git_head() -> bool:
    try:
        git("rev-parse", "--verify", "HEAD")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


@unittest.skipIf(BASH is None or os.name == "nt", "needs bash on a POSIX host")
class ScriptShapeTests(unittest.TestCase):
    def test_file_is_lf_executable_and_strict(self) -> None:
        raw = SCRIPT.read_bytes()
        self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\n"))
        self.assertNotIn(b"\r", raw)
        self.assertIn(b"set -euo pipefail", raw)
        self.assertTrue(SCRIPT.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(0, subprocess.run([BASH, "-n", str(SCRIPT)]).returncode)

    def test_the_script_names_no_provider_or_network_command(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        for word in ("curl", "wget", "ssh ", "git push", "git fetch", "claude ", "codex "):
            self.assertNotIn(word, text.replace("(try: git fetch origin $sha)", ""), word)


@unittest.skipIf(BASH is None or os.name == "nt", "needs bash on a POSIX host")
class RefusalTests(unittest.TestCase):
    def test_anything_but_a_full_lowercase_sha_is_refused(self) -> None:
        for arg in ("HEAD", "master", "origin/master", "abc1234", "A" * 40, "g" * 40, "a" * 39, "a" * 41, "a" * 40 + "~1"):
            result = run_script(arg, "--dry-run")
            self.assertEqual(2, result.returncode, arg)
            self.assertEqual("", result.stdout)

    def test_bad_usage_is_refused(self) -> None:
        for args in (
            (),
            ("--dry-run",),
            ("a" * 40, "b" * 40),
            ("a" * 40, "--steps", "gate,rm"),
            ("a" * 40, "--steps", "record,record"),
            ("a" * 40, "--bogus"),
            ("a" * 40, "--out"),
        ):
            self.assertEqual(2, run_script(*args).returncode, args)

    @unittest.skipUnless(have_git_head(), "needs a git checkout")
    def test_a_well_formed_sha_that_is_not_a_commit_here_is_refused(self) -> None:
        result = run_script("0" * 40, "--dry-run")
        self.assertEqual(2, result.returncode)
        self.assertIn("not a commit in this clone", result.stderr)

    @unittest.skipUnless(have_git_head(), "needs a git checkout")
    def test_dry_run_resolves_the_frozen_sha_and_touches_nothing(self) -> None:
        head = git("rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "evidence"
            result = run_script(head, "--dry-run", "--out", str(out))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(f"sha={head}", result.stdout)
            self.assertFalse(out.exists())


@unittest.skipUnless(BASH and IS_LINUX, "a real run is Linux only")
class EvidenceTests(unittest.TestCase):
    @unittest.skipUnless(have_git_head(), "needs a git checkout")
    def test_a_passing_run_names_the_exact_commit_and_every_command(self) -> None:
        head = git("rev-parse", "HEAD")
        before = git("status", "--porcelain")
        with tempfile.TemporaryDirectory() as directory:
            result = run_script(head, "--steps", "record,scope", "--out", directory)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            evidence = json.loads((Path(directory) / f"spine-rerun-{head}.json").read_text())
            self.assertEqual(head, evidence["candidate_sha"])
            self.assertTrue(evidence["passed"])
            self.assertEqual(0, evidence["provider_calls"])
            self.assertEqual("Linux", evidence["environment"]["system"])
            self.assertEqual(["record", "scope"], [s["step"] for s in evidence["steps"]])
            for step in evidence["steps"]:
                self.assertEqual(0, step["exit_code"])
                self.assertRegex(step["log_sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue((Path(directory) / step["log"]).is_file())
            self.assertIn("scripts.v3.control_api.record --check", evidence["steps"][0]["command"])
            self.assertEqual(0, evidence["tree_dirty_files_after"])
        self.assertEqual(before, git("status", "--porcelain"), "the working tree must not change")
        self.assertFalse(any(line.split()[0].endswith("/tree") for line in git("worktree", "list").splitlines()))

    @unittest.skipUnless(have_git_head(), "needs a git checkout")
    def test_default_out_does_not_dirty_the_invoking_checkout(self) -> None:
        head = git("rev-parse", "HEAD")
        before = git("status", "--porcelain")
        result = run_script(head, "--steps", "scope")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        summary = json.loads(result.stdout.splitlines()[-1])
        evidence = Path(summary["evidence"])
        self.assertTrue(evidence.is_file())
        self.assertFalse(str(evidence.resolve()).startswith(str(ROOT.resolve())))
        self.assertEqual(before, git("status", "--porcelain"), "the working tree must not change")
        self.assertFalse((ROOT / "spine-rerun-evidence").exists())

    def test_a_failing_step_fails_the_run_and_the_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            git("init", "-q", cwd=repo)
            (repo / "README").write_text("no spine here\n")
            git("add", "README", cwd=repo)
            git("commit", "-q", "-m", "empty candidate", cwd=repo)
            sha = git("rev-parse", "HEAD", cwd=repo)
            out = Path(directory) / "out"
            result = run_script(sha, "--steps", "record", "--out", str(out), cwd=repo)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            evidence = json.loads((out / f"spine-rerun-{sha}.json").read_text())
            self.assertFalse(evidence["passed"])
            self.assertNotEqual(0, evidence["steps"][0]["exit_code"])
            self.assertFalse(any(line.split()[0].endswith("/tree") for line in git("worktree", "list", cwd=repo).splitlines()))


if __name__ == "__main__":
    unittest.main()
