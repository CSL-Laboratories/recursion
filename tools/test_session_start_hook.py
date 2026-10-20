#!/usr/bin/env python3
"""Tests for .claude/hooks/session-start.sh (CSL-302).

The hook itself is not modified. These tests drive it with a local bare
origin, a feature-branch clone, and a PATH stub for python3 so pip is never
invoked and no network is used.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HOOK = ".claude/hooks/session-start.sh"

GIT_AUTHOR = {
    "GIT_AUTHOR_NAME": "session-start-test",
    "GIT_AUTHOR_EMAIL": "session-start-test@example.com",
    "GIT_COMMITTER_NAME": "session-start-test",
    "GIT_COMMITTER_EMAIL": "session-start-test@example.com",
}

# Keys that would point git at the parent process instead of the temp repo.
_GIT_ENV_DROPS = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
}


def _bash_available() -> bool:
    return shutil.which("bash") is not None


@unittest.skipUnless(_bash_available(), "bash is not available")
class SessionStartHookTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        self.tmp = Path(tempfile.mkdtemp(prefix="session-start-hook-"))
        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        self.bin = self.tmp / "bin"
        self.marker = self.tmp / "python3-invoked"
        self.env_file = self.tmp / "claude.env"
        self.gitconfig = self.tmp / "gitconfig"
        self.gitconfig.write_text("", encoding="utf-8")
        self.bin.mkdir()
        self._write_stub(0)
        self._init_repos()

    def tearDown(self) -> None:
        tmp = getattr(self, "tmp", None)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in list(env):
            if key in _GIT_ENV_DROPS or key.startswith("GIT_CONFIG_"):
                env.pop(key, None)
        env.update(GIT_AUTHOR)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = str(self.gitconfig)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self._env(),
            check=True,
            capture_output=True,
            text=True,
        )

    def _rev(self, *args: str) -> str:
        return self._git(self.work, "rev-parse", *args).stdout.strip()

    def _merge_base(self) -> str:
        return self._git(self.work, "merge-base", "HEAD", "origin/master").stdout.strip()

    def _write_stub(self, exit_code: int) -> None:
        stub = self.bin / "python3"
        marker = shlex.quote(str(self.marker))
        stub.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' invoked >> {marker}\n"
            f"exit {int(exit_code)}\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)

    def _init_repos(self) -> None:
        seed = self.tmp / "seed"
        self._git(self.tmp, "init", "-b", "master", str(seed))
        (seed / "README").write_text("seed\n", encoding="utf-8")
        self._git(seed, "add", "README")
        self._git(seed, "commit", "-m", "seed")
        self._git(self.tmp, "clone", "--bare", str(seed), str(self.origin))
        self._git(self.tmp, "clone", str(self.origin), str(self.work))
        self._git(self.work, "checkout", "-b", "feature")
        (self.work / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git(self.work, "add", "feature.txt")
        self._git(self.work, "commit", "-m", "feature")

    def _run(self, *, remote: bool) -> subprocess.CompletedProcess[str]:
        env = self._env()
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["CLAUDE_PROJECT_DIR"] = str(self.work)
        env["CLAUDE_ENV_FILE"] = str(self.env_file)
        if remote:
            env["CLAUDE_CODE_REMOTE"] = "true"
        else:
            env.pop("CLAUDE_CODE_REMOTE", None)
        return subprocess.run(
            ["bash", HOOK],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_remote_success_writes_base_and_leaves_head(self) -> None:
        head = self._rev("HEAD")
        base = self._merge_base()
        self.assertNotEqual(head, base)
        self.assertFalse(self.marker.exists())

        result = self._run(remote=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fetch=ok deps=ok ci_base=ok", result.stdout)
        env_text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("export PYTHONUTF8=1 PYTHONIOENCODING=utf-8", env_text)
        self.assertIn(f"export CI_BASE_SHA={base}", env_text)
        self.assertEqual(self._rev("HEAD"), head)
        self.assertTrue(self.marker.exists())

    def test_local_mode_skips_without_env_or_python(self) -> None:
        result = self._run(remote=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.env_file.exists())
        self.assertFalse(self.marker.exists())

    def test_deps_failure_exits_and_still_writes_base(self) -> None:
        self._write_stub(1)
        base = self._merge_base()

        result = self._run(remote=True)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("deps=FAIL", result.stderr)
        env_text = self.env_file.read_text(encoding="utf-8")
        self.assertIn(f"export CI_BASE_SHA={base}", env_text)
        self.assertTrue(self.marker.exists())

    def test_fetch_failure_reports_stale_master(self) -> None:
        missing = self.tmp / "missing-origin"
        self.assertFalse(missing.exists())
        self._git(self.work, "remote", "set-url", "origin", str(missing))

        result = self._run(remote=True)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("fetch=FAIL", result.stderr)
        self.assertIn("(stale)", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
