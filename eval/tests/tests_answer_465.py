"""Answers to the first review of draft #465.

An interrupt must not kill the grading command and then record that kill as the model failing
tests. A reader that already started, blocked because a helper in its own session holds the pipe,
is named on the error that is already propagating. The same note is attached when the wait itself
is interrupted.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import arms, runner, scratch, tasks
from eval.tests.tests_eval_b import SAFE_ENV, dispose, state

_HELPER = """
import os, sys, time
from pathlib import Path
marker = Path(sys.argv[1])
child = os.fork()
if child == 0:
    os.setsid()
    marker.write_text(str(os.getpid()))
    time.sleep(30)
    os._exit(0)
for _ in range(100):
    if marker.exists():
        break
    time.sleep(0.01)
stay = sys.argv[2] == "stay"
if stay:
    time.sleep(30)
os._exit(0)
"""


def _end_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    for _ in range(50):
        if state(pid) is None:
            return
        time.sleep(0.01)


def _notes(exc: BaseException) -> str:
    return "\n".join(getattr(exc, "__notes__", ()))


class GradingInterruptTests(unittest.TestCase):
    def test_an_interrupt_during_grading_does_not_record_the_model_as_failing(self):
        """The runner's cancel must leave the grader to finish, and the trial keeps that result."""
        made = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            made.append(real_popen(*args, **kwargs))
            return made[-1]

        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            xdg_base = Path(tempfile.mkdtemp(prefix="eval-xdg-b-", dir=os.environ.get("TMPDIR") or "/var/tmp"))
            marker = folder / "grader-ready"
            found = []
            task = types.SimpleNamespace(id="one", cluster=None, sha256="0" * 64, created_at=None)
            suite = types.SimpleNamespace(
                split="dev", runner=None, frozen=False, tasks=[task], engines={}, evaluated_commit=None,
                id="s6", version="1", manifest_sha256="0" * 64, task=lambda name: task)
            code = f"from pathlib import Path; import time; Path({str(marker)!r}).write_text('up'); time.sleep(0.8)"

            def attempt(ctx, arm, task, index, **kwargs):
                work = folder / "work"
                work.mkdir(exist_ok=True)
                held = scratch.Scratch("g", folder, folder / "runtime")
                target = tasks.Task(
                    id="one", prompt="p", test_command=[sys.executable, "-B", "-c", code],
                    hidden={"t.py": b"x\n"}, solution={}, sha256="0" * 64, grading_timeout_s=5)
                graded = runner.grade(target, work, held)
                trial = runner.blank_trial(ctx.record_id + ".one.1", "one", 1, True, None)
                runner.classify(trial, graded, types.SimpleNamespace(failure=None, detail=None))
                found.append((graded, trial))
                return trial

            def interrupt():
                for _ in range(200):
                    if marker.exists():
                        os.kill(os.getpid(), signal.SIGINT)
                        return
                    time.sleep(0.005)
                raise AssertionError("grader failed to start")

            interrupter = threading.Thread(target=interrupt)
            try:
                with patch.object(arms.subprocess, "Popen", side_effect=launch), \
                        patch.object(runner, "run_attempt", attempt), \
                        patch.object(runner.record_mod, "require_valid"):
                    interrupter.start()
                    try:
                        result = runner.run(
                            suite, [arms.DirectArm("standin")], k=1, replacements=0,
                            tmp_base=folder / "tmp", xdg_base=xdg_base, out_dir=folder / "out",
                            log=lambda *a: None)
                    finally:
                        interrupter.join(timeout=2)
                self.assertTrue(result.interrupted)
                self.assertEqual(len(found), 1, "the grading worker did not finish")
                graded, trial = found[0]
                self.assertEqual(graded["exit"], 0)
                self.assertEqual(graded["verdict"], "resolved")
                self.assertNotEqual(trial["error"]["class"], "tests_failed")
                self.assertNotEqual(trial["error"]["tier"], "agent")
            finally:
                interrupter.join(timeout=2)
                for process in made:
                    dispose(process)
                import shutil
                shutil.rmtree(xdg_base, ignore_errors=True)


class HeldPipeTests(unittest.TestCase):
    def _helper(self, stay: bool):
        folder = Path(tempfile.mkdtemp(prefix="eval-held-"))
        pidfile = folder / "helper.pid"
        command = [sys.executable, "-B", "-c", _HELPER, str(pidfile), "stay" if stay else "exit"]
        return folder, pidfile, command

    def _stop_helper(self, pidfile: Path, folder: Path) -> None:
        if pidfile.exists():
            text = pidfile.read_text().strip()
            if text.isdigit():
                _end_pid(int(text))
        import shutil
        shutil.rmtree(folder, ignore_errors=True)

    def test_a_started_reader_is_named_when_a_helper_holds_the_pipe(self):
        folder, pidfile, command = self._helper(stay=False)
        real_start = arms.threading.Thread.start

        def start(thread, *args, **kwargs):
            if thread.name == "stdin":
                time.sleep(0.4)
                raise RuntimeError("synthetic thread exhaustion")
            return real_start(thread, *args, **kwargs)

        try:
            with patch.object(arms, "CLEANUP_GRACE_S", 0.15), \
                    patch.object(arms.threading.Thread, "start", start):
                with self.assertRaises(RuntimeError) as caught:
                    arms.run_process(command, cwd=folder, env=SAFE_ENV, stdin=None, timeout_s=2)
            note = _notes(caught.exception)
            self.assertIn("still open", note)
            self.assertIn("stdout", note)
            self.assertIn("stderr", note)
        finally:
            self._stop_helper(pidfile, folder)

    def test_an_interrupted_wait_names_a_helper_that_still_holds_the_pipe(self):
        folder, pidfile, command = self._helper(stay=True)
        original_wait = subprocess.Popen.wait
        first = True

        def interrupted_wait(process, timeout=None):
            nonlocal first
            if first:
                first = False
                for _ in range(50):
                    if pidfile.exists():
                        break
                    time.sleep(0.01)
                raise KeyboardInterrupt("synthetic interruption of wait")
            return original_wait(process, timeout=timeout)

        try:
            with patch.object(arms, "CLEANUP_GRACE_S", 0.15), \
                    patch.object(subprocess.Popen, "wait", interrupted_wait):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    arms.run_process(command, cwd=folder, env=SAFE_ENV, stdin=None, timeout_s=2)
            note = _notes(caught.exception)
            self.assertIn("still open", note)
            self.assertIn("stdout", note)
        finally:
            self._stop_helper(pidfile, folder)


class CallerRefusalTests(unittest.TestCase):
    def test_an_empty_command_is_refused_before_a_process_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(arms.EngineMissing):
                arms.run_process([], cwd=Path(folder), env=SAFE_ENV, stdin=None, timeout_s=1)

    def test_text_stdin_is_refused_before_a_process_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                arms.run_process([sys.executable, "-B", "-c", "print(0)"], cwd=Path(folder), env=SAFE_ENV,
                                 stdin="text", timeout_s=1)


if __name__ == "__main__":
    unittest.main()
