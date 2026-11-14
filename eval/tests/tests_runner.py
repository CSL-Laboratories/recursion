"""The runner end to end on the stand-in engine: the first proof, scratch folders, failures by tier."""
from __future__ import annotations

import json
import os
import unittest
from fractions import Fraction
from pathlib import Path

from eval import analyze, arms, passk, runner
from eval import record as record_mod
from eval import suite as suite_mod
from eval.tests import make_suite, outside_folder, scratch_bases, tiny_task


def standin_arms():
    return [arms.DirectArm("standin"), arms.RecursionArm("standin")]


class FirstProofTests(unittest.TestCase):
    """The dev set through both arms on the stand-in engine (EVAL brief, first proof)."""

    @classmethod
    def setUpClass(cls):
        cls.messages = []
        with scratch_bases() as (tmp, xdg), outside_folder() as out:
            suite = suite_mod.load_dev(runner.DEV_SUITE)
            cls.outcome = runner.run(suite, standin_arms(), k=5, label="dev", jobs=4, out_dir=out,
                                     tmp_base=tmp, xdg_base=xdg, log=cls.messages.append)
            cls.records = {name: json.loads(Path(path).read_text(encoding="utf-8"))
                           for name, path in cls.outcome.records.items()}
            cls.left_tmp = sorted(os.listdir(tmp))
            cls.left_xdg = sorted(os.listdir(xdg))

    def test_recursion_arm_refuses_and_the_direct_arm_runs(self):
        self.assertEqual(set(self.outcome.records), {"direct"})
        self.assertIn("recursion", self.outcome.refused)
        message = self.outcome.refused["recursion"]
        self.assertIn("workspace", message)
        self.assertIn("events.read", message)
        self.assertIn(message, self.messages)

    def test_one_valid_record_whose_per_task_planned_trial_count_is_k(self):
        record = self.records["direct"]
        self.assertEqual(record_mod.validate(record), [])
        self.assertEqual(record["plan"]["k"], 5)
        planned = {}
        for trial in record["trials"]:
            if trial["attempt"]["planned"]:
                planned[trial["task_id"]] = planned.get(trial["task_id"], 0) + 1
        self.assertEqual(len(planned), 12)
        self.assertEqual(set(planned.values()), {5})

    def test_calculator_recomputes_pass_hat_k_from_the_trials(self):
        record = self.records["direct"]
        out = analyze.summarize(record, resamples=1000)
        # Stand-in plans: 01, 02, 07, 11 resolve all 5 planned trials; no other task does.
        self.assertEqual(Fraction(out["headline"]["pass_hat_k"]).limit_denominator(100), Fraction(1, 3))
        self.assertAlmostEqual(out["headline"]["pass_at_1"], 39 / 60)
        # Independently of analyze.py: count resolved planned trials per task and apply C(c,5)/C(5,5).
        counts = {}
        for trial in record["trials"]:
            if trial["attempt"]["planned"]:
                n, c = counts.get(trial["task_id"], (0, 0))
                counts[trial["task_id"]] = (n + 1, c + (trial["grade"]["verdict"] == "resolved"))
        values = [passk.pass_hat_k(passk.Counts(n, c), 5) for n, c in counts.values()]
        self.assertEqual(sum(values) / len(values), Fraction(1, 3))

    def test_infra_failures_get_replacements_and_leave_only_the_valid_score(self):
        record = self.records["direct"]
        out = analyze.summarize(record, resamples=1000)
        by_task = {}
        for trial in record["trials"]:
            by_task.setdefault(trial["task_id"], []).append(trial)
        config = by_task["dev-06-config"]
        self.assertEqual(len(config), 6)
        limit = [t for t in config if t["error"]["class"] == "vendor_limit"]
        self.assertEqual(len(limit), 1)
        self.assertEqual(limit[0]["error"]["tier"], "infra")
        replacement = [t for t in config if not t["attempt"]["planned"]]
        self.assertEqual(replacement[0]["attempt"]["replacement_for"], limit[0]["trial_id"])
        self.assertEqual(out["counts"]["dev-06-config"], {"headline": [5, 4], "valid": [5, 5]})
        # dev-10: three sign-in failures, two replacements (the budget), one valid replacement: 3 valid trials.
        self.assertEqual(len(by_task["dev-10-rotate"]), 7)
        self.assertEqual(out["counts"]["dev-10-rotate"]["valid"], [3, 3])
        self.assertIn("dev-10-rotate", out["valid_runs_only"]["not_estimable"])
        self.assertEqual(Fraction(out["valid_runs_only"]["pass_hat_k"]).limit_denominator(100), Fraction(5, 11))
        self.assertEqual(out["valid_runs_only"]["excluded"]["infra"], 4)

    def test_failure_classes_and_false_done(self):
        record = self.records["direct"]
        classes = {(t["task_id"], t["attempt"]["index"]): t["error"]["class"] for t in record["trials"]}
        self.assertEqual(classes[("dev-05-semver", 2)], "engine_crash")
        self.assertEqual(classes[("dev-03-merge-intervals", 5)], "engine_error")
        self.assertEqual(classes[("dev-03-merge-intervals", 2)], "tests_failed")
        self.assertEqual(classes[("dev-10-rotate", 2)], "auth_failed")
        out = analyze.summarize(record, resamples=1000)
        self.assertEqual(out["false_done"]["false_done"], 11)
        self.assertEqual(out["false_done"]["claimed_done"], 52)

    def test_tokens_known_only_where_the_engine_reported_them(self):
        record = self.records["direct"]
        out = analyze.summarize(record, resamples=1000)
        self.assertEqual(out["usage"]["output"]["known"], 5)
        self.assertEqual(out["usage"]["output"]["sum_of_known"], 5 * 450)
        self.assertGreater(out["usage"]["output"]["unknown"], 0)
        roman = [t for t in record["trials"] if t["task_id"] == "dev-02-roman"]
        self.assertEqual({t["usage"]["cache_write"] for t in roman}, {300})
        lru = [t for t in record["trials"] if t["task_id"] == "dev-07-lru"]
        self.assertEqual({t["usage"]["cache_write"] for t in lru}, {None})

    def test_each_attempt_had_its_own_scratch_and_none_is_left(self):
        record = self.records["direct"]
        tmpdirs = [t["scratch"]["tmpdir"] for t in record["trials"]]
        xdgs = [t["scratch"]["xdg_runtime_dir"] for t in record["trials"]]
        self.assertEqual(len(set(tmpdirs)), len(tmpdirs))
        self.assertEqual(len(set(xdgs)), len(xdgs))
        self.assertTrue(all(t["scratch"]["removed"] is True for t in record["trials"]))
        self.assertEqual(self.left_tmp, [])
        self.assertEqual(self.left_xdg, [])


class AttemptTests(unittest.TestCase):
    def run_suite(self, tasks, *, k=1, arm=None, artifacts=True, bad_tree_for=(), **kwargs):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            manifest = make_suite(folder / "suite", tasks, tmp_base=tmp, xdg_base=xdg, bad_tree_for=bad_tree_for)
            suite = suite_mod.load_dev(manifest)
            art = folder / "artifacts" if artifacts else None
            outcome = runner.run(suite, [arm or arms.DirectArm("standin")], k=k, label="dev", out_dir=folder / "out",
                                 artifacts=art, tmp_base=tmp, xdg_base=xdg, log=lambda _m: None, **kwargs)
            record = json.loads(Path(outcome.records["direct"]).read_text(encoding="utf-8"))
            engine_lines = {}
            if art is not None:
                for trial in record["trials"]:
                    path = art / record["record_id"] / trial["trial_id"] / "engine.jsonl"
                    if path.exists():
                        engine_lines[trial["trial_id"]] = [json.loads(line) for line in
                                                           path.read_text(encoding="utf-8").splitlines() if line]
            leftovers = (sorted(os.listdir(tmp)), sorted(os.listdir(xdg)))
            return record, engine_lines, leftovers

    def test_engine_sees_its_own_scratch_and_no_bus_address(self):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/eval-test-bus"
        try:
            record, lines, leftovers = self.run_suite({"t-solve": tiny_task("t-solve", ["solve"])}, k=3)
        finally:
            del os.environ["DBUS_SESSION_BUS_ADDRESS"]
        self.assertEqual(leftovers, ([], []))
        seen = set()
        for trial in record["trials"]:
            init = lines[trial["trial_id"]][0]
            self.assertEqual(init["subtype"], "init")
            self.assertEqual(init["tmpdir"], trial["scratch"]["tmpdir"])
            self.assertEqual(init["xdg_runtime_dir"], trial["scratch"]["xdg_runtime_dir"])
            self.assertFalse(init["dbus_session_bus_address_set"])
            self.assertTrue(init["cwd"].startswith(trial["scratch"]["tmpdir"]))
            seen.add(init["tmpdir"])
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(t["grade"]["verdict"] == "resolved" for t in record["trials"]))

    def test_a_hanging_engine_is_killed_with_its_helper_and_counts_as_a_timeout(self):
        # The engine never ends by itself; the 1-second limit is the behaviour under test, not a wait.
        record, lines, leftovers = self.run_suite({"t-hang": tiny_task("t-hang", ["hang"], timeout=1)})
        trial = record["trials"][0]
        self.assertEqual(trial["error"], {"class": "timeout", "tier": "agent",
                                          "detail": "engine ran past 1 s and was killed"})
        self.assertTrue(trial["engine"]["timed_out"])
        self.assertIsNone(trial["engine"]["claimed_done"])
        self.assertEqual(trial["grade"]["verdict"], "unresolved")
        helper = [line for line in lines[trial["trial_id"]] if line.get("subtype") == "stand_in_helper"]
        self.assertEqual(len(helper), 1)
        pid = helper[0]["helper_pid"]
        state = None
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                state = handle.read().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            pass
        self.assertIn(state, (None, "Z", "X"))
        self.assertEqual(leftovers, ([], []))

    def test_start_tree_mismatch_is_infra_and_not_graded(self):
        record, _, _ = self.run_suite({"t-bad": tiny_task("t-bad", ["solve"])}, bad_tree_for=("t-bad",),
                                      replacements=0)
        trial = record["trials"][0]
        self.assertEqual(trial["error"]["class"], "start_tree_mismatch")
        self.assertEqual(trial["error"]["tier"], "infra")
        self.assertEqual(trial["grade"]["verdict"], "not_graded")

    def test_engine_missing_in_an_attempt_is_infra(self):
        class Missing(arms.DirectArm):
            def argv(self):
                return ["/nonexistent/eval-engine"]

        record, _, _ = self.run_suite({"t-miss": tiny_task("t-miss", ["solve"])}, arm=Missing("standin"),
                                      replacements=1)
        self.assertEqual([t["error"]["class"] for t in record["trials"]], ["engine_missing", "engine_missing"])
        self.assertFalse(record["trials"][1]["attempt"]["planned"])

    def test_false_done_and_give_up(self):
        record, _, _ = self.run_suite({"t-wrong": tiny_task("t-wrong", ["wrong", "give_up", "idle"])}, k=3)
        rows = sorted(record["trials"], key=lambda t: t["attempt"]["index"])
        self.assertEqual([t["engine"]["claimed_done"] for t in rows], [True, False, True])
        self.assertEqual([t["error"]["class"] for t in rows], ["tests_failed", "engine_error", "tests_failed"])
        self.assertEqual([t["artifacts"]["patch_empty"] for t in rows], [False, True, True])

    def test_attempts_below_k_refused(self):
        with scratch_bases() as (tmp, xdg):
            suite = suite_mod.load_dev(runner.DEV_SUITE)
            with self.assertRaises(runner.RunRefused):
                runner.run(suite, [arms.DirectArm("standin")], k=5, attempts=3, tmp_base=tmp, xdg_base=xdg)

    def test_pinned_engine_version_must_match(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            manifest = make_suite(folder, {"t-pin": tiny_task("t-pin", ["solve"])}, tmp_base=tmp, xdg_base=xdg,
                                  extra={"engines": {"standin": "eval-standin/0"}})
            suite = suite_mod.load_dev(manifest)
            outcome = runner.run(suite, [arms.DirectArm("standin")], k=1, out_dir=folder / "out", tmp_base=tmp,
                                 xdg_base=xdg, log=lambda _m: None)
        self.assertEqual(outcome.records, {})
        self.assertIn("eval-standin/0", outcome.refused["direct"])

    def test_pinned_runner_must_match(self):
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            manifest = make_suite(folder, {"t-pin": tiny_task("t-pin", ["solve"])}, tmp_base=tmp, xdg_base=xdg,
                                  extra={"runner": {"version": "0.0.0", "sha256": "0" * 64}})
            suite = suite_mod.load_dev(manifest)
            with self.assertRaises(runner.RunRefused):
                runner.run(suite, [arms.DirectArm("standin")], k=1, out_dir=folder / "out", tmp_base=tmp,
                           xdg_base=xdg, log=lambda _m: None)

    def test_two_runs_of_one_pinned_suite_compare_paired(self):
        tasks = {f"t-{i}": tiny_task(f"t-{i}", ["solve"] if i < 3 else ["wrong"]) for i in range(5)}
        with scratch_bases() as (tmp, xdg), outside_folder() as folder:
            manifest = make_suite(folder, tasks, tmp_base=tmp, xdg_base=xdg)
            suite = suite_mod.load_dev(manifest)
            paths = []
            for _ in range(2):
                outcome = runner.run(suite, [arms.DirectArm("standin")], k=1, out_dir=folder / "out", tmp_base=tmp,
                                     xdg_base=xdg, log=lambda _m: None)
                paths.append(outcome.records["direct"])
            a, b = (record_mod.load(p) for p in paths)
        out = analyze.compare(a, b, resamples=1000)
        self.assertEqual(out["pass_hat_k"]["tasks"], 5)
        self.assertEqual(out["pass_hat_k"]["bootstrap"], (0.0, 0.0))
        self.assertIn("no detectable difference", out["pass_hat_k"]["claim"])


if __name__ == "__main__":
    unittest.main()
