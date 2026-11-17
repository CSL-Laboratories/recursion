"""EVAL-B witnesses: the runner owns the engine until the process is gone, and a success line
does not hide a bad exit.

Adapted from the proc-lifecycle evidence. The subreaper flag is not set: the engine here is a
direct child and each test reaps it by pid. Scratch bases are private folders, because a base
under the real runtime directory is refused.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from eval import arms, runner, scratch, tasks
from eval.tests import scratch_bases

_TMP = os.environ.get("TMPDIR") or "/var/tmp/grok-eval"
for _name in ("TMPDIR", "TEMP", "TMP"):
    os.environ.setdefault(_name, _TMP)
if not os.environ.get("XDG_RUNTIME_DIR"):
    os.environ["XDG_RUNTIME_DIR"] = _TMP
SAFE_ENV = {key: os.environ[key] for key in ("PATH", "TMPDIR", "TEMP", "TMP", "XDG_RUNTIME_DIR")}


def state(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError, OSError):
        return None


def dispose(process):
    """End this test's child by its own pid, then close the pipes it was given."""
    if process.poll() is None:
        try:
            os.kill(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


class LifecycleWitnesses(unittest.TestCase):
    def test_F1_runner_interrupt_cancels_running_engine(self):
        """Real SIGINT to the runner; the active engine is in its own session in a worker."""
        made = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            made.append(real_popen(*args, **kwargs))
            return made[-1]

        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            xdg_base = Path(tempfile.mkdtemp(prefix="eval-xdg-b-", dir=_TMP))
            marker = folder / "engine-ready"
            sent = []
            worker_done = []
            task = types.SimpleNamespace(id="one", cluster=None, sha256="0" * 64, created_at=None)
            suite = types.SimpleNamespace(
                split="dev", runner=None, frozen=False, tasks=[task], engines={}, evaluated_commit=None,
                id="s6", version="1", manifest_sha256="0" * 64, task=lambda name: task)
            code = f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(.8)"

            def attempt(ctx, arm, task, index, **kwargs):
                arms.run_process([sys.executable, "-B", "-c", code], cwd=folder, env=SAFE_ENV,
                                 stdin=None, timeout_s=2)
                worker_done.append(time.monotonic())
                return runner.blank_trial(ctx.record_id + ".one.1", "one", 1, True, None)

            def interrupt():
                for _ in range(150):
                    if marker.exists():
                        sent.append(time.monotonic())
                        os.kill(os.getpid(), signal.SIGINT)
                        return
                    time.sleep(.005)
                raise AssertionError("engine failed to start")

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
                delay = time.monotonic() - sent[0]
                print(f"F1 interrupt-to-return={delay:.3f}s; worker finished={bool(worker_done)}", flush=True)
                self.assertTrue(result.interrupted)
                self.assertLess(delay, .4, "SIGINT waits for the active engine instead of cancelling it")
            finally:
                for process in made:
                    dispose(process)
                shutil.rmtree(xdg_base, ignore_errors=True)

    def test_F3_thread_start_failure_closes_and_reaps_engine(self):
        made = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            made.append(real_popen(*args, **kwargs))
            return made[-1]

        with tempfile.TemporaryDirectory() as folder:
            try:
                with patch.object(arms.subprocess, "Popen", side_effect=launch), \
                        patch.object(arms.threading.Thread, "start",
                                     side_effect=RuntimeError("synthetic thread exhaustion")):
                    with self.assertRaisesRegex(RuntimeError, "thread exhaustion"):
                        arms.run_process([sys.executable, "-B", "-c", "import time; time.sleep(.8)"],
                                         cwd=Path(folder), env=SAFE_ENV, stdin=None, timeout_s=.1)
                process = made[0]
                # /proc only: poll() would reap the child and hide a missing reap.
                observed = state(process.pid)
                print(f"F3 child state={observed}; stdin closed={process.stdin.closed}", flush=True)
                self.assertIsNone(observed, "launched child is lost before the protected wait")
                self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))
            finally:
                for process in made:
                    dispose(process)

    def test_F4_stale_group_id_must_not_be_signalled(self):
        # The leader was already reaped and its numeric id may now be some other process group.
        # Every signal is mocked: the witness itself signals nothing.
        with patch.object(arms.os, "killpg") as killpg, \
                patch.object(arms, "_session_members", side_effect=[[], []]), \
                patch.object(arms.time, "sleep"):
            arms._kill_session(987654321)
        print(f"F4 unvalidated stale-group signals={killpg.call_args_list}", flush=True)
        self.assertFalse(killpg.called, "signals an unanchored recycled group before checking ownership")

    def test_F4b_pid_reused_after_session_scan_must_not_be_signalled(self):
        foreign = set()
        sent_to_foreign = []

        def scan(sid):
            if not foreign:
                foreign.add(987654322)  # the process exits and the pid is reused after this scan
                return [987654322]
            return []

        def kill(pid, sig):
            if pid in foreign:
                sent_to_foreign.append((pid, sig))

        with patch.object(arms.os, "killpg"), patch.object(arms.os, "kill", side_effect=kill), \
                patch.object(arms, "_session_members", side_effect=scan), patch.object(arms.time, "sleep"):
            arms._kill_session(987654321)
        print(f"F4b signals to reused pid={sent_to_foreign}", flush=True)
        self.assertEqual(sent_to_foreign, [], "numeric PID from stale /proc snapshot is not identity-safe")

    def test_F5_interrupt_path_reaps_engine_before_reraising(self):
        made = []
        original_popen = subprocess.Popen
        original_wait = subprocess.Popen.wait
        first = True

        def launch(*args, **kwargs):
            made.append(original_popen(*args, **kwargs))
            return made[-1]

        def interrupted_wait(process, timeout=None):
            nonlocal first
            if first:
                first = False
                raise KeyboardInterrupt("synthetic interruption of wait")
            return original_wait(process, timeout=timeout)

        with tempfile.TemporaryDirectory() as folder:
            try:
                with patch.object(arms.subprocess, "Popen", side_effect=launch), \
                        patch.object(original_popen, "wait", interrupted_wait):
                    try:
                        arms.run_process([sys.executable, "-B", "-c", "import time; time.sleep(.8)"],
                                         cwd=Path(folder), env=SAFE_ENV, stdin=None, timeout_s=1)
                    except KeyboardInterrupt as exc:
                        # Keep the traceback so the Popen stays alive and a zombie is still visible.
                        retained = exc
                child = made[0]
                observed = state(child.pid)
                print(f"F5 child after interruption: state={observed}, returncode={child.returncode}", flush=True)
                self.assertIsNotNone(child.returncode, "interruption kills but never waits/reaps")
                self.assertIsNone(observed, "interrupted child remains a zombie while traceback is retained")
                del retained
            finally:
                for process in made:
                    dispose(process)


class DeadlineWitnesses(unittest.TestCase):
    def test_F8_result_success_does_not_mask_signal_or_error_exit(self):
        observations = []
        with scratch_bases() as (tmp, xdg), scratch.attempt("terminal-status", tmp, xdg) as sc:
            work = sc.tmpdir / "work"
            work.mkdir()
            task = tasks.Task(id="status", prompt="hello", test_command=["{python}", "-c", "pass"],
                              hidden={"test_x.py": b""}, solution={}, sha256="0" * 64, timeout_s=2)
            for tail in ("raise SystemExit(7)", "import os,signal; os.kill(os.getpid(),signal.SIGTERM)"):
                code = ('import json\nprint(json.dumps({"type":"result","subtype":"success","is_error":False}),'
                        "flush=True)\n" + tail)
                arm = arms.DirectArm("standin")
                with patch.object(arm, "argv", return_value=[sys.executable, "-B", "-c", code]):
                    result = arm.run(task, work, sc, 1)
                observations.append((result.exit, result.signal, result.claimed_done, result.failure))
        print(f"F8 (exit,signal,claimed_done,failure)={observations}", flush=True)
        self.assertTrue(all(row[3] == "engine_crash" for row in observations),
                        "an engine terminating abnormally is recorded without a failure")

    def test_F8b_version_probe_does_not_accept_an_error_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / "version-fixture"
            fake.write_text(f"#!{sys.executable} -B\nprint('fixture-version')\nraise SystemExit(7)\n",
                            encoding="utf-8")
            fake.chmod(0o700)
            arm = arms.DirectArm("claude_code", model="fixture", claude=arms.ClaudeCodeSettings(binary=str(fake)))
            version = arm.engine_version()
            print(f"F8b failed --version accepted as {version!r}", flush=True)
            self.assertIsNone(version, "a failed version command must not satisfy an engine pin")


if __name__ == "__main__":
    unittest.main()
