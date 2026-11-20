"""The route table (eval/routes.py): several stand-in records, one per route, side by side."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from eval import passk, routes
from eval import record as record_mod
from eval.tests import build_record, trial


def usage(cost, *, unit="usd", basis="reported_not_billed"):
    """Usage with a known cost (a number) or an unknown one (None, never 0)."""
    out = record_mod.empty_usage()
    if cost is not None:
        out.update(cost=cost, cost_unit=unit, cost_basis=basis, source="engine_output")
    return out


def settle(record, *, permission_mode=None):
    """Hash the arm's settings as eval/arms.py does (model and permission mode included) and re-hash the plan."""
    arm = record["arm"]
    arm["settings_sha256"] = record_mod.sha256_json({
        "engine": arm["engine"], "model": arm["model"], "route": arm["route"], "effort": arm["effort"],
        "permission_mode": permission_mode, "extra_args": []})
    plan = record["plan"]
    body = {key: plan[key] for key in plan if key != "plan_sha256"}
    plan["plan_sha256"] = record_mod.sha256_json(dict(body, suite_manifest_sha256=record["suite"]["manifest_sha256"],
                                                      arm_settings_sha256=arm["settings_sha256"]))
    return record_mod.require_valid(record)


def standin_record(model, outcomes, *, record_id, cost=None, label="dev", k=2, permission_mode=None):
    """A stand-in record of one route (the arm's --model): outcomes {task_id: ['pass'|'fail', ...]}."""
    rows = []
    for task_id, results in outcomes.items():
        for index, outcome in enumerate(results, start=1):
            rows.append(trial(task_id, index, outcome, usage=usage(cost), record_id=record_id))
    attempts = len(next(iter(outcomes.values())))
    record = build_record(rows, k=k, attempts=attempts, label=label, record_id=record_id)
    record["arm"]["model"] = model
    return settle(record, permission_mode=permission_mode)


def pilot(record):
    """The same record as a pilot would carry it: held-out frozen suite, a real engine, k of at least 5."""
    record["label"] = "pilot"
    record["suite"].update(split="held_out", frozen=True)
    record["arm"].update(engine="claude_code", engine_version="2.1.0")
    return settle(record)


def table(records):
    """Write the records into a temp folder, run the command on them, return (exit, stdout lines, stderr)."""
    with tempfile.TemporaryDirectory(prefix="eval-routes-") as folder:
        paths = []
        for i, record in enumerate(records):
            path = Path(folder) / f"{i}-{record['record_id']}.json"
            path.write_text(record_mod.dumps(record), encoding="utf-8")
            paths.append(str(path))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = routes.main(paths)
    return code, out.getvalue().splitlines(), err.getvalue()


def fields(line):
    parts = line.split("\t")
    return parts[0], parts[1], dict(p.split("=", 1) for p in parts[2:])


def route_name(model, engine="standin"):
    return json.dumps([engine, model, None, None])


def route_lines(out):
    return {f["route"]: f for _, kind, f in map(fields, out) if kind == "route"}


class RouteTableTests(unittest.TestCase):
    def test_two_routes_give_one_row_per_task_route_and_repeat(self):
        a = standin_record("model-a", {"t-1": ["pass", "pass", "fail"], "t-2": ["fail", "fail", "fail"]},
                           record_id="ra", cost=0.5)
        b = standin_record("model-b", {"t-1": ["pass", "pass", "pass"], "t-2": ["pass", "fail", "pass"]},
                           record_id="rb", cost=0.25)
        code, out, err = table([a, b])
        self.assertEqual(code, 0, err)
        rows = [fields(line) for line in out if fields(line)[1] == "row"]
        keys = [(f["route"], f["task"], f["repeat"]) for _, _, f in rows]
        self.assertEqual(len(keys), 2 * 2 * 3)
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual({k[0] for k in keys}, {route_name("model-a"), route_name("model-b")})
        summary = route_lines(out)
        self.assertEqual(len(summary), 2)
        # pass^2 from eval/passk.py on the planned counts: a = mean(C(2,2)/C(3,2), 0) = 1/6;
        # b = mean(1, C(2,2)/C(3,2)) = 2/3.
        expected_a = (passk.pass_hat_k(passk.Counts(3, 2), 2) + passk.pass_hat_k(passk.Counts(3, 0), 2)) / 2
        self.assertEqual(expected_a, Fraction(1, 6))
        self.assertEqual(summary[route_name("model-a")]["pass^k_planned"], f"{float(expected_a):.4f}")
        self.assertEqual(summary[route_name("model-b")]["pass^k_planned"], f"{2 / 3:.4f}")

    def test_both_cost_figures_on_the_reads_example(self):
        # Route a: two of six trials pass, one task solved, 3.00 in all; route b: 1.50 in all, five
        # passing trials, two tasks solved.
        a = standin_record("model-a", {"t-1": ["pass", "pass", "fail"], "t-2": ["fail", "fail", "fail"]},
                           record_id="ra", cost=0.5)
        b = standin_record("model-b", {"t-1": ["pass", "pass", "pass"], "t-2": ["pass", "fail", "pass"]},
                           record_id="rb", cost=0.25)
        code, out, err = table([a, b])
        self.assertEqual(code, 0, err)
        ra, rb = route_lines(out)[route_name("model-a")], route_lines(out)[route_name("model-b")]
        self.assertEqual((ra["known_cost"], ra["passing_trials"], ra["solved_tasks"]), ("3.0000 usd", "2", "1"))
        self.assertEqual(ra["cost_per_passing_trial"], "1.5000 usd")
        self.assertEqual(ra["cost_per_solved_task"], "3.0000 usd")
        self.assertEqual(rb["cost_per_passing_trial"], "0.3000 usd")
        self.assertEqual(rb["cost_per_solved_task"], "0.7500 usd")
        legends = [f for _, kind, f in map(fields, out) if kind == "legend"]
        self.assertEqual(len(legends), 2)
        self.assertIn("passing trials", legends[0]["cost_per_passing_trial"])
        self.assertIn("distinct tasks", legends[0]["cost_per_solved_task"])
        self.assertIn("planned trials only", legends[0]["pass^k_planned"])
        self.assertIn("replacements included", legends[0]["costs"])

    def test_an_unknown_cost_prints_unknown_never_zero(self):
        record = standin_record("model-a", {"t-1": ["pass", "fail"], "t-2": ["pass", "pass"]}, record_id="ru")
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        for _, kind, f in map(fields, out):
            if kind == "row":
                self.assertEqual(f["cost"], "unknown")
            elif kind == "route":
                for key in ("known_cost", "cost_per_passing_trial", "cost_per_solved_task"):
                    self.assertEqual(f[key], "unknown")

    def test_one_unknown_cost_makes_the_route_cost_unknown(self):
        record = standin_record("model-a", {"t-1": ["pass", "pass"], "t-2": ["pass", "fail"]}, record_id="rm",
                                cost=1.0)
        record["trials"][0]["usage"] = usage(None)
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        summary = list(route_lines(out).values())
        self.assertEqual(summary[0]["cost_per_passing_trial"], "unknown")
        self.assertEqual(summary[0]["cost_per_solved_task"], "unknown")
        self.assertEqual(summary[0]["cost_unknown"], "1")

    def test_a_tiny_known_cost_never_prints_as_zero(self):
        record = standin_record("model-a", {"t-1": ["pass", "pass"]}, record_id="rt", cost=0.00004)
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        for _, kind, f in map(fields, out):
            for key in ("cost", "known_cost", "cost_per_passing_trial", "cost_per_solved_task"):
                if key in f:
                    self.assertNotRegex(f[key], r"^0\.0+ ", f"{key}={f[key]}")
        self.assertEqual(list(route_lines(out).values())[0]["cost_per_passing_trial"], "0.00004000 usd")

    def test_a_route_that_solves_nothing(self):
        record = standin_record("model-a", {"t-1": ["fail", "fail"], "t-2": ["fail", "fail"]}, record_id="rn",
                                cost=0.5)
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        f = list(route_lines(out).values())[0]
        self.assertEqual((f["passing_trials"], f["solved_tasks"]), ("0", "0"))
        self.assertEqual(f["cost_per_passing_trial"], "none-passing")
        self.assertEqual(f["cost_per_solved_task"], "none-solved")
        self.assertEqual(f["known_cost"], "2.0000 usd")

    def test_more_than_one_cost_unit_is_named_never_added(self):
        record = standin_record("model-a", {"t-1": ["pass", "pass"]}, record_id="rx", cost=1.0)
        record["trials"][1]["usage"] = usage(1.0, unit="eur")
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        f = list(route_lines(out).values())[0]
        for key in ("known_cost", "cost_per_passing_trial", "cost_per_solved_task"):
            self.assertEqual(f[key], "mixed-units:eur+usd")

    def test_billed_and_not_billed_costs_are_never_added(self):
        record = standin_record("model-a", {"t-1": ["pass", "pass"]}, record_id="rb", cost=1.0)
        record["trials"][1]["usage"] = usage(1.0, basis="billed")
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        f = list(route_lines(out).values())[0]
        self.assertEqual(f["cost_basis"], "billed+reported_not_billed")
        for key in ("known_cost", "cost_per_passing_trial", "cost_per_solved_task"):
            self.assertEqual(f[key], "mixed-basis:billed+reported_not_billed")
        rows = [f for _, kind, f in map(fields, out) if kind == "row"]
        self.assertEqual(sorted(r["basis"] for r in rows), ["billed", "reported_not_billed"])

    def test_one_basis_is_printed_beside_the_cost(self):
        record = standin_record("model-a", {"t-1": ["pass", "pass"]}, record_id="rb", cost=1.0)
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        self.assertEqual(list(route_lines(out).values())[0]["cost_basis"], "reported_not_billed")

    def test_replacement_and_not_run_trials(self):
        # k=1, two planned attempts. t-1: planned 1 hits a vendor limit (infra, cost known), planned 2
        # passes, replacement 3 passes. t-2: planned 1 never ran, planned 2 fails.
        rows = [trial("t-1", 1, "vendor_limit", usage=usage(0.5), record_id="rr"),
                trial("t-1", 2, "pass", usage=usage(0.5), record_id="rr"),
                trial("t-1", 3, "pass", planned=False, replacement_for="rr.t-1.1", usage=usage(0.5), record_id="rr"),
                trial("t-2", 1, "not_run", record_id="rr"),
                trial("t-2", 2, "fail", usage=usage(0.5), record_id="rr")]
        record = build_record(rows, k=1, attempts=2, record_id="rr")
        record["arm"]["model"] = "model-r"
        settle(record)
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        rows_out = [f for _, kind, f in map(fields, out) if kind == "row"]
        self.assertEqual(len(rows_out), 5)
        self.assertEqual([r["planned"] for r in rows_out if r["task"] == "t-1"], ["yes", "yes", "no"])
        self.assertEqual([r["cost"] for r in rows_out if r["task"] == "t-2"], ["unknown", "0.5000 usd"])
        f = list(route_lines(out).values())[0]
        # pass^1 over planned trials only: t-1 1 of 2, t-2 0 of 2 (the not_run counts as failed) = 0.25.
        self.assertEqual(f["pass^k_planned"], "0.2500")
        # Costs over the four trials that ran, the replacement included and the not_run left out.
        self.assertEqual((f["trials_ran"], f["passing_trials"], f["solved_tasks"]), ("4", "2", "1"))
        self.assertEqual(f["known_cost"], "2.0000 usd")
        self.assertEqual(f["cost_per_passing_trial"], "1.0000 usd")
        self.assertEqual(f["cost_per_solved_task"], "2.0000 usd")
        self.assertEqual(f["cost_unknown"], "0")

    def test_a_pilot_record_prints_pilot_on_every_line(self):
        record = pilot(standin_record("model-p", {"t-1": ["pass"] * 5, "t-2": ["fail"] * 5}, record_id="rp",
                                      k=5))
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out), 2 * 5 + 2)
        for line in out:
            self.assertEqual(line.split("\t")[0], "pilot", line)

    def test_each_record_keeps_its_own_label(self):
        dev = standin_record("model-a", {"t-1": ["pass"] * 5}, record_id="rd", k=5)
        pil = pilot(standin_record("model-p", {"t-1": ["pass"] * 5}, record_id="rp", k=5))
        code, out, err = table([dev, pil])
        self.assertEqual(code, 0, err)
        for label, _, f in map(fields, out):
            if "route" in f:
                self.assertEqual(label, "pilot" if f["route"] == route_name("model-p", "claude_code") else "dev")
        # A legend line names no route: it belongs to the route line that follows it, and carries that
        # record's label.
        kinds = [(label, kind) for label, kind, _ in map(fields, out) if kind in ("legend", "route")]
        self.assertEqual(kinds, [("dev", "legend"), ("dev", "route"), ("pilot", "legend"), ("pilot", "route")])

    def test_a_model_name_with_a_slash_keeps_its_route(self):
        a = standin_record("vendor/model", {"t-1": ["pass", "pass"]}, record_id="r1")
        b = standin_record("vendor", {"t-1": ["pass", "pass"]}, record_id="r2")
        b["arm"]["route"] = "model"
        settle(b)
        code, out, err = table([a, b])
        self.assertEqual(code, 0, err)
        summary = route_lines(out)
        self.assertEqual(set(summary), {route_name("vendor/model"), json.dumps(["standin", "vendor", "model", None])})
        self.assertEqual(json.loads(summary[route_name("vendor/model")]["route"])[1], "vendor/model")

    def test_records_that_differ_only_in_permission_mode_are_two_routes(self):
        a = standin_record("model-a", {"t-1": ["pass", "pass"]}, record_id="r1")
        b = standin_record("model-a", {"t-1": ["fail", "fail"]}, record_id="r2", permission_mode="plan")
        code, out, err = table([a, b])
        self.assertEqual(code, 0, err)
        summary = [f for _, kind, f in map(fields, out) if kind == "route"]
        self.assertEqual(len(summary), 2)
        self.assertEqual(len({f["settings"] for f in summary}), 2)

    def test_two_records_of_one_route_are_refused(self):
        a = standin_record("model-a", {"t-1": ["pass", "pass"]}, record_id="r1")
        b = standin_record("model-a", {"t-1": ["fail", "fail"]}, record_id="r2")
        code, out, err = table([a, b])
        self.assertEqual(code, 2)
        self.assertEqual(out, [])
        self.assertIn("one record per route", err)

    def test_an_invalid_record_is_refused(self):
        record = standin_record("model-a", {"t-1": ["pass", "pass"]}, record_id="r1")
        record["label"] = "pilot"   # a pilot of the stand-in engine on the dev set: invalid
        code, out, err = table([record])
        self.assertEqual(code, 2)
        self.assertEqual(out, [])
        self.assertIn("invalid recursion-eval-run/1 record", err)
        with self.assertRaisesRegex(ValueError, "invalid recursion-eval-run/1 record"):
            routes.lines([record])

    def test_a_record_of_another_protocol_version_is_refused(self):
        record = standin_record("model-a", {"t-1": ["pass", "pass"]}, record_id="r1")
        record["protocol"]["analysis_sha256"] = "1" * 64
        code, out, err = table([record])
        self.assertEqual(code, 2)
        self.assertEqual(out, [])
        self.assertIn("unsupported version", err)

    def test_no_record_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            routes.main([])
        with self.assertRaises(ValueError):
            routes.lines([])



def table_of_files(texts):
    """Write each text as a record file named by its index only (an id may hold what no file name may), run the
    command on them, return (exit, stdout text, stderr text)."""
    with tempfile.TemporaryDirectory(prefix="eval-routes-") as folder:
        paths = []
        for i, text in enumerate(texts):
            path = Path(folder) / f"{i}.json"
            path.write_text(text, encoding="utf-8")
            paths.append(str(path))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = routes.main(paths)
    return code, out.getvalue(), err.getvalue()


class PlainFieldTests(unittest.TestCase):
    """A record file nested too deeply is refused like every other bad record file, and a value the table prints
    raw holds neither its field separator (TAB) nor a character str.isprintable() refuses."""

    def refused(self, record, field, value):
        record_mod.require_valid(record)   # the validator accepts it, so the table itself must refuse it
        code, out, err = table_of_files([record_mod.dumps(record)])
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("refused: "), err)
        self.assertIn(field, err)
        self.assertNotIn(value, err)

    def test_a_record_file_nested_too_deeply_is_refused(self):
        code, out, err = table_of_files(["[" * 200000 + "]" * 200000])
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("refused: "), err)
        self.assertNotIn("Traceback", err)

    def test_a_tab_in_task_id_is_refused(self):
        task_id = "t-1\tverdict=pass\tcost=0"
        self.refused(standin_record("model-a", {task_id: ["fail", "fail"]}, record_id="r1"), "task_id", task_id)

    def test_a_newline_in_record_id_is_refused(self):
        record_id = "rec\nok\troute\tforged=1"
        self.refused(standin_record("model-a", {"t-1": ["fail", "fail"]}, record_id=record_id),
                     "record_id", record_id)

    def test_a_line_separator_in_record_id_is_refused(self):
        record_id = "rec forged"
        self.refused(standin_record("model-a", {"t-1": ["fail", "fail"]}, record_id=record_id),
                     "record_id", record_id)

    def test_every_unprintable_character_in_an_id_is_refused(self):
        for char in ("\r", "\x00", "\x1b", "\x1f", "\x7f", "\x85", " "):
            with self.subTest(char=hex(ord(char))):
                self.refused(standin_record("model-a", {"t-1": ["fail", "fail"]}, record_id="rec" + char + "x"),
                             "record_id", "rec" + char + "x")
                self.refused(standin_record("model-a", {"t" + char + "1": ["fail", "fail"]}, record_id="r1"),
                             "task_id", "t" + char + "1")

    def test_a_tab_in_cost_unit_is_refused(self):
        record = standin_record("model-a", {"t-1": ["pass", "fail"]}, record_id="r1", cost=0.5)
        record["trials"][1]["usage"]["cost_unit"] = "usd\tverdict=pass"
        self.refused(record, "cost_unit", "usd\tverdict=pass")

    def test_an_empty_id_is_refused_by_the_table_itself(self):
        # record.py refuses an empty id first; the table's own rule is pinned by calling it directly
        record = standin_record("model-a", {"t-1": ["fail", "fail"]}, record_id="r1")
        record["record_id"] = ""
        with self.assertRaisesRegex(ValueError, "record 1: record_id"):
            routes.require_plain(record, "record 1")
        record["record_id"], record["trials"][0]["task_id"] = "r1", ""
        with self.assertRaisesRegex(ValueError, r"record 1: trials\[0\]\.task_id"):
            routes.require_plain(record, "record 1")

    def test_printable_non_ascii_ids_still_print(self):
        record = standin_record("model-a", {"t-é": ["pass", "pass"]}, record_id="r-é")
        code, out, err = table_of_files([record_mod.dumps(record)])
        self.assertEqual(code, 0, err)
        rows = [fields(line) for line in out.split("\n") if line]
        self.assertIn("task=t-é", out)
        self.assertEqual([f["record"] for _, kind, f in rows if kind == "route"], ["r-é"])


if __name__ == "__main__":
    unittest.main()
