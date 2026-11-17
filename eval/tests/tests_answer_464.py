"""Answers to the first review of draft #464.

A name planted between the check and the write must not make the install follow a symlink out of
the work tree, and must not make it wait on a FIFO. The final hidden-test summary is what the
all-skipped rule reads: an earlier nested summary does not count, and a run where nothing passed
is not a pass.
"""
from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import REPO_ROOT, runner, scratch, tasks

_CHILD = r"""
import os, stat, sys, threading, time
from pathlib import Path
from eval import tasks
target, outside, work_s, kind = sys.argv[1:]
work = Path(work_s)
stop = False

def race():
    while not stop:
        try:
            os.unlink(target)
        except OSError:
            pass
        try:
            if kind == "link":
                os.symlink(outside, target)
            else:
                os.mkfifo(target, 0o600)
        except OSError:
            pass

threading.Thread(target=race, daemon=True).start()
task = tasks.Task(id="t", prompt="", test_command=["x"], hidden={"hidden.py": b"HIDDEN\n"},
                  solution={}, sha256="0" * 64)
for _ in range(80):
    try:
        task.write_hidden(work)
    except (OSError, tasks.TaskError):
        pass
stop = True
time.sleep(0.05)
task.write_hidden(work)
info = (work / "hidden.py").lstat()
if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
    raise SystemExit("hidden.py is not a private regular file")
if (work / "hidden.py").read_bytes() != b"HIDDEN\n":
    raise SystemExit("hidden.py does not hold the hidden bytes")
sentinel = Path(outside)
print(f"{sentinel.read_bytes()!r} {sentinel.stat().st_mode & 0o777:o}")
"""


def _end(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


class PlantedNameTests(unittest.TestCase):
    def _outside(self, folder: Path) -> Path:
        sentinel = folder / "outside.txt"
        sentinel.write_bytes(b"KEEP-OUTSIDE\n")
        os.chmod(sentinel, 0o604)
        return sentinel

    def _child(self, work: Path, sentinel: Path, kind: str) -> tuple:
        env = os.environ.copy()
        root = str(REPO_ROOT)
        previous = env.get("PYTHONPATH")
        env["PYTHONPATH"] = root if not previous else root + os.pathsep + previous
        proc = subprocess.Popen(
            [sys.executable, "-B", "-c", _CHILD, str(work / "hidden.py"), str(sentinel), str(work), kind],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                return None, b"", b"timed out"
            return proc.returncode, stdout, stderr
        finally:
            _end(proc)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def _assert_untouched(self, folder: Path, sentinel: Path, code, stdout, stderr) -> None:
        self.assertEqual(code, 0, stderr.decode("utf-8", "replace")[-600:])
        self.assertEqual(sentinel.read_bytes(), b"KEEP-OUTSIDE\n")
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o604)
        self.assertEqual(stdout.decode().strip(), "b'KEEP-OUTSIDE\\n' 604")
        beside = sorted(p.name for p in folder.iterdir())
        self.assertEqual(beside, ["outside.txt", "work"])

    def test_a_planted_symlink_does_not_write_or_chmod_outside_the_work_tree(self):
        from eval.tests import outside_folder
        with outside_folder() as folder:
            work = folder / "work"
            work.mkdir()
            sentinel = self._outside(folder)
            code, stdout, stderr = self._child(work, sentinel, "link")
            self._assert_untouched(folder, sentinel, code, stdout, stderr)

    def test_a_planted_fifo_does_not_block_or_touch_a_file_outside_the_work_tree(self):
        from eval.tests import outside_folder
        with outside_folder() as folder:
            work = folder / "work"
            work.mkdir()
            sentinel = self._outside(folder)
            code, stdout, stderr = self._child(work, sentinel, "fifo")
            self._assert_untouched(folder, sentinel, code, stdout, stderr)


class FinalSummaryTests(unittest.TestCase):
    def grade(self, log: bytes) -> dict:
        from eval.tests import outside_folder
        with outside_folder() as folder:
            work = folder / "work"
            work.mkdir()
            runtime = folder / "runtime"
            runtime.mkdir()
            held = scratch.Scratch("s6", folder, runtime)
            target = tasks.Task(id="s6", prompt="p", test_command=["{python}", "-B", "-c", "pass"],
                                hidden={"test_hidden.py": b"x\n"}, solution={}, sha256="0" * 64)
            done = {"exit": 0, "signal": None, "timed_out": False, "seconds": 0.0,
                    "stdout": b"", "stderr": log}
            with patch.object(runner.arms_mod, "run_process", return_value=done):
                return runner.grade(target, work, held)

    def test_a_nested_skipped_summary_does_not_hide_a_final_pass(self):
        log = (b"Ran 1 test in 0.000s\n\nOK (skipped=1)\n.\n---\n"
               b"Ran 1 test in 0.029s\n\nOK\n")
        result = self.grade(log)
        self.assertEqual(result["verdict"], "resolved")
        self.assertIsNone(result["error"])

    def test_nothing_passed_when_the_only_results_are_a_skip_and_an_expected_failure(self):
        log = b"xs\n---\nRan 2 tests in 0.000s\n\nOK (skipped=1, expected failures=1)\n"
        result = self.grade(log)
        self.assertEqual(result["verdict"], "not_graded")
        self.assertEqual(result["error"], "no_tests_ran")

    def test_one_pass_and_one_skip_stays_a_pass(self):
        log = b"Ran 2 tests in 0.000s\n\nOK (skipped=1)\n"
        result = self.grade(log)
        self.assertEqual(result["verdict"], "resolved")
        self.assertIsNone(result["error"])


if __name__ == "__main__":
    unittest.main()
