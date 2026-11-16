"""Answers to the two first reads of #381 (GPT-6 Astra, R1-R10; GPT-6.1 Sol, R1-R11), one class per finding,
and a test for every rule removal either read found surviving. Each test fails without its rule."""
from __future__ import annotations

import copy
import json
import os
import signal
import sys
import types
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from eval import REPO_ROOT, __version__, analyze, arms, gitutil, harvest, heldout, runner, runner_sha256
from eval import record as record_mod
from eval import scratch as scratch_mod
from eval import suite as suite_mod
from eval import tasks as task_mod
from eval import validate as validate_mod
from eval.tests import build_record, make_suite, outside_folder, scratch_bases, tiny_task, trial
from eval.tests.tests_harvest import Repo
from eval.tests.tests_record import good

SUCCESS = b'{"type":"result","subtype":"success","is_error":false}\n'


def rehash(record: dict) -> dict:
    body = {k: v for k, v in record["plan"].items() if k != "plan_sha256"}
    body.update(suite_manifest_sha256=record["suite"]["manifest_sha256"],
                arm_settings_sha256=record["arm"]["settings_sha256"])
    record["plan"]["plan_sha256"] = record_mod.sha256_json(body)
    return record


def five(record_id="r", arm="direct"):
    rows = [trial(t, i, "pass", record_id=record_id) for t in ("a", "b") for i in range(1, 6)]
    return build_record(rows, k=5, attempts=5, record_id=record_id, arm=arm)


class InterruptionAndStorageTests(unittest.TestCase):
    """Astra R1, Sol R1: a run keeps every trial that ran; storage failure keeps the trial."""

    def test_attempts_that_finished_survive_an_interrupt_during_collection(self):
        class Finished:
            def __init__(self, value, interrupt):
                self.value, self.interrupt = value, interrupt

            def result(self):
                if self.interrupt:
                    self.interrupt = False
                    raise KeyboardInterrupt
                return self.value

            def done(self):
                return True

            def cancelled(self):
                return False

        class RunsAtOnce:
            def __init__(self, **_):
                self.count = 0

            def submit(self, fn, *args, **kwargs):
                self.count += 1
                return Finished(fn(*args, **kwargs), self.count == 1)

            def shutdown(self, **_):
                pass

        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            loaded = suite_mod.load_dev(make_suite(folder / "s", {"t": tiny_task("t", ["solve"])},
                                                   tmp_base=tmp, xdg_base=xdg))
            with patch.object(runner, "ThreadPoolExecutor", RunsAtOnce):
                out = runner.run(loaded, [arms.DirectArm("standin")], k=5, replacements=0, tmp_base=tmp,
                                 xdg_base=xdg, out_dir=folder / "out", log=lambda _m: None)
            record = record_mod.load(out.records["direct"])
        self.assertTrue(out.interrupted)
        self.assertEqual([t["grade"]["verdict"] for t in record["trials"]], ["resolved"] * 5)

    def test_only_the_attempt_that_never_ran_is_not_run(self):
        class Submits:
            def __init__(self, **_):
                self.i = 0

            def submit(self, fn, ctx, arm, task, index, **kwargs):
                self.i += 1
                future = Future()
                if self.i == 1:
                    future.set_exception(KeyboardInterrupt())
                else:
                    future.set_result(trial(task.id, index, "pass", record_id=ctx.record_id))
                return future

            def shutdown(self, **_):
                pass

        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            loaded = suite_mod.load_dev(make_suite(folder / "s", {"t": tiny_task("t", ["solve"])},
                                                   tmp_base=tmp, xdg_base=xdg))
            with patch.object(runner, "ThreadPoolExecutor", Submits):
                out = runner.run(loaded, [arms.DirectArm("standin")], k=5, tmp_base=tmp, xdg_base=xdg,
                                 out_dir=folder / "out", log=lambda _m: None)
            record = record_mod.load(out.records["direct"])
        classes = [t["error"]["class"] for t in record["trials"]]
        self.assertEqual(classes.count("not_run"), 1)
        self.assertEqual(classes.count(None), 4)

    def test_a_storage_failure_keeps_the_trial_and_says_why(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            loaded = suite_mod.load_dev(make_suite(folder / "s", {"t": tiny_task("t", ["solve"])},
                                                   tmp_base=tmp, xdg_base=xdg))
            with patch.object(runner, "_write_artifacts", side_effect=OSError("synthetic disk full")):
                out = runner.run(loaded, [arms.DirectArm("standin")], k=1, replacements=0,
                                 artifacts=folder / "artifacts", tmp_base=tmp, xdg_base=xdg,
                                 out_dir=folder / "out", log=lambda _m: None)
            row = record_mod.load(out.records["direct"])["trials"][0]
        self.assertEqual(row["grade"]["verdict"], "resolved")
        self.assertFalse(row["artifacts"]["retained"])
        self.assertIn("synthetic disk full", row["artifacts"]["artifact_error"])


class ComparisonTests(unittest.TestCase):
    """Astra R2, Sol R2: only comparable records are paired."""

    def test_a_direct_and_a_recursion_record_with_shared_settings_compare(self):
        out = analyze.compare(five("A"), five("B", arm="recursion"), resamples=1000)
        self.assertEqual(out["pass_hat_k"]["tasks"], 2)

    def test_every_difference_that_breaks_a_comparison_is_refused_and_named(self):
        changes = [("arm", "engine", "claude_code"), ("arm", "engine_version", "other"), ("arm", "model", "m2"),
                   ("arm", "route", "r2"), ("arm", "effort", "low"), ("arm", "settings_sha256", "f" * 64),
                   ("runner", "sha256", "e" * 64), ("runner", "evaluated_commit", "d" * 40),
                   ("protocol", "protocol_sha256", "c" * 64), ("protocol", "analysis_sha256", "b" * 64)]
        for block, field, value in changes:
            with self.subTest(f"{block}.{field}"):
                a, b = five("A"), five("B", arm="recursion")
                b[block][field] = value
                rehash(b)
                self.assertEqual(record_mod.validate(b), [])
                with self.assertRaisesRegex(ValueError, f"{block}\\.{field}"):
                    analyze.compare(a, b, resamples=1000)

    def test_the_shared_settings_hash_leaves_out_the_arm_name_and_follows_the_settings(self):
        self.assertEqual(arms.DirectArm("standin").settings_sha256(), arms.RecursionArm("standin").settings_sha256())
        self.assertNotEqual(arms.DirectArm("standin").settings_sha256(),
                            arms.DirectArm("standin", effort="high").settings_sha256())
        direct = arms.DirectArm("claude_code", model="m", claude=arms.ClaudeCodeSettings(
            binary="/x/claude", permission_mode="acceptEdits", pass_env=["CLAUDE_CONFIG_DIR"]))
        recursion = arms.RecursionArm("claude_code", model="m", permission_mode="acceptEdits")
        self.assertEqual(direct.settings_sha256(), recursion.settings_sha256())


class EffortTests(unittest.TestCase):
    """Astra R3, Sol R6: the effort a record names is the effort the engine got."""

    def launches(self, arm, task, folder, tmp, xdg):
        seen = []

        def fake(argv, **kwargs):
            seen.append((argv, kwargs["env"]))
            return {"stdout": SUCCESS, "stderr": b"", "exit": 0, "signal": None, "timed_out": False, "seconds": 0.0}

        with scratch_mod.attempt(f"effort-{len(os.listdir(tmp))}", tmp, xdg) as sc, patch.object(arms, "run_process", fake):
            arm.run(task, folder, sc, 1)
        return seen[0]

    def test_claude_code_gets_effort_and_permission_mode_on_its_own_flags(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            (folder / "t.txtar").write_text(tiny_task("t", ["solve"]), encoding="utf-8")
            task = task_mod.load_txtar(folder / "t.txtar")
            low = self.launches(arms.DirectArm("claude_code", model="m", effort="low"), task, folder, tmp, xdg)
            high = self.launches(arms.DirectArm("claude_code", model="m", effort="high", claude=arms.ClaudeCodeSettings(
                permission_mode="acceptEdits")), task, folder, tmp, xdg)
        self.assertEqual(low[0][low[0].index("--effort") + 1], "low")
        self.assertEqual(high[0][high[0].index("--effort") + 1], "high")
        self.assertEqual(high[0][high[0].index("--permission-mode") + 1], "acceptEdits")
        self.assertNotIn("--effort", arms.DirectArm("claude_code", model="m").argv())

    def test_the_stand_in_is_told_its_effort(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            loaded = suite_mod.load_dev(make_suite(folder / "s", {"t": tiny_task("t", ["solve"])},
                                                   tmp_base=tmp, xdg_base=xdg))
            out = runner.run(loaded, [arms.DirectArm("standin", effort="high")], k=1, replacements=0,
                             artifacts=folder / "art", tmp_base=tmp, xdg_base=xdg, out_dir=folder / "out",
                             log=lambda _m: None)
            record = record_mod.load(out.records["direct"])
            row = record["trials"][0]
            lines = (folder / "art" / record["record_id"] / row["trial_id"] / "engine.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["effort"], "high")
        self.assertEqual(record["arm"]["effort"], "high")

    def test_an_effort_or_permission_mode_the_engine_does_not_have_is_refused(self):
        with self.assertRaises(arms.ArmRefused):
            arms.DirectArm("claude_code", model="m", effort="extreme")
        with self.assertRaises(arms.ArmRefused):
            arms.DirectArm("claude_code", model="m", claude=arms.ClaudeCodeSettings(permission_mode="yolo"))
        with self.assertRaises(arms.ArmRefused):
            arms.DirectArm("standin", claude=arms.ClaudeCodeSettings(permission_mode="acceptEdits"))


class PlanAndLabelTests(unittest.TestCase):
    """Astra R6, Sol R7: measurement rules and the plan are checked before any attempt."""

    def suite(self, split="held_out", frozen=False):
        return types.SimpleNamespace(split=split, frozen=frozen)

    def test_what_a_measurement_needs(self):
        real = [arms.DirectArm("claude_code", model="m")]
        art = Path("/var/tmp/x")
        refused = [
            ("pilot with k 4", dict(suite=self.suite(frozen=True), arms=real, k=4, label="pilot", artifacts=art)),
            ("screen with the stand-in", dict(suite=self.suite(frozen=True), arms=[arms.DirectArm("standin")], k=5,
                                              label="screen", artifacts=art)),
            ("full without artifacts", dict(suite=self.suite(frozen=True), arms=real, k=5, label="full",
                                            artifacts=None)),
            ("pilot on an unfrozen suite", dict(suite=self.suite(), arms=real, k=5, label="pilot", artifacts=art)),
            ("screen on an unfrozen suite", dict(suite=self.suite(), arms=real, k=5, label="screen", artifacts=art)),
            ("dev label on a held-out suite", dict(suite=self.suite(), arms=real, k=5, label="dev", artifacts=art)),
            ("shakedown on the dev set", dict(suite=self.suite("dev"), arms=real, k=5, label="shakedown",
                                              artifacts=art)),
        ]
        for name, kw in refused:
            with self.subTest(name):
                with self.assertRaises(runner.RunRefused):
                    runner.check_plan(kw["suite"], kw["arms"], k=kw["k"], attempts=kw["k"], replacements=2,
                                      label=kw["label"], artifacts=kw["artifacts"])
        runner.check_plan(self.suite(frozen=True), real, k=5, attempts=5, replacements=2, label="screen", artifacts=art)
        runner.check_plan(self.suite(), [arms.DirectArm("standin")], k=1, attempts=1, replacements=0,
                          label="shakedown", artifacts=None)

    def test_a_bad_plan_is_refused_before_any_attempt(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            loaded = suite_mod.load_dev(make_suite(folder / "s", {"t": tiny_task("t", ["solve"])},
                                                   tmp_base=tmp, xdg_base=xdg))
            for bad in (dict(replacements=-1), dict(k=0), dict(k=2, attempts=1), dict(replacements=True)):
                with self.subTest(bad), patch.object(runner, "run_attempt") as called:
                    params = dict(k=5, replacements=0) | bad
                    with self.assertRaises(runner.RunRefused):
                        runner.run(loaded, [arms.DirectArm("standin")], tmp_base=tmp, xdg_base=xdg,
                                   out_dir=folder / "out", log=lambda _m: None, **params)
                    self.assertEqual(called.call_count, 0)

    def test_a_record_cannot_claim_more_than_its_run(self):
        def held_out(record, frozen=False):
            record["suite"].update(split="held_out", frozen=frozen)
            return record

        def real(record):
            record["arm"] = dict(record["arm"], engine="claude_code")
            return record

        bad = {
            "dev set labelled full": (dict(five(), label="full"), "dev set is labelled dev"),
            "held-out labelled dev": (held_out(five()), "dev set is labelled dev"),
            "pilot on the stand-in": (held_out(dict(five(), label="pilot"), frozen=True), "stand-in"),
            "pilot unfrozen": (real(held_out(dict(five(), label="pilot"))), "frozen suite"),
        }
        for name, (record, reason) in bad.items():
            with self.subTest(name):
                problems = record_mod.validate(record)
                self.assertTrue(any(reason in p for p in problems), problems)
        frozen_real = real(held_out(dict(five(), label="pilot"), frozen=True))
        self.assertEqual(record_mod.validate(frozen_real), [])
        short = build_record([trial("a", 1, "pass")], k=1, attempts=1, label="screen")
        short["suite"]["split"] = "held_out"
        short["arm"]["engine"] = "claude_code"
        self.assertTrue(any("k of at least 5" in p for p in record_mod.validate(short)))
        ok = held_out(dict(five(), label="shakedown"))
        self.assertEqual(record_mod.validate(ok), [])


class ReplacementTests(unittest.TestCase):
    """Astra R8, Sol R8: replacement trials obey the plan."""

    base = [("a", i, "vendor_limit" if i == 1 else "pass") for i in range(1, 6)] + [("b", i, "pass") for i in range(1, 6)]

    def record(self, extras, budget=2):
        rows = [trial(t, i, o) for t, i, o in self.base] + extras
        return build_record(copy.deepcopy(rows), k=5, attempts=5, replacements=budget)

    def test_invalid_replacements_are_refused(self):
        cases = {
            "zero budget": ([trial("a", 6, "pass", planned=False, replacement_for="r.a.1")], 0),
            "another task's failure": ([trial("b", 6, "pass", planned=False, replacement_for="r.a.1")], 2),
            "an original that passed": ([trial("a", 6, "pass", planned=False, replacement_for="r.a.2")], 2),
            "the same original twice": ([trial("a", 6, "pass", planned=False, replacement_for="r.a.1"),
                                         trial("a", 7, "pass", planned=False, replacement_for="r.a.1")], 2),
            "itself": ([trial("a", 6, "pass", planned=False, replacement_for="r.a.6")], 2),
            "an unknown trial": ([trial("a", 6, "pass", planned=False, replacement_for="nope")], 2),
            "an index inside the planned attempts": ([], 2),
            "a gap in the indexes": ([trial("a", 7, "pass", planned=False, replacement_for="r.a.1")], 2),
        }
        for name, (extras, budget) in cases.items():
            with self.subTest(name):
                record = self.record(extras, budget)
                if name == "an index inside the planned attempts":
                    record["trials"].append(trial("a", 5, "pass", planned=False, replacement_for="r.a.1"))
                self.assertTrue(record_mod.validate(record), name)

    def test_a_failed_replacement_may_be_replaced_within_the_budget(self):
        extras = [trial("a", 6, "vendor_limit", planned=False, replacement_for="r.a.1"),
                  trial("a", 7, "pass", planned=False, replacement_for="r.a.6")]
        self.assertEqual(record_mod.validate(self.record(extras)), [])
        self.assertTrue(record_mod.validate(self.record(extras, budget=1)))


class FiniteNumberTests(unittest.TestCase):
    """Astra R9: NaN and infinity are not measurements."""

    def test_non_finite_numbers_are_refused_everywhere(self):
        for number in (float("nan"), float("inf")):
            with self.subTest(number):
                rec = build_record([trial("a", 1, "pass")], k=1, attempts=1)
                rec["trials"][0]["usage"].update(cost=number, cost_unit="USD", cost_basis="billed")
                self.assertTrue(record_mod.validate(rec))
                rec = build_record([trial("a", 1, "pass")], k=1, attempts=1)
                rec["trials"][0]["timing"]["wall_seconds"] = number
                self.assertTrue(record_mod.validate(rec))
        with self.assertRaises(ValueError):
            record_mod.dumps({"x": float("nan")})
        with self.assertRaises(ValueError):
            record_mod.loads('{"x": NaN}')
        out = arms.parse_stream_json(b'{"type":"result","subtype":"success","total_cost_usd":Infinity}\n',
                                     cost_basis="billed")
        self.assertIsNone(out["usage"]["cost"])


class FrozenSuiteTests(unittest.TestCase):
    """Astra R5, Sol R3: a frozen suite pins what is actually run."""

    def frozen(self, folder, tmp, xdg, **extra):
        manifest = dict(frozen=True, runner={"version": __version__, "sha256": runner_sha256()},
                        evaluated_commit="f" * 40, engines={"standin": "eval-standin/1"}, arms={"standin": {}})
        manifest.update(extra)
        return suite_mod.load_dev(make_suite(folder / "s", {"t": tiny_task("t", ["solve"])}, tmp_base=tmp,
                                             xdg_base=xdg, extra=manifest))

    def test_the_selected_engine_must_be_pinned(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            loaded = self.frozen(folder, tmp, xdg, engines={"another-engine": "1"})
            out = runner.run(loaded, [arms.DirectArm("standin")], k=1, replacements=0, tmp_base=tmp, xdg_base=xdg,
                             out_dir=folder / "out", log=lambda _m: None)
        self.assertEqual(out.records, {})
        self.assertIn("pins no version for engine 'standin'", out.refused["direct"])

    def test_the_evaluated_commit_must_be_the_checkout(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            loaded = self.frozen(folder, tmp, xdg, evaluated_commit="0" * 40)
            with self.assertRaises(runner.RunRefused):
                runner.run(loaded, [arms.DirectArm("standin")], k=1, replacements=0, tmp_base=tmp, xdg_base=xdg,
                           out_dir=folder / "out", log=lambda _m: None)
        self.assertIn("not a full commit id", runner.evaluated_commit_matches("abc"))

    def test_the_arm_settings_must_be_the_pinned_ones(self):
        # The evaluated-commit check is the test above's; here it is set aside so this runs anywhere.
        with scratch_bases() as (tmp, xdg), outside_folder() as folder, \
                patch.object(runner, "evaluated_commit_matches", return_value=None):
            for pinned, arm in (({"effort": "high"}, arms.DirectArm("standin")),
                                ({}, arms.DirectArm("standin", effort="low")),
                                ({"colour": "red"}, arms.DirectArm("standin"))):
                with self.subTest(pinned=pinned, effort=arm.effort):
                    loaded = self.frozen(folder / str(len(os.listdir(folder))), tmp, xdg, arms={"standin": pinned})
                    out = runner.run(loaded, [arm], k=1, replacements=0, tmp_base=tmp, xdg_base=xdg,
                                     out_dir=folder / "out", log=lambda _m: None)
                    self.assertEqual(out.records, {})
                    self.assertIn("frozen suite", out.refused["direct"])
            loaded = self.frozen(folder / "match", tmp, xdg, arms={"standin": {"effort": "low"}})
            out = runner.run(loaded, [arms.DirectArm("standin", effort="low")], k=1, replacements=0, tmp_base=tmp,
                             xdg_base=xdg, out_dir=folder / "out", log=lambda _m: None)
            self.assertIn("direct", out.records)

    def test_a_frozen_suite_on_the_checked_out_commit_runs(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            loaded = self.frozen(folder, tmp, xdg, evaluated_commit=self.head())
            out = runner.run(loaded, [arms.DirectArm("standin")], k=1, replacements=0, tmp_base=tmp, xdg_base=xdg,
                             out_dir=folder / "out", log=lambda _m: None)
            record = record_mod.load(out.records["direct"])
        self.assertEqual(record["runner"]["evaluated_commit"], self.head())

    def head(self):
        if not (REPO_ROOT / ".git").exists():
            self.skipTest("the evaluated checkout is not a git repository here")
        problem = runner.evaluated_commit_matches(gitutil.git(["rev-parse", "HEAD"], REPO_ROOT).decode().strip())
        if problem:
            self.skipTest(problem)
        return gitutil.git(["rev-parse", "HEAD"], REPO_ROOT).decode().strip()


class SessionAndEnvironmentTests(unittest.TestCase):
    """Sol R5, R4: nothing left in the engine's session; nothing passed in replaces the attempt's runtime."""

    def test_a_helper_in_another_process_group_of_the_session_is_killed(self):
        with scratch_bases() as (tmp, xdg), scratch_mod.attempt("group", tmp, xdg) as sc:
            marker = sc.tmpdir / "ready"
            helper = ("import os, time; from pathlib import Path; os.setpgid(0, 0); "
                      f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)")
            engine = ("import subprocess, sys, time; from pathlib import Path\n"
                      f"p = subprocess.Popen([sys.executable, '-c', {helper!r}], stdin=subprocess.DEVNULL, "
                      "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                      f"while not Path({str(marker)!r}).exists():\n    time.sleep(0.01)\n"
                      "print(p.pid, flush=True)\n")
            pid = None
            try:
                done = arms.run_process([sys.executable, "-B", "-c", engine], cwd=sc.tmpdir,
                                        env=scratch_mod.environment(sc), stdin=None, timeout_s=60)
                pid = int(done["stdout"].strip())
                found = arms._session_of(pid)
                self.assertIn(found[1] if found else None, (None, "Z", "X"))
            finally:
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_passed_variables_never_replace_the_attempts_runtime(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            (folder / "t.txtar").write_text(tiny_task("t", ["solve"]), encoding="utf-8")
            task = task_mod.load_txtar(folder / "t.txtar")
            seen = []

            def fake(argv, **kwargs):
                seen.append(kwargs["env"])
                return {"stdout": SUCCESS, "stderr": b"", "exit": 0, "signal": None, "timed_out": False, "seconds": 0.0}

            arm = arms.DirectArm("claude_code", model="m", claude=arms.ClaudeCodeSettings(
                binary=sys.executable, pass_env=["TMPDIR", "XDG_RUNTIME_DIR", "HOME"]))
            with scratch_mod.attempt("pass-env", tmp, xdg) as sc, patch.object(arms, "run_process", fake), \
                    patch.dict(os.environ, {"TMPDIR": str(tmp), "XDG_RUNTIME_DIR": str(xdg), "HOME": "/nowhere"}):
                arm.run(task, folder, sc, 1)
                self.assertEqual(seen[0]["TMPDIR"], str(sc.tmpdir))
                self.assertEqual(seen[0]["XDG_RUNTIME_DIR"], str(sc.xdg_runtime_dir))
                self.assertEqual(seen[0]["HOME"], str(sc.home))
            with self.assertRaises(arms.ArmRefused):
                arm.preflight()


class HeldOutSourceTests(unittest.TestCase):
    """Astra R7: every input of a held-out suite lies in its folder, outside the repository."""

    def setUp(self):
        heldout._declared = None

    def tearDown(self):
        heldout._declared = None

    def test_a_link_to_the_repositorys_tasks_is_refused(self):
        with outside_folder() as folder:
            manifest = json.loads(runner.DEV_SUITE.read_text(encoding="utf-8"))
            manifest.update(id="synthetic", split="held_out")
            (folder / "suite.json").write_text(json.dumps(manifest), encoding="utf-8")
            (folder / "tasks").symlink_to(runner.DEV_SUITE.parent / "tasks", target_is_directory=True)
            with self.assertRaisesRegex(heldout.HeldOutDenied, "repository"):
                heldout.open_suite(folder, purpose="score")

    def test_a_link_inside_a_task_folder_is_checked(self):
        with outside_folder() as folder, outside_folder() as elsewhere:
            task = folder / "h-1"
            (task / "hidden").mkdir(parents=True)
            (task / "task.json").write_text(json.dumps({"id": "h-1", "test_command": ["x"]}), encoding="utf-8")
            (task / "prompt.md").write_text("p", encoding="utf-8")
            (elsewhere / "test_x.py").write_text("", encoding="utf-8")
            (task / "hidden" / "test_x.py").symlink_to(elsewhere / "test_x.py")
            manifest = {"schema": suite_mod.SCHEMA_ID, "id": "s", "version": "1", "split": "held_out", "frozen": False,
                        "runner": None, "evaluated_commit": None, "engines": {}, "arms": {},
                        "tasks": [{"id": "h-1", "dir": "h-1", "sha256": "0" * 64, "parent_tree": "0" * 40,
                                   "parent_commit": "0" * 40, "commit": "0" * 40}]}
            (folder / "suite.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(heldout.HeldOutDenied, "is a link"):
                heldout.open_suite(folder, purpose="score")


class HarvestTreeTests(unittest.TestCase):
    """Astra R4: a harvested task is checked against the commit's own tree."""

    def candidate(self, folder, files_before, files_after, message="change", deletions=(), chmod=()):
        repo = Repo(folder / "upstream")
        repo.commit(files_before, "initial", "2026-01-01T00:00:00+0000")
        for name in deletions:
            (repo.path / name).unlink()
        repo.write(files_after)
        for name in chmod:
            (repo.path / name).chmod(0o755)
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", message, date="2026-03-01T00:00:00+0000")
        head = repo.git("rev-parse", "HEAD")
        cfg = {"name": "synthetic", "path": str(repo.path), "branch": "main", "test_globs": ["tests/**/test_*.py"],
               "test_command": ["{python}", "-B", "-m", "unittest", "{hidden_modules}"]}
        candidate, reason = harvest.build_candidate(cfg, head, {})
        self.assertIsNone(reason)
        harvest.write_task(folder / "task", candidate)
        task = task_mod.load_dir(folder / "task")
        task.parent_tree = candidate["parent_tree"]
        return candidate, task

    def test_a_deleted_test_file_is_part_of_the_solution_and_the_commits_failure_is_seen(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            candidate, task = self.candidate(
                folder,
                {"feature.py": "answer = 0\n", "tests/__init__.py": "", "tests/test_helper.py": "expected = 42\n",
                 "tests/test_feature.py": "# old\n"},
                {"feature.py": "answer = 42\n", "tests/test_feature.py":
                    "import unittest\nfrom feature import answer\nfrom tests.test_helper import expected\n"
                    "class Check(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer, expected)\n"},
                deletions=("tests/test_helper.py",))
            self.assertIn("tests/test_helper.py", candidate["meta"]["solution_deletes"])
            checked = validate_mod.validate(task, tmp_base=tmp, xdg_base=xdg)
        self.assertFalse(checked.ok)
        self.assertIn("fail on the commit", checked.reason)

    def test_a_graded_tree_that_is_not_the_commits_tree_is_refused(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            _, task = self.candidate(folder, {"m.py": "x = 0\n", "tests/__init__.py": ""},
                                     {"m.py": "x = 1\n", "tests/test_m.py":
                                      "import unittest\nimport m\nclass T(unittest.TestCase):\n"
                                      "    def test_x(self):\n        self.assertEqual(m.x, 1)\n"})
            self.assertTrue(validate_mod.validate(task, tmp_base=tmp, xdg_base=xdg).ok)
            task.commit_tree = "0" * 40
            checked = validate_mod.validate(task, tmp_base=tmp, xdg_base=xdg)
        self.assertFalse(checked.ok)
        self.assertIn("not the pinned commit tree", checked.reason)

    def test_a_mode_change_is_kept_so_the_trees_agree(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            candidate, task = self.candidate(
                folder, {"run.sh": "echo hi\n", "tests/__init__.py": ""},
                {"run.sh": "echo hello\n", "tests/test_run.py":
                 "import os, unittest\nclass T(unittest.TestCase):\n    def test_exec(self):\n"
                 "        self.assertTrue(os.access('run.sh', os.X_OK))\n"}, chmod=("run.sh", "tests/test_run.py"))
            self.assertEqual(candidate["meta"]["modes"], {"run.sh": "100755", "tests/test_run.py": "100755"})
            checked = validate_mod.validate(task, tmp_base=tmp, xdg_base=xdg)
        self.assertTrue(checked.ok, checked.reason)


class ExclusionTests(unittest.TestCase):
    """Sol R10: the valid score's exclusions count failed replacements; the bounds use planned trials."""

    def test_failed_replacements_are_counted_among_exclusions(self):
        rows = [trial(t, i, "vendor_limit" if (t, i) == ("a", 1) else "pass") for t in ("a", "b") for i in range(1, 6)]
        rows += [trial("a", 6, "vendor_limit", planned=False, replacement_for="r.a.1"),
                 trial("a", 7, "pass", planned=False, replacement_for="r.a.6")]
        out = analyze.summarize(build_record(rows, k=5, attempts=5, replacements=2), resamples=1000)
        self.assertEqual(out["valid_runs_only"]["excluded"]["infra"], 2)
        self.assertEqual(out["valid_runs_only"]["excluded_planned"]["infra"], 1)


class SurvivingRemovalTests(unittest.TestCase):
    """Rule removals the two reads found surviving the first suite (Sol M03-M09, M13, M14; Astra M01-M03,
    M06, M10): each now fails a test."""

    def manifest(self, folder, tmp, xdg, **extra):
        return make_suite(folder, {"t": tiny_task("t", ["solve"])}, tmp_base=tmp, xdg_base=xdg, extra=extra)

    def test_frozen_manifest_pins(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            cases = {
                "an engine without a version": dict(frozen=True, evaluated_commit="f" * 40,
                                                    runner={"version": "v", "sha256": "0" * 64},
                                                    engines={"standin": None}),
                "no runner": dict(frozen=True, evaluated_commit="f" * 40, runner=None),
                "a wrong schema id": dict(schema="recursion-eval-suite/0"),
                "a bad suite id": dict(id="../escape"),
            }
            for name, extra in cases.items():
                with self.subTest(name):
                    path = self.manifest(folder / name.replace(" ", "-"), tmp, xdg, **extra)
                    with self.assertRaises(suite_mod.SuiteError):
                        suite_mod.load_dev(path)

    def test_a_task_entry_needs_its_parent_commit(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            path = self.manifest(folder, tmp, xdg)
            data = json.loads(path.read_text(encoding="utf-8"))
            del data["tasks"][0]["parent_commit"]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(suite_mod.SuiteError, "parent_commit"):
                suite_mod.load_dev(path)

    def test_record_rules(self):
        cases = {
            "negative wall time": lambda r: r["trials"][0]["timing"].update(wall_seconds=-1),
            "a duplicate trial id": lambda r: r["trials"][1].update(trial_id=r["trials"][0]["trial_id"]),
            "a replacement of an unknown trial": lambda r: r["trials"][3]["attempt"].update(replacement_for="nope"),
            "a negative cost": lambda r: r["trials"][0]["usage"].update(cost=-1.0, cost_unit="USD", cost_basis="billed"),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                record = good()
                mutate(record)
                self.assertTrue(record_mod.validate(record), name)

    def test_a_boolean_token_count_is_unknown(self):
        out = arms.parse_stream_json(b'{"type":"result","subtype":"success","usage":{"input_tokens":true}}\n',
                                     cost_basis="billed")
        self.assertIsNone(out["usage"]["uncached_input"])

    def test_no_tests_ran_is_a_grader_failure_when_grading_and_named_when_validating(self):
        task = task_mod.Task(id="empty", prompt="p", test_command=["{python}", "-B", "-m", "unittest", "{hidden_modules}"],
                             hidden={"test_empty.py": b"# no tests here\n"}, solution={"unchanged.py": b"x = 1\n"},
                             sha256="0" * 64, start_files={"unchanged.py": b""})
        with scratch_bases() as (tmp, xdg):
            with scratch_mod.attempt("empty", tmp, xdg) as sc:
                work = sc.tmpdir / "work"
                work.mkdir()
                graded = runner.grade(task, work, sc)
            checked = validate_mod.validate(task, tmp_base=tmp, xdg_base=xdg)
        self.assertEqual((graded["verdict"], graded["error"]), ("not_graded", "no_tests_ran"))
        self.assertFalse(checked.ok)
        self.assertIn("ran no test on the parent", checked.reason)

    def test_a_commit_side_that_runs_no_test_is_named(self):
        # The hidden file fails to import on the parent (a failure) and imports with no TestCase on the commit.
        task = task_mod.Task(id="none", prompt="p", test_command=["{python}", "-B", "-m", "unittest", "{hidden_modules}"],
                             hidden={"test_added.py": b"from m import added\n\ndef test_added():\n    assert added() == 42\n"},
                             solution={"m.py": b"def added():\n    return 42\n"}, sha256="0" * 64,
                             start_files={"m.py": b"x = 1\n"})
        with scratch_bases() as (tmp, xdg):
            checked = validate_mod.validate(task, tmp_base=tmp, xdg_base=xdg)
        self.assertFalse(checked.ok)
        self.assertIn("ran no test on the commit", checked.reason)


if __name__ == "__main__":
    unittest.main()
