"""The route table (eval/routes.py): several stand-in records, one per route, side by side."""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from eval import passk, routes
from eval import record as record_mod
from eval.tests import build_record, trial


def usage(cost):
    """Usage with a known cost (a number) or an unknown one (None, never 0)."""
    out = record_mod.empty_usage()
    if cost is not None:
        out.update(cost=cost, cost_unit="usd", cost_basis="reported_not_billed", source="engine_output")
    return out


def standin_record(model, outcomes, *, record_id, cost=None, label="dev", k=2):
    """A stand-in record of one route (the arm's --model): outcomes {task_id: ['pass'|'fail', ...]}."""
    rows = []
    for task_id, results in outcomes.items():
        for index, outcome in enumerate(results, start=1):
            rows.append(trial(task_id, index, outcome, usage=usage(cost), record_id=record_id))
    attempts = len(next(iter(outcomes.values())))
    record = build_record(rows, k=k, attempts=attempts, label=label, record_id=record_id)
    record["arm"]["model"] = model
    return record


def pilot(record):
    """The same record as a pilot would carry it: held-out frozen suite, a real engine, k of at least 5."""
    record["label"] = "pilot"
    record["suite"].update(split="held_out", frozen=True)
    record["arm"]["engine"] = "claude_code"
    return record_mod.require_valid(record)


def table(records):
    """Write the records into a temp folder, run the command on them, return (exit, stdout lines, stderr)."""
    with tempfile.TemporaryDirectory(prefix="eval-routes-") as folder:
        paths = []
        for record in records:
            path = Path(folder) / f"{record['record_id']}.json"
            path.write_text(record_mod.dumps(record), encoding="utf-8")
            paths.append(str(path))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = routes.main(paths)
    return code, out.getvalue().splitlines(), err.getvalue()


def fields(line):
    parts = line.split("\t")
    return parts[0], parts[1], dict(p.split("=", 1) for p in parts[2:])


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
        self.assertEqual({k[0] for k in keys}, {"standin/model-a/-/-", "standin/model-b/-/-"})
        summary = {f["route"]: f for _, kind, f in map(fields, out) if kind == "route"}
        self.assertEqual(len(summary), 2)
        # pass^2 from eval/passk.py on the planned counts: a = mean(C(2,2)/C(3,2), 0) = 1/6;
        # b = mean(1, C(2,2)/C(3,2)) = 2/3.
        expected_a = (passk.pass_hat_k(passk.Counts(3, 2), 2) + passk.pass_hat_k(passk.Counts(3, 0), 2)) / 2
        self.assertEqual(expected_a, Fraction(1, 6))
        self.assertEqual(summary["standin/model-a/-/-"]["pass^k"], f"{float(expected_a):.4f}")
        self.assertEqual(summary["standin/model-b/-/-"]["pass^k"], f"{2 / 3:.4f}")
        # Cost per solved: a spent 6 x 0.5 for 2 resolved trials; b 6 x 0.25 for 5.
        self.assertEqual(summary["standin/model-a/-/-"]["cost_per_solved"], "1.5000 usd")
        self.assertEqual(summary["standin/model-b/-/-"]["cost_per_solved"], "0.3000 usd")

    def test_an_unknown_cost_prints_unknown_never_zero(self):
        record = standin_record("model-a", {"t-1": ["pass", "fail"], "t-2": ["pass", "pass"]}, record_id="ru")
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        for _, kind, f in map(fields, out):
            key = "cost" if kind == "row" else "cost_per_solved"
            self.assertEqual(f[key], "unknown")

    def test_one_unknown_cost_makes_the_route_cost_unknown(self):
        record = standin_record("model-a", {"t-1": ["pass", "pass"], "t-2": ["pass", "fail"]}, record_id="rm",
                                cost=1.0)
        record["trials"][0]["usage"] = usage(None)
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        summary = [f for _, kind, f in map(fields, out) if kind == "route"]
        self.assertEqual(summary[0]["cost_per_solved"], "unknown")

    def test_a_pilot_record_prints_pilot_on_every_line(self):
        record = pilot(standin_record("model-p", {"t-1": ["pass"] * 5, "t-2": ["fail"] * 5}, record_id="rp",
                                      k=5))
        code, out, err = table([record])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out), 2 * 5 + 1)
        for line in out:
            self.assertEqual(line.split("\t")[0], "pilot", line)

    def test_each_record_keeps_its_own_label(self):
        dev = standin_record("model-a", {"t-1": ["pass"] * 5}, record_id="rd", k=5)
        pil = pilot(standin_record("model-p", {"t-1": ["pass"] * 5}, record_id="rp", k=5))
        code, out, err = table([dev, pil])
        self.assertEqual(code, 0, err)
        for label, _, f in map(fields, out):
            self.assertEqual(label, "pilot" if f["route"].endswith("model-p/-/-") else "dev")

    def test_two_records_of_one_route_are_refused(self):
        a = standin_record("model-a", {"t-1": ["pass", "pass"]}, record_id="r1")
        b = standin_record("model-a", {"t-1": ["fail", "fail"]}, record_id="r2")
        code, out, err = table([a, b])
        self.assertEqual(code, 2)
        self.assertEqual(out, [])
        self.assertIn("one record per route", err)


if __name__ == "__main__":
    unittest.main()
