"""Price schedules (eval/price_schedules.py): eval records re-costed under named, made-up price lists."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from eval import price_schedules
from eval import record as record_mod
from eval.tests import build_record, trial

MARK = "modelled, not billed"

# Two made-up schedules, per million tokens. Output to cache-read price: 8 / 0.1 = 80:1 and 6 / 0.5 = 12:1.
SCHEDULES = {"schedules": [
    {"name": "list-80", "unit": "usd",
     "per_million": {"uncached_input": 1, "cache_write": 1, "cache_read": 0.1, "output": 8}},
    {"name": "list-12", "unit": "usd",
     "per_million": {"uncached_input": 1, "cache_write": 1, "cache_read": 0.5, "output": 6}},
]}


def tokens(*, uncached_input=0, cache_write=0, cache_read=0, output=0, cost=None, basis=None):
    """Usage with the four token classes (None = not reported) and, if given, the record's own cost."""
    out = record_mod.empty_usage()
    out.update(uncached_input=uncached_input, cache_write=cache_write, cache_read=cache_read, output=output,
               source="engine_output")
    if cost is not None:
        out.update(cost=cost, cost_unit="usd", cost_basis=basis or "reported_not_billed")
    return out


def route_record(model, outcomes, usage, *, record_id, label="dev", k=2, arm="direct", manifest_sha=None):
    """One route's record (the arm's model): outcomes {task_id: ['pass'|'fail', ...]}, the same usage per trial."""
    rows = []
    for task_id, results in outcomes.items():
        for index, outcome in enumerate(results, start=1):
            rows.append(trial(task_id, index, outcome, usage=dict(usage), record_id=record_id))
    attempts = len(next(iter(outcomes.values())))
    extra = {} if manifest_sha is None else {"manifest_sha": manifest_sha}
    record = build_record(rows, k=k, attempts=attempts, label=label, record_id=record_id, arm=arm, **extra)
    record["arm"]["model"] = model
    return record


def pilot(record):
    """The same record as a pilot carries it: held-out frozen suite, a real engine, k of at least 5."""
    record["label"] = "pilot"
    record["suite"].update(split="held_out", frozen=True)
    record["arm"]["engine"] = "claude_code"
    return record_mod.require_valid(record)


def run(records, schedules=SCHEDULES):
    """Write the records and schedules into a temp folder, run the command, return (exit, lines, stderr)."""
    with tempfile.TemporaryDirectory(prefix="eval-prices-") as folder:
        sched_path = Path(folder) / "schedules.json"
        sched_path.write_text(json.dumps(schedules), encoding="utf-8")
        paths = []
        for record in records:
            path = Path(folder) / f"{record['record_id']}.json"
            path.write_text(record_mod.dumps(record), encoding="utf-8")
            paths.append(str(path))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = price_schedules.main(["--schedules", str(sched_path), *paths])
    return code, out.getvalue().splitlines(), err.getvalue()


def parse(line):
    """(label, kind, mark, {key: value}) of one tab-separated line."""
    parts = line.split("\t")
    return parts[0], parts[1], parts[2], dict(p.split("=", 1) for p in parts[3:])


# Route A answers mostly in output; route B mostly from the cache. Per trial:
#   A: 1,000,000 output                      -> list-80: 8.00, list-12: 6.00
#   B: 100,000 output + 20,000,000 cache read -> list-80: 0.80 + 2.00 = 2.80, list-12: 0.60 + 10.00 = 10.60
OUTCOMES = {"t-1": ["pass", "pass"], "t-2": ["pass", "fail"]}


class PriceScheduleTests(unittest.TestCase):
    def test_the_cheaper_route_flips_between_an_80_to_1_and_a_12_to_1_schedule(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1_000_000), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(output=100_000, cache_read=20_000_000), record_id="rb")
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        costs = {(f["schedule"], f["route"]): f for _, kind, _, f in map(parse, out) if kind == "cost"}
        self.assertEqual(len(costs), 4)
        # 4 trials, 3 of them passing.
        self.assertEqual(costs[("list-80", R("model-a"))]["per_trial_ran"], "8.000000 usd")
        self.assertEqual(costs[("list-80", R("model-a"))]["per_passing_trial"], "10.666667 usd")
        self.assertEqual(costs[("list-80", R("model-b"))]["per_trial_ran"], "2.800000 usd")
        self.assertEqual(costs[("list-12", R("model-a"))]["per_trial_ran"], "6.000000 usd")
        self.assertEqual(costs[("list-12", R("model-b"))]["per_trial_ran"], "10.600000 usd")
        cheaper = {f["schedule"]: f["route"] for _, kind, _, f in map(parse, out) if kind == "cheaper"}
        self.assertEqual(cheaper, {"list-80": R("model-b"), "list-12": R("model-a")})
        flips = [f for _, kind, _, f in map(parse, out) if kind == "flips"]
        self.assertEqual(flips, [{"flips": "yes", "cheaper": "2", "ties": "0", "unknowns": "0",
                                  "list-80": R("model-b"), "list-12": R("model-a")}])

    def test_flips_is_no_when_one_route_is_cheaper_under_every_schedule(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1_000_000), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(output=100_000), record_id="rb")
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        flips = [f for _, kind, _, f in map(parse, out) if kind == "flips"]
        self.assertEqual([f["flips"] for f in flips], ["no"])

    def test_a_null_cache_read_makes_the_cost_unknown_never_zero(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1_000_000, cache_read=None), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(output=100_000, cache_read=20_000_000), record_id="rb")
        b["trials"][0]["usage"]["cache_read"] = None
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        for _, kind, _, f in map(parse, out):
            if kind == "cost":
                self.assertEqual(f["per_trial_ran"], "unknown", f)
                self.assertEqual(f["per_passing_trial"], "unknown", f)
                self.assertEqual(f["per_solved_task"], "unknown", f)
            if kind == "cheaper":
                self.assertEqual(f["result"], "UNKNOWN", f)
        self.assertNotIn("0.000000 usd", "\n".join(out))

    def test_a_billed_record_cost_prints_as_billed_beside_the_modelled_cost(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1_000_000, cost=3.0, basis="billed"), record_id="ra")
        code, out, err = run([a])
        self.assertEqual(code, 0, err)
        costs = [f for _, kind, _, f in map(parse, out) if kind == "cost"]
        recorded = [f for _, kind, _, f in map(parse, out) if kind == "recorded"]
        self.assertEqual((len(costs), len(recorded)), (2, 1))
        self.assertEqual(recorded[0]["basis"], "billed")
        # The record's own 4 x 3.00 for 3 passing trials, never merged into the modelled figure.
        self.assertEqual(recorded[0]["per_passing_trial"], "4.000000 usd")
        for f in costs:
            self.assertNotEqual(f["per_passing_trial"], recorded[0]["per_passing_trial"])
        self.assertEqual({f["per_passing_trial"] for f in costs}, {"10.666667 usd", "8.000000 usd"})

    def test_a_pilot_label_is_on_every_line_and_the_modelled_mark_on_every_line_but_recorded(self):
        outcomes = {"t-1": ["pass"] * 5, "t-2": ["fail"] * 5}
        a = pilot(route_record("model-a", outcomes, tokens(output=1_000_000), record_id="ra", k=5))
        b = pilot(route_record("model-b", outcomes, tokens(output=100_000, cache_read=20_000_000),
                               record_id="rb", k=5))
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        self.assertTrue(out)
        self.assertIn("flips", [parse(line)[1] for line in out])
        for line in out:
            label, kind, mark, _ = parse(line)
            self.assertEqual(label, "pilot", line)
            self.assertEqual(mark, "recorded, unknown" if kind == "recorded" else MARK, line)

    def test_two_records_of_one_route_are_refused(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1), record_id="r1")
        b = route_record("model-a", OUTCOMES, tokens(output=2), record_id="r2")
        code, out, err = run([a, b])
        self.assertEqual(code, 2)
        self.assertEqual(out, [])
        self.assertIn("one record per route", err)

    def test_a_schedule_missing_a_token_class_is_refused(self):
        bad = {"schedules": [{"name": "short", "unit": "usd",
                              "per_million": {"uncached_input": 1, "cache_write": 1, "output": 8}}]}
        a = route_record("model-a", OUTCOMES, tokens(output=1), record_id="ra")
        code, out, err = run([a], bad)
        self.assertEqual(code, 2)
        self.assertEqual(out, [])
        self.assertIn("cache_read", err)


def lines_of(out, kind):
    return [f for _, k, _, f in map(parse, out) if k == kind]


def write_and_run(records, schedules_text):
    """Like run(), with the schedules file given as raw text."""
    with tempfile.TemporaryDirectory(prefix="eval-prices-") as folder:
        sched_path = Path(folder) / "schedules.json"
        sched_path.write_text(schedules_text, encoding="utf-8")
        paths = []
        for record in records:
            path = Path(folder) / f"{record['record_id']}.json"
            path.write_text(record_mod.dumps(record), encoding="utf-8")
            paths.append(str(path))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = price_schedules.main(["--schedules", str(sched_path), *paths])
    return code, out.getvalue().splitlines(), err.getvalue()


A_TOKENS = {"output": 1_000_000}
B_TOKENS = {"output": 100_000, "cache_read": 20_000_000}


class AnswerRoundTests(unittest.TestCase):
    """The read of b7e3258e (work/reads/read-prices-b7e3258e.md), order line 173."""

    def test_the_two_arms_of_one_comparison_are_two_routes(self):
        direct = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="rd", arm="direct")
        recursion = route_record("model-a", OUTCOMES, tokens(**B_TOKENS), record_id="rr", arm="recursion")
        code, out, err = run([direct, recursion])
        self.assertEqual(code, 0, err)
        routes = {f["route"] for f in lines_of(out, "cost")}
        self.assertEqual(routes, {R("model-a"), R("model-a", arm="recursion")})
        notes = lines_of(out, "note")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["arms"], "direct,recursion")

    def test_one_arm_gives_no_arm_note(self):
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(**B_TOKENS), record_id="rb")
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        self.assertEqual(lines_of(out, "note"), [])

    def test_per_passing_trial_and_per_solved_task_are_both_given_and_named(self):
        # 4 trials ran, 3 passing, 2 distinct tasks solved; list-80 total for A = 4 x 8.00 = 32.00.
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        code, out, err = run([a])
        self.assertEqual(code, 0, err)
        cost = {f["schedule"]: f for f in lines_of(out, "cost")}["list-80"]
        self.assertEqual(cost["passing_trials"], "3")
        self.assertEqual(cost["solved_tasks"], "2")
        self.assertEqual(cost["per_trial_ran"], "8.000000 usd")
        self.assertEqual(cost["per_passing_trial"], "10.666667 usd")
        self.assertEqual(cost["per_solved_task"], "16.000000 usd")

    def test_a_tie_is_said_in_words(self):
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(**A_TOKENS), record_id="rb")
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        for f in lines_of(out, "cheaper"):
            self.assertEqual(f["result"], "TIE", f)
            self.assertEqual(f["routes"], "[" + R("model-a") + "," + R("model-b") + "]")
        flips = lines_of(out, "flips")
        self.assertEqual(len(flips), 1)
        self.assertEqual((flips[0]["flips"], flips[0]["ties"], flips[0]["unknowns"]), ("no", "2", "0"))

    def test_a_change_from_one_cheaper_route_to_a_tie_counts_as_a_flip(self):
        # list-80: B 2.80 against A and C 8.00 -> B; list-12: A and C 6.00 against B 10.60 -> TIE A, C.
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(**B_TOKENS), record_id="rb")
        c = route_record("model-c", OUTCOMES, tokens(**A_TOKENS), record_id="rc")
        code, out, err = run([a, b, c])
        self.assertEqual(code, 0, err)
        results = {f["schedule"]: f["result"] for f in lines_of(out, "cheaper")}
        self.assertEqual(results, {"list-80": "CHEAPER", "list-12": "TIE"})
        flips = lines_of(out, "flips")
        self.assertEqual(len(flips), 1)
        self.assertEqual((flips[0]["flips"], flips[0]["cheaper"], flips[0]["ties"], flips[0]["unknowns"]),
                         ("yes", "1", "1", "0"))

    def test_unknowns_are_counted_on_the_flips_line(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1_000_000, cache_read=None), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(**B_TOKENS), record_id="rb")
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        for f in lines_of(out, "cheaper"):
            self.assertEqual(f["result"], "UNKNOWN", f)
        flips = lines_of(out, "flips")
        self.assertEqual(len(flips), 1)
        self.assertEqual((flips[0]["flips"], flips[0]["unknowns"]), ("unknown", "2"))

    def test_a_single_route_names_no_cheaper_route(self):
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        code, out, err = run([a])
        self.assertEqual(code, 0, err)
        cheaper = lines_of(out, "cheaper")
        self.assertEqual(len(cheaper), 2)
        for f in cheaper:
            self.assertEqual(f["result"], "UNKNOWN", f)
            self.assertIn("one route", f["reason"])
        self.assertEqual(lines_of(out, "flips"), [])

    def test_a_route_that_solved_no_task_is_never_cheaper_even_with_an_unknown_cost(self):
        none = route_record("model-a", {"t-1": ["fail", "fail"], "t-2": ["fail", "fail"]},
                            tokens(output=1, cache_read=None), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(**B_TOKENS), record_id="rb")
        code, out, err = run([none, b])
        self.assertEqual(code, 0, err)
        cost = {(f["schedule"], f["route"]): f for f in lines_of(out, "cost")}
        self.assertEqual(cost[("list-80", R("model-a"))]["per_solved_task"], "none-solved")
        self.assertEqual(cost[("list-80", R("model-a"))]["per_passing_trial"], "none-passed")
        for f in lines_of(out, "cheaper"):
            self.assertEqual((f["result"], f["route"]), ("CHEAPER", R("model-b")), f)

    def test_no_route_solving_a_task_is_unknown_in_words(self):
        fails = {"t-1": ["fail", "fail"], "t-2": ["fail", "fail"]}
        a = route_record("model-a", fails, tokens(**A_TOKENS), record_id="ra")
        b = route_record("model-b", fails, tokens(**B_TOKENS), record_id="rb")
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        for f in lines_of(out, "cheaper"):
            self.assertEqual(f["result"], "UNKNOWN", f)
            self.assertIn("no route solved a task", f["reason"])

    def test_the_recorded_figure_is_labelled_recorded_with_its_basis_never_under_the_mark(self):
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS, cost=3.0, basis="billed"), record_id="ra")
        code, out, err = run([a])
        self.assertEqual(code, 0, err)
        for line in out:
            label, kind, mark, f = parse(line)
            if any(key.startswith("recorded") for key in f) or kind == "recorded":
                self.assertNotEqual(mark, MARK, line)
        recorded = [(mark, f) for _, kind, mark, f in map(parse, out) if kind == "recorded"]
        self.assertEqual(len(recorded), 1)
        mark, f = recorded[0]
        self.assertEqual(mark, "recorded, billed")
        self.assertEqual(f["basis"], "billed")
        self.assertEqual(f["per_passing_trial"], "4.000000 usd")
        self.assertEqual(f["per_solved_task"], "6.000000 usd")

    def test_billed_and_not_billed_recorded_costs_are_never_added(self):
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS, cost=3.0, basis="billed"), record_id="ra")
        a["trials"][0]["usage"]["cost_basis"] = "reported_not_billed"
        code, out, err = run([a])
        self.assertEqual(code, 0, err)
        f = lines_of(out, "recorded")[0]
        self.assertEqual(f["basis"], "billed,reported_not_billed")
        self.assertEqual((f["per_passing_trial"], f["per_solved_task"]), ("unknown", "unknown"))


def R(model, *, arm="direct", engine="standin", route=None, effort=None):
    """A route as the output prints it: its parts as recorded, a JSON array (null where none)."""
    return json.dumps([arm, engine, model, route, effort], separators=(",", ":"))


def tiny_schedule():
    return {"schedules": [{"name": "fine", "unit": "usd",
                           "per_million": {"uncached_input": 0, "cache_write": 0, "cache_read": 0.01, "output": 1}}]}


class ThirdRoundTests(unittest.TestCase):
    """The read of ac0621eb (work/reads/read-prices-b7e3258e.md, section Answer round), order line 177."""

    def test_names_holding_a_slash_or_a_dash_are_distinct_routes(self):
        a = route_record("x/y", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        a["arm"]["effort"] = "z"
        b = route_record("x", OUTCOMES, tokens(**B_TOKENS), record_id="rb")
        b["arm"].update(route="y/-", effort="z")
        c = route_record("-", OUTCOMES, tokens(**A_TOKENS), record_id="rc")
        d = route_record(None, OUTCOMES, tokens(**B_TOKENS), record_id="rd")
        code, out, err = run([a, b, c, d])
        self.assertEqual(code, 0, err)
        routes = {f["route"] for f in lines_of(out, "cost")}
        self.assertEqual(routes, {R("x/y", effort="z"), R("x", route="y/-", effort="z"), R("-"), R(None)})

    def test_names_are_compared_exactly_as_recorded(self):
        a = route_record("Model-A", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        b = route_record("model-a", OUTCOMES, tokens(**B_TOKENS), record_id="rb")
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        self.assertEqual({f["route"] for f in lines_of(out, "cost")}, {R("Model-A"), R("model-a")})

    def test_cheaper_never_stands_beside_two_figures_that_print_the_same(self):
        # Per trial: A 1 output token = 0.000001; B 1 output + 1 cache read = 0.00000101.
        # 4 trials, 2 tasks solved: per solved task A 0.000002, B 0.00000202 (equal at 6 places).
        a = route_record("model-a", OUTCOMES, tokens(output=1), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(output=1, cache_read=1), record_id="rb")
        code, out, err = run([a, b], tiny_schedule())
        self.assertEqual(code, 0, err)
        cheaper = lines_of(out, "cheaper")
        self.assertEqual((cheaper[0]["result"], cheaper[0]["route"]), ("CHEAPER", R("model-a")))
        figures = {f["route"]: f["per_solved_task"] for f in lines_of(out, "cost")}
        self.assertEqual(figures, {R("model-a"): "0.00000200 usd", R("model-b"): "0.00000202 usd"})

    def test_different_k_is_refused_naming_it(self):
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="ra", k=1)
        b = route_record("model-b", OUTCOMES, tokens(**B_TOKENS), record_id="rb", k=2)
        code, out, err = run([a, b])
        self.assertEqual(code, 2, err)
        self.assertEqual(out, [])
        self.assertIn("k 1 against 2", err)

    def test_a_task_that_never_ran_on_one_route_makes_the_comparison_unknown(self):
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        b = route_record("model-b", {"t-1": ["pass", "pass"], "t-2": ["not_run", "not_run"]}, tokens(**B_TOKENS),
                         record_id="rb")
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        cost = {(f["schedule"], f["route"]): f for f in lines_of(out, "cost")}
        self.assertEqual(cost[("list-80", R("model-b"))]["trials"], "2")
        for f in lines_of(out, "cheaper"):
            self.assertEqual(f["result"], "UNKNOWN", f)
            self.assertEqual(f["reason"], f"the tasks that ran differ: {R('model-b')} ran no trial of t-2")

    def test_a_route_where_nothing_ran_prints_none_ran_and_is_not_compared(self):
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        b = route_record("model-b", {"t-1": ["not_run", "not_run"], "t-2": ["not_run", "not_run"]},
                         tokens(**B_TOKENS), record_id="rb")
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        cost = {(f["schedule"], f["route"]): f for f in lines_of(out, "cost")}
        f = cost[("list-80", R("model-b"))]
        self.assertEqual((f["trials"], f["per_trial_ran"], f["per_passing_trial"], f["per_solved_task"]),
                         ("0", "none-ran", "none-passed", "none-solved"))
        for f in lines_of(out, "cheaper"):
            self.assertEqual(f["result"], "UNKNOWN", f)
            self.assertEqual(f["reason"], f"the tasks that ran differ: {R('model-b')} ran no trial of t-1, t-2")

    def test_the_legend_says_the_none_words_count_as_unknown_and_names_are_exact(self):
        legend = " ".join(price_schedules.__doc__.split())
        self.assertIn("none-ran, none-passed and none-solved count as unknown in a comparison", legend)
        self.assertIn("compared exactly as recorded", legend)


class RefusalTests(unittest.TestCase):
    def assertRefused(self, records, schedules_text, words):
        code, out, err = write_and_run(records, schedules_text)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, [])
        for word in words:
            self.assertIn(word, err)

    def test_different_suites_are_refused_naming_the_difference(self):
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(**B_TOKENS), record_id="rb", manifest_sha="1" * 64)
        self.assertRefused([a, b], json.dumps(SCHEDULES), ["suite", "manifest_sha256", "ra", "rb"])

    def test_different_task_sets_are_refused_naming_the_difference(self):
        a = route_record("model-a", OUTCOMES, tokens(**A_TOKENS), record_id="ra")
        b = route_record("model-b", {"t-1": ["pass", "pass"], "t-3": ["pass", "fail"]}, tokens(**B_TOKENS),
                         record_id="rb")
        self.assertRefused([a, b], json.dumps(SCHEDULES), ["task", "t-2", "t-3"])

    def test_the_same_arm_and_route_twice_is_refused(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1), record_id="r1", arm="recursion")
        b = route_record("model-a", OUTCOMES, tokens(output=2), record_id="r2", arm="recursion")
        self.assertRefused([a, b], json.dumps(SCHEDULES), [R("model-a", arm="recursion"), "one record per route"])

    def test_an_invalid_record_is_refused(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1), record_id="ra")
        del a["label"]
        self.assertRefused([a], json.dumps(SCHEDULES), ["invalid"])

    def test_every_bad_schedules_file_is_refused(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1), record_id="ra")
        good = {"uncached_input": 1, "cache_write": 1, "cache_read": 0.1, "output": 8}

        def one(name="s", unit="usd", per_million=None, **extra):
            item = {"name": name, "unit": unit, "per_million": good if per_million is None else per_million}
            item.update(extra)
            return json.dumps({"schedules": [item]})

        cases = {
            "not json": ("{", ["refused"]),
            "another top-level key": (json.dumps({"schedules": [], "other": 1}), ["schedules"]),
            "an empty list": (json.dumps({"schedules": []}), ["non-empty"]),
            "an extra key in a schedule": (one(note="x"), ["name, unit, per_million"]),
            "an extra token class": (one(per_million=dict(good, reasoning=1)), ["reasoning"]),
            "a missing token class": (one(per_million={k: v for k, v in good.items() if k != "output"}),
                                      ["output"]),
            "a negative price": (one(per_million=dict(good, output=-1)), ["negative"]),
            "a boolean price": (one(per_million=dict(good, output=True)), ["number"]),
            "a text price": (one(per_million=dict(good, output="8")), ["number"]),
            "a NaN price": ('{"schedules": [{"name": "s", "unit": "usd", "per_million": {"uncached_input": 1, '
                            '"cache_write": 1, "cache_read": NaN, "output": 8}}]}', ["NaN"]),
            "a name with a tab": (one(name="a\tb"), ["name"]),
            "a name with =": (one(name="a=b"), ["name"]),
            "an empty unit": (one(unit=""), ["unit"]),
            "a name given twice": (json.dumps({"schedules": [json.loads(one())["schedules"][0]] * 2}), ["twice"]),
        }
        for what, (text, words) in cases.items():
            with self.subTest(what):
                self.assertRefused([a], text, words)


if __name__ == "__main__":
    unittest.main()
