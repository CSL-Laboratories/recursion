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


def route_record(model, outcomes, usage, *, record_id, label="dev", k=2):
    """One route's record (the arm's model): outcomes {task_id: ['pass'|'fail', ...]}, the same usage per trial."""
    rows = []
    for task_id, results in outcomes.items():
        for index, outcome in enumerate(results, start=1):
            rows.append(trial(task_id, index, outcome, usage=dict(usage), record_id=record_id))
    attempts = len(next(iter(outcomes.values())))
    record = build_record(rows, k=k, attempts=attempts, label=label, record_id=record_id)
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
        self.assertEqual(costs[("list-80", "standin/model-a/-/-")]["per_trial"], "8.000000 usd")
        self.assertEqual(costs[("list-80", "standin/model-a/-/-")]["per_solved"], "10.666667 usd")
        self.assertEqual(costs[("list-80", "standin/model-b/-/-")]["per_trial"], "2.800000 usd")
        self.assertEqual(costs[("list-12", "standin/model-a/-/-")]["per_trial"], "6.000000 usd")
        self.assertEqual(costs[("list-12", "standin/model-b/-/-")]["per_trial"], "10.600000 usd")
        cheaper = {f["schedule"]: f["route"] for _, kind, _, f in map(parse, out) if kind == "cheaper"}
        self.assertEqual(cheaper, {"list-80": "standin/model-b/-/-", "list-12": "standin/model-a/-/-"})
        flips = [f for _, kind, _, f in map(parse, out) if kind == "flips"]
        self.assertEqual(flips, [{"list-80": "standin/model-b/-/-", "list-12": "standin/model-a/-/-"}])

    def test_no_flips_line_when_one_route_is_cheaper_under_every_schedule(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1_000_000), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(output=100_000), record_id="rb")
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        self.assertEqual([line for line in out if parse(line)[1] == "flips"], [])

    def test_a_null_cache_read_makes_the_cost_unknown_never_zero(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1_000_000, cache_read=None), record_id="ra")
        b = route_record("model-b", OUTCOMES, tokens(output=100_000, cache_read=20_000_000), record_id="rb")
        b["trials"][0]["usage"]["cache_read"] = None
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        for _, kind, _, f in map(parse, out):
            if kind == "cost":
                self.assertEqual(f["per_trial"], "unknown", f)
                self.assertEqual(f["per_solved"], "unknown", f)
            if kind == "cheaper":
                self.assertEqual(f["route"], "unknown", f)
        self.assertNotIn("0.000000 usd", "\n".join(out))

    def test_a_billed_record_cost_prints_as_billed_beside_the_modelled_cost(self):
        a = route_record("model-a", OUTCOMES, tokens(output=1_000_000, cost=3.0, basis="billed"), record_id="ra")
        code, out, err = run([a])
        self.assertEqual(code, 0, err)
        costs = [f for _, kind, _, f in map(parse, out) if kind == "cost"]
        self.assertEqual(len(costs), 2)
        for f in costs:
            self.assertEqual(f["recorded_basis"], "billed")
            # The record's own 4 x 3.00 for 3 passing trials, never merged into the modelled figure.
            self.assertEqual(f["recorded_per_solved"], "4.000000 usd")
            self.assertNotEqual(f["per_solved"], f["recorded_per_solved"])
        self.assertEqual({f["per_solved"] for f in costs}, {"10.666667 usd", "8.000000 usd"})

    def test_a_pilot_label_and_the_modelled_mark_are_on_every_line(self):
        outcomes = {"t-1": ["pass"] * 5, "t-2": ["fail"] * 5}
        a = pilot(route_record("model-a", outcomes, tokens(output=1_000_000), record_id="ra", k=5))
        b = pilot(route_record("model-b", outcomes, tokens(output=100_000, cache_read=20_000_000),
                               record_id="rb", k=5))
        code, out, err = run([a, b])
        self.assertEqual(code, 0, err)
        self.assertTrue(out)
        self.assertIn("flips", [parse(line)[1] for line in out])
        for line in out:
            label, _, mark, _ = parse(line)
            self.assertEqual(label, "pilot", line)
            self.assertEqual(mark, MARK, line)

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


if __name__ == "__main__":
    unittest.main()
