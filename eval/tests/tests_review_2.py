"""Answers to the two confirming reads of #381 (GPT-6 Astra C1-C5, GPT-6.1 Sol 1-6), one class per change,
and a test for every removal either read found surviving. Each test fails without its rule."""
from __future__ import annotations

import itertools
import json
import os
import signal
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import REPO_ROOT, __version__, analyze, arms, gitutil, heldout, runner, runner_sha256
from eval import record as record_mod
from eval import scratch as scratch_mod
from eval import suite as suite_mod
from eval import tasks as task_mod
from eval.tests import build_record, make_suite, outside_folder, scratch_bases, tiny_task, trial

SUCCESS = b'{"type":"result","subtype":"success","is_error":false}\n'


def five(record_id="r", arm="direct"):
    rows = [trial(t, i, "pass", record_id=record_id) for t in ("a", "b") for i in range(1, 6)]
    return build_record(rows, k=5, attempts=5, record_id=record_id, arm=arm)


class UnsupportedVersionTests(unittest.TestCase):
    """Astra C1, Sol 3: a record is scored only by the protocol and analysis it names."""

    def test_a_record_of_another_analysis_or_protocol_is_not_scored(self):
        for field in ("analysis_sha256", "protocol_sha256"):
            with self.subTest(field):
                record = five()
                record["protocol"][field] = "e" * 64
                self.assertEqual(record_mod.validate(record), [])
                with self.assertRaisesRegex(ValueError, "unsupported version"):
                    analyze.summarize(record, resamples=1000)
                a, b = five("A"), five("B", arm="recursion")
                a["protocol"][field] = b["protocol"][field] = "e" * 64
                with self.assertRaisesRegex(ValueError, "unsupported version"):
                    analyze.compare(a, b, resamples=1000)

    def test_the_current_version_is_scored(self):
        self.assertEqual(five()["protocol"], runner.protocol_block())
        self.assertIsNotNone(analyze.summarize(five(), resamples=1000)["headline"]["pass_hat_k"])


class EffortOverrideTests(unittest.TestCase):
    """Astra C2: nothing but the named settings sets effort, model or permission mode."""

    def test_a_passed_variable_outside_the_allowed_few_is_refused(self):
        with patch.object(arms.shutil, "which", return_value="/synthetic/claude"):
            for name in ("CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_MODEL", "TMPDIR", "SOMETHING_ELSE"):
                with self.subTest(name):
                    arm = arms.DirectArm("claude_code", model="m", effort="high",
                                         claude=arms.ClaudeCodeSettings(pass_env=[name]))
                    with self.assertRaisesRegex(arms.ArmRefused, name):
                        arm.preflight()
            arms.DirectArm("claude_code", model="m", claude=arms.ClaudeCodeSettings(
                pass_env=["CLAUDE_CONFIG_DIR", "HTTPS_PROXY"])).preflight()

    def test_engine_arguments_that_set_a_named_setting_are_refused(self):
        for args in (["--effort", "low"], ["--model=other"], ["--permission-mode", "plan"], ["--settings", "x.json"],
                     ["--dangerously-skip-permissions"]):
            with self.subTest(args):
                with self.assertRaises(arms.ArmRefused):
                    arms.DirectArm("claude_code", model="m", claude=arms.ClaudeCodeSettings(extra_args=args))
                with self.assertRaises(arms.ArmRefused):
                    arms.RecursionArm("claude_code", model="m", extra_args=args)
        arm = arms.DirectArm("claude_code", model="m", claude=arms.ClaudeCodeSettings(extra_args=["--max-turns", "7"]))
        self.assertEqual(arm.argv()[-2:], ["--max-turns", "7"])


class SeedTests(unittest.TestCase):
    """Astra C3: the seed is an integer, checked before any attempt."""

    def test_a_seed_that_is_not_an_integer_is_refused_before_any_attempt(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            loaded = suite_mod.load_dev(make_suite(folder / "s", {"t": tiny_task("t", ["solve"])},
                                                   tmp_base=tmp, xdg_base=xdg))
            for seed in (1.5, True, "7"):
                with self.subTest(seed=seed), patch.object(runner, "run_attempt") as called:
                    with self.assertRaisesRegex(runner.RunRefused, "seed"):
                        runner.run(loaded, [arms.DirectArm("standin")], k=5, seed=seed, replacements=0, tmp_base=tmp,
                                   xdg_base=xdg, out_dir=folder / "out", log=lambda _m: None)
                    self.assertEqual(called.call_count, 0)


class HeldOutLinkTests(unittest.TestCase):
    """Astra C4, Sol 2: held-out inputs are reached through no link, and task folders hold none."""

    def setUp(self):
        heldout._declared = None

    def tearDown(self):
        heldout._declared = None

    def held_out(self, folder: Path, task_dir: str = "task") -> None:
        manifest = {"schema": suite_mod.SCHEMA_ID, "id": "s", "version": "1", "split": "held_out", "frozen": False,
                    "runner": None, "evaluated_commit": None, "engines": {}, "arms": {},
                    "tasks": [{"id": "h-1", "dir": task_dir, "sha256": "0" * 64, "parent_tree": "0" * 40,
                               "parent_commit": "0" * 40, "commit": "0" * 40}]}
        (folder / "suite.json").write_text(json.dumps(manifest), encoding="utf-8")

    def task(self, where: Path) -> Path:
        where.mkdir(parents=True)
        (where / "task.json").write_text(json.dumps({"id": "h-1", "test_command": ["x"]}), encoding="utf-8")
        (where / "prompt.md").write_text("p", encoding="utf-8")
        (where / "start.tar").write_bytes(task_mod.tar_of_files({"m.py": b"x = 0\n"}))
        (where / "solution").mkdir()
        return where

    def test_an_internal_directory_link_cannot_hide_a_repository_leaf(self):
        with outside_folder() as folder:
            source = self.task(folder / "task")
            (folder / "assets").mkdir()
            (folder / "assets" / "test_leaf.py").symlink_to(REPO_ROOT / "eval" / "tests" / "tests_passk.py")
            (source / "hidden").symlink_to(folder / "assets", target_is_directory=True)
            self.held_out(folder)
            with self.assertRaisesRegex(heldout.HeldOutDenied, "is a link"):
                heldout.open_suite(folder, purpose="score")

    def test_a_task_folder_reached_through_an_internal_link_is_refused(self):
        with outside_folder() as folder:
            self.task(folder / "real")
            (folder / "real" / "hidden").mkdir()
            (folder / "real" / "hidden" / "test_x.py").write_text("", encoding="utf-8")
            (folder / "alias").symlink_to(folder / "real", target_is_directory=True)
            self.held_out(folder, task_dir="alias")
            with self.assertRaisesRegex(heldout.HeldOutDenied, "reached through a link"):
                heldout.open_suite(folder, purpose="score")

    def test_a_manifest_reached_through_a_link_is_refused(self):
        with outside_folder() as folder:
            self.task(folder / "task")
            (folder / "task" / "hidden").mkdir()
            (folder / "task" / "hidden" / "test_x.py").write_text("", encoding="utf-8")
            self.held_out(folder)
            (folder / "suite.json").rename(folder / "real-suite.json")
            (folder / "suite.json").symlink_to(folder / "real-suite.json")
            with self.assertRaisesRegex(heldout.HeldOutDenied, "reached through a link"):
                heldout.open_suite(folder, purpose="score")

    def test_the_loader_itself_refuses_a_link_in_a_task_folder(self):
        with outside_folder() as folder:
            source = self.task(folder / "task")
            (folder / "elsewhere").mkdir()
            (folder / "elsewhere" / "test_x.py").write_text("", encoding="utf-8")
            (source / "hidden").symlink_to(folder / "elsewhere", target_is_directory=True)
            with self.assertRaisesRegex(task_mod.TaskError, "is a link"):
                task_mod.load_dir(source)
            plain = self.task(folder / "plain")
            (plain / "hidden").mkdir()
            (plain / "hidden" / "test_x.py").write_text("", encoding="utf-8")
            task_mod.load_dir(plain)
            (folder / "alias").symlink_to(plain, target_is_directory=True)
            with self.assertRaisesRegex(task_mod.TaskError, "is a link"):
                task_mod.load_dir(folder / "alias")


class SessionCleanupTests(unittest.TestCase):
    """Sol 1: cleanup rescans until the session is empty, and says so when it cannot."""

    def test_a_process_forked_during_the_scan_is_found_and_killed(self):
        with scratch_bases() as (tmp, xdg), scratch_mod.attempt("late", tmp, xdg) as sc:
            ready, trigger, child_marker = (sc.tmpdir / n for n in ("ready", "trigger", "child"))
            helper = ("import os, subprocess, sys, time; from pathlib import Path\n"
                      "os.setpgid(0, 0)\n"
                      f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
                      f"while not Path({str(trigger)!r}).exists(): time.sleep(0.001)\n"
                      "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                      "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                      f"Path({str(child_marker)!r}).write_text(str(p.pid))\n"
                      "time.sleep(60)\n")
            engine = ("import subprocess, sys, time; from pathlib import Path\n"
                      f"subprocess.Popen([sys.executable, '-c', {helper!r}], stdin=subprocess.DEVNULL, "
                      "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                      f"while not Path({str(ready)!r}).exists(): time.sleep(0.001)\n")
            scan, forked = arms._session_members, []

            def scan_then_fork(sid):
                found = scan(sid)
                if found and not forked:
                    forked.append(sid)
                    trigger.write_text("go")
                    # Wait for the pid itself, not the file: the helper creates the file before writing.
                    while not (child_marker.exists() and child_marker.read_text().strip()):
                        time.sleep(0.001)
                return found

            child = None
            try:
                with patch.object(arms, "_session_members", scan_then_fork):
                    arms.run_process([sys.executable, "-B", "-c", engine], cwd=sc.tmpdir,
                                     env=scratch_mod.environment(sc), stdin=None, timeout_s=60)
                child = int(child_marker.read_text())
                state = arms._session_of(child)
                self.assertIn(state[1] if state else None, (None, "Z", "X"))
            finally:
                for marker in (child_marker, ready):
                    if marker.exists():
                        try:
                            os.kill(int(marker.read_text()), signal.SIGKILL)
                        except (ProcessLookupError, ValueError):
                            pass

    def test_a_session_that_will_not_empty_is_reported(self):
        clock = itertools.chain([0.0, 0.0], itertools.repeat(20.0))
        with patch.object(arms.os, "killpg"), patch.object(arms.os, "kill"), \
                patch.object(arms, "_session_members", return_value=[4242]), \
                patch.object(arms.time, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(arms.SessionCleanupError, "survived cleanup"):
                arms._kill_session(os.getsid(0) + 100000)

    def test_a_cleanup_failure_reaches_the_caller_and_leaves_the_trial_ungraded(self):
        with scratch_bases() as (tmp, xdg), scratch_mod.attempt("cleanup", tmp, xdg) as sc, \
                patch.object(arms, "_kill_session", side_effect=arms.SessionCleanupError("pids survived cleanup")):
            with self.assertRaises(arms.SessionCleanupError):
                arms.run_process([sys.executable, "-c", "pass"], cwd=sc.tmpdir, env=scratch_mod.environment(sc),
                                 stdin=None, timeout_s=60)
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            loaded = suite_mod.load_dev(make_suite(folder / "s", {"t": tiny_task("t", ["solve"])},
                                                   tmp_base=tmp, xdg_base=xdg))
            with patch.object(arms, "run_process", side_effect=arms.SessionCleanupError("pids survived cleanup")):
                out = runner.run(loaded, [arms.DirectArm("standin")], k=1, replacements=0, tmp_base=tmp,
                                 xdg_base=xdg, out_dir=folder / "out", log=lambda _m: None)
            row = record_mod.load(out.records["direct"])["trials"][0]
        self.assertEqual(row["grade"]["verdict"], "not_graded")
        self.assertIn("survived cleanup", row["error"]["detail"])

    def test_the_runners_own_session_is_never_signalled(self):
        with patch.object(arms.os, "killpg") as group, patch.object(arms.os, "kill") as single, \
                patch.object(arms, "_session_members", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "own session"):
                arms._kill_session(os.getsid(0))
        group.assert_not_called()
        single.assert_not_called()


class FrozenRealEngineTests(unittest.TestCase):
    """Sol 4: a frozen real-engine suite pins model, route, effort and permission mode."""

    def run_frozen(self, pinned: dict, arm):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            extra = dict(frozen=True, runner={"version": __version__, "sha256": runner_sha256()}, evaluated_commit="f" * 40,
                         engines={"claude_code": "synthetic-version"}, arms={"claude_code": pinned})
            loaded = suite_mod._load_held_out(make_suite(folder / "s", {"t": tiny_task("t", ["solve"])}, split="held_out",
                                                         tmp_base=tmp, xdg_base=xdg, extra=extra))

            def attempt(ctx, arm, task, index, **kwargs):
                return trial(task.id, index, "pass", record_id=ctx.record_id, **kwargs)

            with patch.object(arm, "preflight"), patch.object(arm, "engine_version", return_value="synthetic-version"), \
                    patch.object(runner, "evaluated_commit_matches", return_value=None), \
                    patch.object(runner, "run_attempt", side_effect=attempt) as called:
                out = runner.run(loaded, [arm], k=5, label="pilot", replacements=0, artifacts=folder / "art",
                                 tmp_base=tmp, xdg_base=xdg, out_dir=folder / "out", log=lambda _m: None)
            return out, called.call_count

    def test_absent_settings_refuse_before_any_attempt(self):
        complete = {"model": "m", "route": "api_key:k", "effort": "high", "permission_mode": "acceptEdits"}
        for missing in ("model", "route", "effort", "permission_mode"):
            with self.subTest(missing):
                pinned = {k: v for k, v in complete.items() if k != missing}
                arm = arms.DirectArm("claude_code", model=pinned.get("model"), route=pinned.get("route"),
                                     effort=pinned.get("effort"),
                                     claude=arms.ClaudeCodeSettings(permission_mode=pinned.get("permission_mode")))
                out, attempts = self.run_frozen(pinned, arm)
                self.assertEqual(out.records, {})
                self.assertIn(missing, out.refused["direct"])
                self.assertEqual(attempts, 0)

    def test_complete_matching_settings_run(self):
        pinned = {"model": "m", "route": "api_key:k", "effort": "high", "permission_mode": "acceptEdits"}
        arm = arms.DirectArm("claude_code", model="m", route="api_key:k", effort="high",
                             claude=arms.ClaudeCodeSettings(permission_mode="acceptEdits"))
        out, attempts = self.run_frozen(pinned, arm)
        self.assertIn("direct", out.records)
        self.assertEqual(attempts, 5)


class ScreenFrozenTests(unittest.TestCase):
    """Sol 5: a screen, like a pilot or a full comparison, runs a frozen suite."""

    def test_writer_and_record_refuse_an_unfrozen_screen(self):
        with self.assertRaisesRegex(runner.RunRefused, "frozen"):
            runner.check_plan(types.SimpleNamespace(split="held_out", frozen=False), [arms.DirectArm("claude_code", model="m")],
                              k=5, attempts=5, replacements=0, label="screen", artifacts=Path("/var/tmp/x"))
        record = five()
        record.update(label="screen")
        record["suite"].update(split="held_out", frozen=False)
        record["arm"]["engine"] = "claude_code"
        self.assertTrue(any("frozen" in p for p in record_mod.validate(record)))


class SurvivingRemovalTests(unittest.TestCase):
    """Removals the confirming reads found surviving (Astra X02, X03, X05-X10 and four schema rules;
    Sol X01, X04-X11); the schema rules are in tests_record.SchemaAgreementTests."""

    plan = types.SimpleNamespace(split="dev", frozen=False)

    def test_k_and_attempts_are_positive_integers(self):
        for k, attempts in ((0, 5), (True, 5), (1.5, 5), (1, True), (1, 1.5), (1, 0)):
            with self.subTest(k=k, attempts=attempts):
                with self.assertRaises(runner.RunRefused):
                    runner.check_plan(self.plan, [arms.DirectArm("standin")], k=k, attempts=attempts, replacements=0,
                                      label="dev", artifacts=None)

    def test_an_unknown_label_is_refused(self):
        with self.assertRaisesRegex(runner.RunRefused, "label must be one of"):
            runner.check_plan(types.SimpleNamespace(split="held_out", frozen=False), [arms.DirectArm("standin")], k=1,
                              attempts=1, replacements=0, label="unknown-label", artifacts=None)

    def test_a_dirty_evaluated_core_is_refused(self):
        with outside_folder() as folder:
            (folder / "core").mkdir()
            (folder / "core" / "m.py").write_text("value = 1\n", encoding="utf-8")
            head, _ = gitutil.snapshot(folder)
            self.assertIsNone(runner.evaluated_commit_matches(head, folder))
            (folder / "core" / "m.py").write_text("value = 2\n", encoding="utf-8")
            self.assertIn("uncommitted", runner.evaluated_commit_matches(head, folder))

    def test_task_modes_are_an_object_of_the_two_git_modes(self):
        with outside_folder() as folder:
            for modes in ('[["tiny/core.py", "100755"]]', '{"tiny/core.py": "100777"}', '{"tiny/core.py": "100600"}'):
                with self.subTest(modes):
                    path = folder / "t.txtar"
                    path.write_text(tiny_task("t", ["solve"]).replace('"timeout_s":', f'"modes": {modes}, "timeout_s":', 1),
                                    encoding="utf-8")
                    with self.assertRaises(task_mod.TaskError):
                        task_mod.load_txtar(path)

    def test_commit_tree_pins_are_object_ids_and_agree(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            path = make_suite(folder / "s", {"t": tiny_task("t", ["solve"])}, tmp_base=tmp, xdg_base=xdg)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["tasks"][0]["commit_tree"] = "not-an-object-id"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(suite_mod.SuiteError, "commit_tree"):
                suite_mod.load_dev(path)
            path = make_suite(folder / "s2", {"t": tiny_task("t", ["solve"])}, tmp_base=tmp, xdg_base=xdg)
            original = task_mod.load_txtar

            def other_pin(source):
                task = original(source)
                task.commit_tree = "e" * 40
                return task

            with patch.object(suite_mod.task_mod, "load_txtar", other_pin):
                with self.assertRaisesRegex(suite_mod.SuiteError, "commit_tree"):
                    suite_mod.load_dev(path)

    def test_retained_artifacts_carry_no_storage_error(self):
        record = build_record([trial("a", 1, "pass")], k=1, attempts=1)
        record["trials"][0]["artifacts"].update(retained=True, artifact_error="synthetic")
        self.assertTrue(any("artifact_error" in p for p in record_mod.validate(record)))

    def test_a_replacement_comes_after_the_trial_it_replaces(self):
        rows = [trial("a", i, "vendor_limit" if i == 1 else "pass") for i in range(1, 6)]
        rows += [trial("a", 7, "vendor_limit", planned=False, replacement_for="r.a.1"),
                 trial("a", 6, "pass", planned=False, replacement_for="r.a.7")]
        problems = record_mod.validate(build_record(rows, k=5, attempts=5, replacements=2))
        self.assertTrue(any("does not come after" in p for p in problems), problems)

    def test_unknown_permission_and_stand_in_engine_arguments_are_refused(self):
        with self.assertRaises(arms.ArmRefused):
            arms.DirectArm("claude_code", model="m", claude=arms.ClaudeCodeSettings(permission_mode="nonexistent"))
        with self.assertRaises(arms.ArmRefused):
            arms.DirectArm("standin", claude=arms.ClaudeCodeSettings(extra_args=["--max-turns", "3"]))


if __name__ == "__main__":
    unittest.main()
