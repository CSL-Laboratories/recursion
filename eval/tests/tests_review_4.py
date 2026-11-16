"""Answers to the two fourth-round reads of #381 (GPT-6 Astra, GPT-6.1 Sol).

Both reads: a relative (or literal "~") configuration folder was checked from the runner's working folder
and read by the engine from the task's work tree. Astra's second change and Sol's second: settings that
appear after the checks, and inputs that cannot be read, each need a test of their own. Every settings file
is a newly written fixture in a scratch folder; the machine's managed-policy paths point at scratch; no
engine is started.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import arms, runner, scratch, tasks
from eval import suite as suite_mod
from eval.tests import make_suite, outside_folder, scratch_bases, tiny_task

EFFORT_LOW = json.dumps({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "low"}})


class _Fixture(unittest.TestCase):
    def setUp(self):
        folder = outside_folder()
        self.folder = folder.__enter__()
        self.addCleanup(folder.__exit__, None, None, None)
        self.managed = self.folder / "managed"
        for item in (patch.object(arms, "MANAGED_SETTINGS", (self.managed / "managed-settings.json",
                                                             self.managed / "managed-settings.d")),
                     patch.object(arms.shutil, "which", return_value="/synthetic/claude")):
            item.start()
            self.addCleanup(item.stop)

    def arm(self, **claude):
        claude.setdefault("binary", "synthetic-never-launched")
        claude.setdefault("permission_mode", "acceptEdits")
        return arms.DirectArm("claude_code", model="synthetic", route="synthetic-route", effort="high",
                              claude=arms.ClaudeCodeSettings(**claude))

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def task(self):
        path = self.write(self.folder / "t.txtar", tiny_task("t", ["solve"]))
        return tasks.load_txtar(path)

    def run_dev(self, arm, after_checks):
        """One attempt of a one-task dev suite; ``after_checks`` runs after preflight and the task check,
        before the attempt. The engine must never start."""
        def version():
            after_checks()
            return "synthetic 1"

        with scratch_bases() as (tmp, xdg):
            loaded = suite_mod.load_dev(make_suite(self.folder / "suite", {"t": tiny_task("t", ["solve"])},
                                                   tmp_base=tmp, xdg_base=xdg))
            with patch.object(arm, "engine_version", side_effect=version), \
                    patch.object(arms, "run_process", side_effect=AssertionError("engine started")) as launch:
                out = runner.run(loaded, [arm], k=1, replacements=0, tmp_base=tmp, xdg_base=xdg,
                                 out_dir=self.folder / "out", log=lambda message: None)
        launch.assert_not_called()
        return json.loads(Path(out.records["direct"]).read_text(encoding="utf-8"))["trials"]


class AbsoluteConfigurationFolderTests(_Fixture):
    """Astra 1, Sol 1: the configuration folder must be an absolute path, before the run and at the attempt."""

    def test_astras_relative_folder_is_refused_before_the_run(self):
        # Astra's witness: "cfg" holds no settings from the runner's folder, but low effort from the work tree.
        driver = self.folder / "driver"
        driver.mkdir()
        self.write(self.folder / "work" / "cfg" / "settings.json", EFFORT_LOW)
        with contextlib.chdir(driver), patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "cfg"}):
            with self.assertRaisesRegex(arms.ArmRefused, "CLAUDE_CONFIG_DIR 'cfg' is not an absolute path"):
                self.arm(pass_env=["CLAUDE_CONFIG_DIR"]).preflight()

    def test_sols_literal_tilde_and_an_empty_value_are_refused_before_the_run(self):
        for value in ("~/synthetic-config", ""):
            with self.subTest(value=value), patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": value}):
                with self.assertRaisesRegex(arms.ArmRefused, "not an absolute path"):
                    self.arm(pass_env=["CLAUDE_CONFIG_DIR"]).preflight()

    def test_a_relative_route_home_is_refused_before_the_run(self):
        with self.assertRaisesRegex(arms.ArmRefused, "the route home 'route-home' is not an absolute path"):
            self.arm(home="route-home").preflight()

    def test_absolute_folders_that_set_nothing_pass(self):
        config = self.folder / "config"
        self.write(config / "settings.json", "{}")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            self.arm(pass_env=["CLAUDE_CONFIG_DIR"]).preflight()
        home = self.folder / "home"
        home.mkdir()
        self.arm(home=str(home)).preflight()

    def test_a_relative_or_empty_folder_at_the_attempt_stops_it_before_the_engine(self):
        config = self.folder / "config"
        config.mkdir()
        for value in ("cfg", ""):
            with self.subTest(value=value), patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                trials = self.run_dev(self.arm(pass_env=["CLAUDE_CONFIG_DIR"]),
                                      lambda: os.environ.__setitem__("CLAUDE_CONFIG_DIR", value))
                self.assertEqual([(t["error"]["class"], t["error"]["tier"]) for t in trials], [("setup_failed", "infra")])
                self.assertIn("not an absolute path", trials[0]["error"]["detail"])

    def test_a_route_home_made_relative_after_the_checks_stops_the_attempt(self):
        home = self.folder / "home"
        home.mkdir()
        arm = self.arm(home=str(home))
        trials = self.run_dev(arm, lambda: setattr(arm.claude, "home", "route-home"))
        self.assertEqual([(t["error"]["class"], t["error"]["tier"]) for t in trials], [("setup_failed", "infra")])
        self.assertIn("HOME 'route-home' is not an absolute path", trials[0]["error"]["detail"])


class LateAndUnreadableSettingsTests(_Fixture):
    """Astra 2 (X01-X03, Y01, Y02) and Sol 2: each recheck and each unreadable input has its own test."""

    def test_managed_policy_appearing_after_the_checks_stops_the_attempt(self):
        # Sol's coverage candidate.
        policy = self.managed / "managed-settings.json"
        trials = self.run_dev(self.arm(), lambda: self.write(policy, EFFORT_LOW))
        self.assertEqual([(t["error"]["class"], t["error"]["tier"]) for t in trials], [("setup_failed", "infra")])
        self.assertIn(str(policy), trials[0]["error"]["detail"])

    def late(self, arm, target: Path, work: Path):
        task = self.task()
        with scratch_bases() as (tmp, xdg), scratch.attempt("late", tmp, xdg) as sc:
            arm.preflight()
            arm.check_tasks([task])
            self.write(target, EFFORT_LOW)
            with patch.object(arms, "run_process", side_effect=AssertionError("engine started")):
                with self.assertRaisesRegex(arms.ArmRefused, re.escape(str(target))):
                    arm.run(task, work, sc, 1)

    def test_project_local_settings_appearing_in_the_work_tree_stop_the_attempt(self):
        work = self.folder / "work"
        work.mkdir()
        self.late(self.arm(), work / ".claude" / "settings.local.json", work)

    def test_route_home_settings_appearing_after_the_checks_stop_the_attempt(self):
        home, work = self.folder / "home", self.folder / "work"
        home.mkdir()
        work.mkdir()
        self.late(self.arm(home=str(home)), home / ".claude" / "settings.json", work)

    def test_a_start_archive_that_cannot_be_read_refuses_the_arm(self):
        archive = self.write(self.folder / "start.tar", "generated non-tar data")
        task = tasks.Task(id="t", prompt="p", test_command=[], hidden={}, solution={}, sha256="0" * 64,
                          start_tar=archive)
        with self.assertRaises(tasks.TaskError):
            task.start_file(".claude/settings.json")
        with self.assertRaisesRegex(arms.ArmRefused, "task t: .claude/settings.json"):
            self.arm().check_tasks([task])

    def test_a_managed_folder_that_cannot_be_listed_refuses_the_arm(self):
        drop_in = self.managed / "managed-settings.d"
        drop_in.mkdir(parents=True)
        listing = Path.iterdir

        def refuse(path):
            if path == drop_in:
                raise PermissionError("synthetic: cannot list")
            return listing(path)

        with patch.object(Path, "iterdir", refuse):
            with self.assertRaisesRegex(arms.ArmRefused, "managed-settings.d"):
                self.arm().preflight()


if __name__ == "__main__":
    unittest.main()
