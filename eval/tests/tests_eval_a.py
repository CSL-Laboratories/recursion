"""EVAL-A witnesses: finite deadlines, writes that stay inside the work tree, cleanup of both
scratch folders, and a hidden suite that skips every test.

Copied from the triage patches for E1, E2, E3, E23 and E26. Each one fails on the base for the
reason that entry states.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import arms, runner, scratch, tasks, txtar, validate
from eval.tests import outside_folder, scratch_bases, tiny_task


class FiniteTaskTimeoutTests(unittest.TestCase):
    def test_both_task_time_limits_refuse_nonfinite_json_numbers(self):
        with outside_folder() as folder:
            comment, files = txtar.parse(tiny_task("t", ["solve"], timeout=2))
            for field in ("timeout_s", "grading_timeout_s"):
                for literal in ("NaN", "Infinity", "-Infinity", "1e400"):
                    with self.subTest(field=field, literal=literal):
                        metadata = json.loads(files["task.json"])
                        metadata[field] = 2
                        raw = json.dumps(metadata).replace('"' + field + '": 2', '"' + field + '": ' + literal)
                        text = txtar.format(comment, dict(files, **{"task.json": raw + "\n"}))
                        path = folder / "nonfinite.txtar"
                        path.write_text(text, encoding="utf-8")
                        # This only loads the task: no infinite wait or process is started.
                        with self.assertRaises(tasks.TaskError):
                            tasks.load_txtar(path)

    def test_a_huge_integer_time_limit_is_a_task_error(self):
        # 10**400 cannot be a float; loading must refuse it rather than raise OverflowError.
        for field in ("timeout_s", "grading_timeout_s"):
            with self.subTest(field=field):
                meta = {"id": "t", "test_command": ["x"], field: 10 ** 400}
                with self.assertRaises(tasks.TaskError):
                    tasks._meta("t", meta)

    def test_a_nonfinite_wait_is_refused_before_a_process_starts(self):
        made = []
        real_popen = __import__("subprocess").Popen

        def launch(*args, **kwargs):
            made.append(real_popen(*args, **kwargs))
            return made[-1]

        def expired(*_args):
            raise TimeoutError("non-finite wait was not refused within one second")

        with outside_folder() as folder:
            old = signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, 1)
            try:
                with patch.object(arms.subprocess, "Popen", side_effect=launch):
                    with self.assertRaises(ValueError):
                        arms.run_process([sys.executable, "-B", "-c", "import time; time.sleep(5)"],
                                         cwd=folder, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                                         stdin=None, timeout_s=float("nan"))
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old)
                for process in made:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)
            self.assertEqual(made, [])


class HardLinkedOutputTests(unittest.TestCase):
    def test_hidden_test_overwrite_does_not_change_a_file_outside_the_work_tree(self):
        with outside_folder() as folder:
            outside = folder / "outside.txt"
            outside.write_bytes(b"KEEP")
            work = folder / "work"
            work.mkdir()
            os.link(outside, work / "hidden.py")
            task = tasks.Task(id="t", prompt="", test_command=["x"], hidden={"hidden.py": b"HIDDEN\n"},
                              solution={}, sha256="0" * 64)
            task.write_hidden(work)
            self.assertEqual((work / "hidden.py").read_bytes(), b"HIDDEN\n")
            self.assertEqual(outside.read_bytes(), b"KEEP")

    def test_reference_write_does_not_change_a_hard_link_target_outside_the_work_tree(self):
        with outside_folder() as folder:
            outside = folder / "outside.txt"
            outside.write_bytes(b"KEEP")
            work = folder / "work"
            work.mkdir()
            os.link(outside, work / "answer.py")
            tasks.apply_files(work, {"answer.py": b"SOLUTION\n"})
            self.assertEqual((work / "answer.py").read_bytes(), b"SOLUTION\n")
            self.assertEqual(outside.read_bytes(), b"KEEP")


class HiddenPathInstallTests(unittest.TestCase):
    def test_F3_hidden_test_fifo_returns_a_refusal_without_waiting(self):
        """A FIFO where a hidden test is written must not block. Opening it for write waits for a reader,
        and that wait starts before the grading deadline."""
        def expired(*_args):
            raise TimeoutError("file operation did not return within one second")

        with outside_folder() as folder:
            work = folder / "work"
            work.mkdir()
            os.mkfifo(work / "test_hidden.py", 0o600)
            task = tasks.Task(id="tiny", prompt="x", test_command=["{python}", "test_hidden.py"],
                              hidden={"test_hidden.py": b"assert True\n"}, solution={}, sha256="a" * 64,
                              start_files={})
            old = signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, 1)
            timed_out = False
            try:
                try:
                    task.write_hidden(work)
                except TimeoutError:
                    timed_out = True
                except (tasks.TaskError, OSError):
                    pass
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old)
            self.assertFalse(timed_out, "hidden-file installation blocks before the grader timeout starts")


class CleanupFailureTests(unittest.TestCase):
    def test_an_error_removing_tmp_does_not_skip_removing_the_runtime_folder(self):
        with scratch_bases() as (tmp, xdg):
            real_remove = scratch._rmtree

            def remove(path):
                if path == tmp / "cleanup-error":
                    raise OSError("synthetic TMPDIR deletion failure")
                return real_remove(path)

            with patch.object(scratch, "_rmtree", side_effect=remove):
                with self.assertRaisesRegex(OSError, "synthetic TMPDIR deletion failure"):
                    with scratch.attempt("cleanup-error", tmp, xdg) as sc:
                        (sc.xdg_runtime_dir / "runtime-marker").write_bytes(b"synthetic")
            self.assertFalse((xdg / "cleanup-error").exists(), "the second cleanup was never attempted")

    def test_F16_cleanup_preserves_interrupt_and_attempts_both(self):
        calls = []
        real_remove = scratch._rmtree

        def fault(path):
            calls.append(path)
            if path.name == "tiny-cleanup" and "eval-tmp-" in str(path.parent):
                raise PermissionError("injected cleanup fault")
            return real_remove(path)

        with scratch_bases() as (tmp, xdg):
            with patch.object(scratch, "_rmtree", side_effect=fault):
                with self.assertRaises(KeyboardInterrupt):
                    with scratch.attempt("tiny-cleanup", tmp, xdg):
                        raise KeyboardInterrupt("operator cancellation")
            self.assertEqual(len(calls), 2)
            self.assertFalse((xdg / "tiny-cleanup").exists())
            self.assertTrue((tmp / "tiny-cleanup").exists())


class AllSkippedGradeTests(unittest.TestCase):
    def test_F5_all_skipped_unittest_is_not_a_resolved_task(self):
        code = ("import unittest\n"
                "@unittest.skip('held')\n"
                "class Hidden(unittest.TestCase):\n"
                "    def test_answer(self):\n"
                "        self.fail('unchanged solution')\n")
        with tempfile.TemporaryDirectory(prefix="s6-grade-") as temp:
            root = Path(temp)
            work = root / "work"
            work.mkdir()
            runtime = root / "runtime"
            runtime.mkdir()
            held = scratch.Scratch("s6", root, runtime)
            target = tasks.Task(id="s6", prompt="fix the answer",
                                test_command=["{python}", "-B", "-m", "unittest", "test_hidden"],
                                hidden={"test_hidden.py": code.encode()}, solution={}, sha256="0" * 64)
            # Captured from real unittest: exit 0, one collected test, every test skipped.
            done = {"exit": 0, "signal": None, "timed_out": False, "seconds": 0.0,
                    "stdout": b"",
                    "stderr": b"s\nRan 1 test in 0.000s\nOK (skipped=1)\n"}
            with patch.object(runner.arms_mod, "run_process", return_value=done):
                result = runner.grade(target, work, held)
            self.assertNotEqual(result["verdict"], "resolved")

    def test_all_skipped_hidden_suite_is_not_kept_as_a_passing_parent(self):
        task = tasks.Task(id="s6", prompt="p", test_command=["x"], hidden={"t.py": b"#\n"}, solution={},
                          sha256="0" * 64, start_files={})
        log = b"s\nRan 1 test in 0.000s\nOK (skipped=1)\n"
        with patch.object(validate, "_run", return_value=("a" * 40, 0, False, log)):
            result = validate.validate(task, reruns=1)
        self.assertFalse(result.ok)
        self.assertIn("ran no test", result.reason or "")


if __name__ == "__main__":
    unittest.main()
