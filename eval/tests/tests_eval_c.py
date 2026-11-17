"""EVAL-C witnesses: a finished trial is kept, a short write is not published, and bad inputs are refused
before any attempt.

Scratch bases are folders under the test's own temporary directory. A base under the real runtime
directory is refused, so the evidence path that joins XDG_RUNTIME_DIR would error first.
"""
from __future__ import annotations

import contextlib
import errno
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from eval import arms, record, runner, suite, tasks
from eval.tests import outside_folder, scratch_bases


def interpreter_flags():
    return ["-X", "gil=0", "-B"] if hasattr(sys, "_is_gil_enabled") else ["-X", "utf8", "-B"]


def task_with_hidden():
    task = tasks.Task(
        id="tiny", prompt="x", test_command=["{python}", "test_hidden.py"],
        hidden={"test_hidden.py": b"assert True\n"}, solution={}, sha256="a" * 64, start_files={})
    task.created_at = "2026-11-15T00:00:00Z"
    return task


class StubArm(arms.Arm):
    name = "direct"

    def __init__(self):
        super().__init__("standin")

    def preflight(self):
        pass

    def engine_version(self):
        return "synthetic/1"


def synthetic_run(out, attempt):
    """Schedule a real run whose attempts are the stub. Bases sit under the output folder's parent."""
    root = Path(out).parent
    task = task_with_hidden()
    subject = suite.Suite("tiny", "1", "dev", False, "a" * 64, Path("unused"),
                          None, None, {}, {}, [task])
    with patch.object(runner, "run_attempt", side_effect=attempt):
        return runner.run(subject, [StubArm()], k=1, attempts=2, replacements=0,
                          out_dir=out, tmp_base=root / "attempts-tmp", xdg_base=root / "attempts-xdg",
                          log=lambda _: None)


def completed_stub(ctx, arm, task, index, **kwargs):
    trial = runner.blank_trial(f"{ctx.record_id}.{task.id}.{index}", task.id, index, True, None)
    trial["error"] = {"class": "setup_failed", "tier": record.CLASS_TIER["setup_failed"],
                      "detail": "synthetic completed attempt, no engine"}
    return trial


def crash_worker(root: Path) -> None:
    def attempt(ctx, arm, task, index, **kwargs):
        if index == 2:
            os.kill(os.getpid(), signal.SIGKILL)
        row = completed_stub(ctx, arm, task, index, **kwargs)
        (root / "completed-marker").write_text("attempt one returned\n", encoding="utf-8")
        return row

    synthetic_run(root / "records", attempt)


class KeptWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eval-c-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_F5_completed_trial_is_recoverable_after_sigkill(self):
        proc = subprocess.Popen(
            [sys.executable, *interpreter_flags(), __file__, "--crash-worker", str(self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", "timed out"
        finally:
            if proc.poll() is None:
                try:
                    os.kill(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
        records = list((self.root / "records").glob("**/*")) if (self.root / "records").exists() else []
        print(f"F5 exit={proc.returncode} marker={(self.root / 'completed-marker').exists()} "
              f"files={[p.name for p in records]} stderr={stderr[-400:]!r}", flush=True)
        self.assertEqual(proc.returncode, -signal.SIGKILL, stderr or stdout)
        self.assertTrue((self.root / "completed-marker").exists(), stderr)
        self.assertTrue(any(p.is_file() for p in records), "no result or progress journal for the completed trial")

    def test_F6_short_final_write_does_not_publish_broken_json(self):
        original = Path.write_text

        def short_write(path, data, *args, **kwargs):
            if path.suffix == ".json" and self.root in path.parents:
                with path.open("w", encoding="utf-8") as stream:
                    stream.write(data[:17])
                    stream.flush()
                    raise OSError(errno.ENOSPC, "synthetic full disk")
            return original(path, data, *args, **kwargs)

        error = None
        with patch.object(Path, "write_text", short_write):
            try:
                synthetic_run(self.root / "records", completed_stub)
            except OSError as exc:
                error = exc.errno
        published = list((self.root / "records").glob("**/*.json"))
        invalid = []
        for item in published:
            try:
                json.loads(item.read_text(encoding="utf-8"))
            except ValueError:
                invalid.append(item.read_text(encoding="utf-8"))
        print(f"F6 errno={error} invalid={invalid!r}", flush=True)
        self.assertFalse(invalid, "a final .json path becomes visible with a partial record")


class DuplicateArmTests(unittest.TestCase):
    def test_f01_duplicate_arms_refuse_before_attempts(self):
        task = task_with_hidden()
        subject = suite.Suite("tiny", "1", "dev", False, "a" * 64, Path("unused"),
                              None, None, {}, {}, [task])

        def successful_attempt(ctx, arm, task, index, **kwargs):
            row = runner.blank_trial(f"{ctx.record_id}.{task.id}.{index}", task.id, index, True, None)
            row["grade"].update(verdict="resolved", exit=0)
            row["error"] = {"class": None, "tier": "none", "detail": None}
            return row

        with scratch_bases() as (tmp, xdg):
            with patch.object(runner, "run_attempt", side_effect=successful_attempt) as attempt:
                with self.assertRaises(runner.RunRefused):
                    runner.run(subject, [arms.DirectArm("standin"), arms.DirectArm("standin")],
                               k=1, replacements=0, jobs=2, out_dir=tmp / "records",
                               tmp_base=tmp, xdg_base=xdg, log=lambda _: None)
                attempt.assert_not_called()


class TaskMetadataTests(unittest.TestCase):
    def test_f07_bad_record_metadata_is_rejected_when_loading_task(self):
        with tempfile.TemporaryDirectory(prefix="eval-c-meta-") as folder:
            folder = Path(folder)
            for field in ("cluster", "created_at"):
                for value in ([], 3, ""):
                    with self.subTest(field=field, value=value):
                        meta = {"id": "tiny", "test_command": ["x"], field: value}
                        source = folder / "task.txtar"
                        source.write_text("-- task.json --\n" + json.dumps(meta)
                                          + "\n-- prompt.md --\np\n-- hidden/test_t.py --\n# empty\n",
                                          encoding="utf-8")
                        with self.assertRaises(tasks.TaskError):
                            tasks.load_txtar(source)


class AllArmsRefusedExitTests(unittest.TestCase):
    def test_no_arm_ran_returns_the_documented_pre_attempt_refusal_code_two(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            with patch.object(runner, "run_attempt") as attempted, contextlib.redirect_stdout(io.StringIO()):
                code = runner.main(["--suite", "dev", "--arms", "recursion", "--engine", "standin",
                                    "--tmp-base", str(tmp), "--xdg-base", str(xdg), "--out", str(folder / "out")])
            attempted.assert_not_called()
            self.assertFalse((folder / "out").exists())
            self.assertEqual(code, 2)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--crash-worker":
        crash_worker(Path(sys.argv[2]))
    else:
        unittest.main()
