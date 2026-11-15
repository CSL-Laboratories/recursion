"""Answers to the two third-round reads of #381 (GPT-6 Astra, GPT-6.1 Sol), one class per finding.

Each reviewer's failing witness is kept here in its own words where it could be (Sol's race and live
leader, Astra's live sleeper, fake process and held pipe), beside a test per rule added in answer. No real
engine, account, credential or settings file of the operator's is used: every settings file is a newly
written fixture in a scratch folder, and the machine's managed-policy paths are pointed at scratch.
"""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import REPO_ROOT, arms, runner, scratch, tasks
from eval import suite as suite_mod
from eval.tests import make_suite, outside_folder, scratch_bases, tiny_task

EFFORT_LOW = json.dumps({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "low"}})
SLEEPER = "import time; time.sleep(1.5)"


def _kill_marked(*markers):
    for marker in markers:
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


class CleanupReportedAtOnceTests(unittest.TestCase):
    """Astra 2 and Sol 2: a cleanup failure is raised without waiting for the engine or its pipes."""

    def test_cleanup_failure_does_not_wait_for_a_live_engine(self):
        # Astra's witness: the sleeper ends by itself after 1.5 s; the error must come well before.
        with scratch_bases() as (tmp, xdg), scratch.attempt("cleanup-bound", tmp, xdg) as sc:
            started = time.monotonic()
            with patch.object(arms, "_kill_session", side_effect=arms.SessionCleanupError("synthetic deadline")):
                with self.assertRaises(arms.SessionCleanupError):
                    arms.run_process([sys.executable, "-B", "-c", SLEEPER], cwd=sc.tmpdir,
                                     env=scratch.environment(sc), stdin=None, timeout_s=0.02)
            self.assertLess(time.monotonic() - started, 0.75)

    def test_cleanup_error_path_never_waits_without_a_timeout(self):
        # Astra's fake process: an unbounded wait fails the test at once.
        class Process:
            pid = 987654321
            returncode = None
            stdin, stdout, stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

            def wait(self, timeout=None):
                if timeout is None:
                    raise AssertionError("unbounded process.wait after the cleanup deadline")
                raise subprocess.TimeoutExpired("synthetic", timeout)

        with patch.object(arms.subprocess, "Popen", return_value=Process()), \
                patch.object(arms, "_kill_session", side_effect=arms.SessionCleanupError("synthetic deadline")):
            with self.assertRaises(arms.SessionCleanupError):
                arms.run_process(["synthetic"], cwd=Path(os.environ.get("TMPDIR", "/tmp")), env={}, stdin=None,
                                 timeout_s=0.02)

    def test_cleanup_error_does_not_block_behind_a_live_leader(self):
        # Sol's witness, in a separate harness so a regression cannot hang this suite: the leader sleeps
        # 60 s; the harness must report the cleanup error within 2 s.
        with outside_folder() as folder:
            marker = folder / "leader"
            program = (
                "import os,subprocess,sys\nfrom pathlib import Path\nfrom unittest.mock import patch\n"
                "from eval import arms\nreal=subprocess.Popen\n"
                "def launch(*a,**kw):\n p=real(*a,**kw)\n"
                f" Path({str(marker)!r}).write_text(str(p.pid))\n return p\n"
                "with patch.object(arms.subprocess,'Popen',side_effect=launch), "
                "patch.object(arms,'_kill_session',side_effect=arms.SessionCleanupError('deadline expired')):\n"
                " try:\n  arms.run_process([sys.executable,'-B','-c','import time; time.sleep(60)'],"
                f"cwd=Path({str(folder)!r}),env=dict(os.environ),stdin=None,timeout_s=.02)\n"
                " except arms.SessionCleanupError:\n  print('reported',flush=True)\n")
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
            harness = subprocess.Popen([sys.executable, "-B", "-c", program], cwd=folder, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            blocked = False
            try:
                try:
                    out, err = harness.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    blocked = True
            finally:
                if marker.exists():
                    try:
                        os.killpg(int(marker.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if harness.poll() is None:
                    try:
                        out, err = harness.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(harness.pid, signal.SIGKILL)
                        out, err = harness.communicate()
            self.assertFalse(blocked, "the cleanup error waited behind a live leader")
            self.assertEqual(harness.returncode, 0, err.decode())
            self.assertIn(b"reported", out)

    def test_cleanup_failure_is_not_held_up_by_a_pipe_a_helper_keeps(self):
        # Astra's witness: the engine exits at once, a helper keeps its pipes for 1.5 s, the drain join is
        # cut short; closing a pipe a reader is blocked on would wait for the helper.
        engine = ("import subprocess,sys; subprocess.Popen([sys.executable,'-B','-c','import time; "
                  "time.sleep(1.5)'],stdin=subprocess.DEVNULL)")
        real_join = arms.threading.Thread.join
        with scratch_bases() as (tmp, xdg), scratch.attempt("pipe-cleanup", tmp, xdg) as sc:
            started = time.monotonic()
            with patch.object(arms, "_kill_session", side_effect=arms.SessionCleanupError("synthetic deadline")), \
                    patch.object(arms.threading.Thread, "join", lambda thread, timeout=None: real_join(thread, 0.01)):
                with self.assertRaises(arms.SessionCleanupError):
                    arms.run_process([sys.executable, "-B", "-c", engine], cwd=sc.tmpdir,
                                     env=scratch.environment(sc), stdin=None, timeout_s=2)
            self.assertLess(time.monotonic() - started, 0.75)

    def test_a_leader_not_reaped_after_an_empty_session_is_a_cleanup_failure(self):
        class Process:
            pid = 987654322
            returncode = None
            stdin, stdout, stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

            def wait(self, timeout=None):
                if timeout is None:
                    raise AssertionError("unbounded process.wait")
                raise subprocess.TimeoutExpired("synthetic", timeout)

        with patch.object(arms.subprocess, "Popen", return_value=Process()), patch.object(arms, "_kill_session"), \
                patch.object(arms, "CLEANUP_GRACE_S", 0.05):
            with self.assertRaisesRegex(arms.SessionCleanupError, "not reaped"):
                arms.run_process(["synthetic"], cwd=Path(os.environ.get("TMPDIR", "/tmp")), env={}, stdin=None,
                                 timeout_s=0.02)

    def test_output_held_by_a_process_outside_the_session_is_reported(self):
        # The engine starts a helper in a session of its own that keeps stdout, and exits. The engine's
        # session empties, but its output never ends: that is a cleanup failure, not a finished run.
        with scratch_bases() as (tmp, xdg), scratch.attempt("pipe-outside", tmp, xdg) as sc:
            marker = sc.tmpdir / "outside"
            engine = ("import subprocess,sys\n"
                      "p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(5)'],"
                      "stdin=subprocess.DEVNULL,start_new_session=True)\n"
                      f"open({str(marker)!r},'w').write(str(p.pid))\n")
            try:
                started = time.monotonic()
                with patch.object(arms, "CLEANUP_GRACE_S", 0.3):
                    with self.assertRaisesRegex(arms.SessionCleanupError, "stdout"):
                        arms.run_process([sys.executable, "-B", "-c", engine], cwd=sc.tmpdir,
                                         env=scratch.environment(sc), stdin=None, timeout_s=10)
                self.assertLess(time.monotonic() - started, 4)
            finally:
                _kill_marked(marker)

    def test_the_reader_threads_close_the_pipes_they_read(self):
        made = []
        real = subprocess.Popen

        def launch(*args, **kwargs):
            made.append(real(*args, **kwargs))
            return made[-1]

        with scratch_bases() as (tmp, xdg), scratch.attempt("pipes-closed", tmp, xdg) as sc, \
                patch.object(arms.subprocess, "Popen", side_effect=launch):
            done = arms.run_process([sys.executable, "-B", "-c", "print('out')"], cwd=sc.tmpdir,
                                    env=scratch.environment(sc), stdin=b"", timeout_s=10)
        self.assertEqual(done["stdout"].strip(), b"out")
        self.assertTrue(made[0].stdout.closed and made[0].stderr.closed and made[0].stdin.closed)

    def test_an_interruption_still_empties_the_session_and_is_what_is_raised(self):
        class Process:
            pid = 987654323
            returncode = None
            stdin, stdout, stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

            def wait(self, timeout=None):
                raise KeyboardInterrupt

        with patch.object(arms.subprocess, "Popen", return_value=Process()), \
                patch.object(arms, "_kill_session", side_effect=arms.SessionCleanupError("synthetic")) as kill:
            with self.assertRaises(KeyboardInterrupt) as caught:
                arms.run_process(["synthetic"], cwd=Path(os.environ.get("TMPDIR", "/tmp")), env={}, stdin=None,
                                 timeout_s=1)
        kill.assert_called_once_with(987654323)
        self.assertIn("cleanup after the interruption", " ".join(getattr(caught.exception, "__notes__", [])))


class ConfirmedEmptyScanTests(unittest.TestCase):
    """Sol 1: an empty scan is confirmed by a fresh one before cleanup returns."""

    def test_an_empty_scan_is_confirmed_and_a_member_found_then_is_killed(self):
        scans = [[], [910001], [], []]
        with patch.object(arms.os, "killpg"), patch.object(arms.os, "kill") as kill, \
                patch.object(arms, "_session_members", side_effect=scans) as members, \
                patch.object(arms.time, "sleep"):
            arms._kill_session(os.getsid(0) + 100000)
        self.assertEqual([c.args for c in kill.call_args_list], [(910001, signal.SIGKILL)])
        self.assertEqual(members.call_count, 4, "a member found after an empty scan restarts the confirmation")

    def test_a_helper_exiting_during_a_scan_does_not_hide_its_child(self):
        # Sol's witness: the real /proc sampler is delayed while a real helper forks and exits, after the
        # listing that could not yet show the child.
        with scratch_bases() as (tmp, xdg), scratch.attempt("vanishing-parent", tmp, xdg) as sc:
            ready, trigger, marker = (sc.tmpdir / n for n in ("ready", "trigger", "child"))
            helper = ("import os,subprocess,sys,time\nfrom pathlib import Path\nos.setpgid(0,0)\n"
                      f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
                      f"while not Path({str(trigger)!r}).exists(): time.sleep(.001)\n"
                      "p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(60)'],"
                      "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                      f"Path({str(marker)!r}).write_text(str(p.pid))\n")
            engine = ("import subprocess,sys,time\nfrom pathlib import Path\n"
                      f"subprocess.Popen([sys.executable,'-B','-c',{helper!r}],stdin=subprocess.DEVNULL,"
                      "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                      f"while not Path({str(ready)!r}).exists(): time.sleep(.001)\n")
            original = arms._session_of
            fired = []

            def disappear(pid):
                if ready.exists() and not fired and pid == int(ready.read_text()):
                    fired.append((pid, original(pid)[0]))
                    trigger.write_text("go")
                    until = time.monotonic() + 5
                    while time.monotonic() < until:
                        state = original(pid)
                        if marker.exists() and (state is None or state[1] in ("Z", "X")):
                            break
                        time.sleep(0.001)
                    else:
                        raise AssertionError("helper did not exit")
                return original(pid)

            try:
                with patch.object(arms, "_session_of", side_effect=disappear):
                    arms.run_process([sys.executable, "-B", "-c", engine], cwd=sc.tmpdir,
                                     env=scratch.environment(sc), stdin=None, timeout_s=10)
                self.assertTrue(fired)
                state = original(int(marker.read_text()))
                self.assertIn(state[1] if state else None, (None, "Z", "X"),
                              "cleanup returned while a child of the session was alive")
            finally:
                _kill_marked(marker, ready)


class SettingsFileTests(unittest.TestCase):
    """Astra 1 (and the effort half of Sol 4): no Claude Code settings file may be in effect."""

    def setUp(self):
        self._folder = outside_folder()
        self.folder = self._folder.__enter__()
        self.managed = self.folder / "managed"
        patches = [patch.object(arms, "MANAGED_SETTINGS", (self.managed / "managed-settings.json",
                                                           self.managed / "managed-settings.d")),
                   patch.object(arms.shutil, "which", return_value="/synthetic/claude")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        self._folder.__exit__(None, None, None)

    def arm(self, **claude):
        claude.setdefault("binary", "synthetic-never-launched")
        claude.setdefault("permission_mode", "acceptEdits")
        return arms.DirectArm("claude_code", model="synthetic", route="synthetic-route", effort="high",
                              claude=arms.ClaudeCodeSettings(**claude))

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_route_home_settings_refuse_the_arm(self):
        home = self.folder / "home"
        settings = self.write(home / ".claude" / "settings.json", EFFORT_LOW)
        with self.assertRaisesRegex(arms.ArmRefused, "settings in effect.*" + str(settings)):
            self.arm(home=str(home)).preflight()

    def test_passed_config_dir_settings_refuse_the_arm(self):
        config = self.folder / "config"
        self.write(config / "settings.json", EFFORT_LOW)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            with self.assertRaisesRegex(arms.ArmRefused, "config/settings.json"):
                self.arm(pass_env=["CLAUDE_CONFIG_DIR"]).preflight()
            self.arm().preflight()   # not passed: the engine does not read that folder

    def test_managed_policy_refuses_the_arm(self):
        self.write(self.managed / "managed-settings.json", json.dumps({"model": "other"}))
        with self.assertRaisesRegex(arms.ArmRefused, "managed-settings.json"):
            self.arm().preflight()

    def test_managed_drop_in_refuses_the_arm(self):
        drop_in = self.managed / "managed-settings.d"
        self.write(drop_in / "00-empty.json", "{}")
        self.write(drop_in / "README", "not a settings file")
        self.arm().preflight()   # a folder of files that set nothing (and a non-.json file) is no setting
        self.write(drop_in / "10-team.json", json.dumps({"effortLevel": "low"}))
        with self.assertRaisesRegex(arms.ArmRefused, "10-team.json"):
            self.arm().preflight()

    def test_what_sets_nothing_is_allowed_and_what_cannot_be_read_refuses(self):
        home = self.folder / "home"
        path = self.write(home / ".claude" / "settings.json", "{}")
        self.arm(home=str(home)).preflight()
        path.write_text(json.dumps({"$schema": "https://json.schemastore.org/claude-code-settings.json"}))
        self.arm(home=str(home)).preflight()
        path.write_text("{not json")
        with self.assertRaises(arms.ArmRefused):
            self.arm(home=str(home)).preflight()
        path.write_text("[]")
        with self.assertRaises(arms.ArmRefused):
            self.arm(home=str(home)).preflight()
        if os.geteuid() != 0:
            path.write_text("{}")
            path.chmod(0)
            try:
                with self.assertRaises(arms.ArmRefused):
                    self.arm(home=str(home)).preflight()
            finally:
                path.chmod(0o600)

    def test_a_task_carrying_project_settings_refuses_the_arm_before_any_attempt(self):
        with scratch_bases() as (tmp, xdg):
            for name in (".claude/settings.json", ".claude/settings.local.json"):
                text = tiny_task("t", ["solve"]).replace(
                    "-- start/tiny/__init__.py --", f"-- start/{name} --\n{EFFORT_LOW}\n-- start/tiny/__init__.py --")
                loaded = suite_mod.load_dev(make_suite(self.folder / name.replace("/", "_"), {"t": text},
                                                       tmp_base=tmp, xdg_base=xdg))
                arm = self.arm()
                with patch.object(arm, "engine_version", return_value="synthetic 1"), \
                        patch.object(arms, "run_process", side_effect=AssertionError("engine started")):
                    out = runner.run(loaded, [arm], k=1, replacements=0, tmp_base=tmp, xdg_base=xdg,
                                     out_dir=self.folder / "out", log=lambda message: None)
                self.assertFalse(out.records)
                self.assertIn(f"task t: {name}", out.refused["direct"])

    def test_a_harvested_start_tree_is_read_for_project_settings(self):
        archive = self.folder / "start.tar"
        with tarfile.open(archive, "w") as tar:
            data = EFFORT_LOW.encode()
            info = tarfile.TarInfo(".claude/settings.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo(".claude/settings.local.json")
            link.type = tarfile.SYMTYPE
            link.linkname = "settings.json"
            tar.addfile(link)
        task = tasks.Task(id="h", prompt="p", test_command=[], hidden={}, solution={}, sha256="0" * 64,
                          start_tar=archive)
        self.assertEqual(task.start_file(".claude/settings.json"), EFFORT_LOW.encode())
        self.assertIsNone(task.start_file("absent.txt"))
        with self.assertRaises(tasks.TaskError):
            task.start_file(".claude/settings.local.json")
        with self.assertRaisesRegex(arms.ArmRefused, "task h: .claude/settings.json; task h: .claude/settings.local"):
            self.arm().check_tasks([task])
        arms.DirectArm("standin").check_tasks([task])   # the stand-in loads no settings

    def test_settings_appearing_after_the_checks_stop_the_attempt_as_an_infra_failure(self):
        config = self.folder / "config"
        config.mkdir()
        arm = self.arm(pass_env=["CLAUDE_CONFIG_DIR"])

        def version():
            # After preflight and the task check, before the first attempt.
            self.write(config / "settings.json", EFFORT_LOW)
            return "synthetic 1"

        with scratch_bases() as (tmp, xdg), patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            loaded = suite_mod.load_dev(make_suite(self.folder / "s", {"t": tiny_task("t", ["solve"])},
                                                   tmp_base=tmp, xdg_base=xdg))
            with patch.object(arm, "engine_version", side_effect=version), \
                    patch.object(arms, "run_process", side_effect=AssertionError("engine started")):
                out = runner.run(loaded, [arm], k=1, replacements=0, tmp_base=tmp, xdg_base=xdg,
                                 out_dir=self.folder / "out", log=lambda message: None)
        record = json.loads(Path(out.records["direct"]).read_text(encoding="utf-8"))
        self.assertEqual([(t["error"]["class"], t["error"]["tier"]) for t in record["trials"]],
                         [("setup_failed", "infra")])
        self.assertIn("config/settings.json", record["trials"][0]["error"]["detail"])

    def test_astras_three_sources_are_refused_or_never_loaded(self):
        # Astra's settings_effort fixtures: home and the passed config folder refuse at preflight; the
        # task repository's file refuses the attempt before the engine starts.
        with scratch_bases() as (tmp, xdg):
            home = self.folder / "synthetic-home"
            self.write(home / ".claude" / "settings.json", EFFORT_LOW)
            with self.assertRaises(arms.ArmRefused):
                self.arm(home=str(home)).preflight()
            work = self.folder / "work"
            self.write(work / ".claude" / "settings.json", EFFORT_LOW)
            (self.folder / "t.txtar").write_text(tiny_task("t", ["solve"]), encoding="utf-8")
            task = tasks.load_txtar(self.folder / "t.txtar")
            arm = self.arm()
            arm.preflight()
            with patch.object(arms, "run_process", side_effect=AssertionError("engine started")), \
                    scratch.attempt("file-effort", tmp, xdg) as sc:
                with self.assertRaisesRegex(arms.ArmRefused, "work/.claude/settings.json"):
                    arm.run(task, work, sc, 1)


class OverridingArgumentTests(unittest.TestCase):
    """Astra's F02, and the two arguments added: each flag is named here, not read from the constant."""

    FLAGS = ("--effort", "--model", "--permission-mode", "--settings", "--dangerously-skip-permissions",
             "--allow-dangerously-skip-permissions", "--fallback-model", "--agent")

    def test_each_overriding_flag_is_refused_in_both_forms(self):
        for flag in self.FLAGS:
            for args in ([flag], [flag, "x"], [f"{flag}=x"]):
                with self.subTest(args=args), self.assertRaises(arms.ArmRefused):
                    arms.DirectArm("claude_code", model="m", claude=arms.ClaudeCodeSettings(extra_args=args))

    def test_other_flags_pass(self):
        arm = arms.DirectArm("claude_code", model="m",
                             claude=arms.ClaudeCodeSettings(extra_args=["--agents", "{}", "--add-dir", "/x"]))
        self.assertEqual(arm.argv()[-4:], ["--agents", "{}", "--add-dir", "/x"])


class EmptyPinTests(unittest.TestCase):
    """Astra's F01: an empty model or route is not a complete frozen real-engine configuration."""

    def test_empty_model_or_route_pin_is_refused(self):
        for key in ("model", "route"):
            pinned = {"model": "synthetic", "route": "synthetic-route", "effort": "high",
                      "permission_mode": "acceptEdits"}
            pinned[key] = ""
            arm = arms.DirectArm("claude_code", model=pinned["model"], route=pinned["route"], effort="high",
                                 claude=arms.ClaudeCodeSettings(permission_mode="acceptEdits"))
            problem = runner._pinned_settings_problem(types.SimpleNamespace(arms={"claude_code": pinned}), arm)
            with self.subTest(key=key):
                self.assertIsNotNone(problem)
                self.assertIn(key, problem)


if __name__ == "__main__":
    unittest.main()
