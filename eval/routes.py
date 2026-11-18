"""A route table: several ``recursion-eval-run/1`` records, one per route, side by side.

    python -m eval.routes RECORD [RECORD ...]

PROTOCOL.md compares two arms on the SAME route and model; which route to use is a different
question, so this table is not part of the frozen protocol and makes no claim. It reads one record
per route (a route is the arm's engine, model, route and effort) and prints:

* one ``row`` line per task, route and repeat (every trial in the record, planned and replacement,
  with its attempt index as the repeat), with the trial's verdict, error tier and cost;
* one ``route`` line per record with its headline pass^k (the plan is the denominator; computed by
  eval/passk.py on the counts eval/analyze.py takes, both imported unchanged) beside its cost per
  solved task: the known cost of the trials that ran, divided by the resolved trials among them.

An unknown cost prints ``unknown``, never 0: a trial with no cost is ``cost=unknown``, and a route
with any unknown cost, or with costs in more than one unit, has ``cost_per_solved=unknown``. A
route that solved nothing has ``cost_per_solved=none-solved``. Every line starts with the record's
label (dev, shakedown, screen, pilot, full), so a pilot stays labelled a pilot on every line.

Records are checked as eval/analyze.py checks them (a valid record, made under the protocol and
analysis this code implements); two records of one route are refused. Nothing is written and no
engine is started.
"""
from __future__ import annotations

import argparse
import sys
from fractions import Fraction

from eval import analyze, passk
from eval import record as record_mod

UNKNOWN = "unknown"


def route_of(record: dict) -> str:
    """The route a record ran on: engine/model/route/effort, '-' where the record has none."""
    arm = record["arm"]
    return "/".join(arm[key] if arm[key] is not None else "-" for key in ("engine", "model", "route", "effort"))


def _number(value) -> str:
    if isinstance(value, Fraction):
        value = float(value)
    return f"{value:.4f}"


def _cost(trial: dict) -> str:
    usage = trial["usage"]
    if usage["cost"] is None:
        return UNKNOWN
    return f"{_number(usage['cost'])}{'' if usage['cost_unit'] is None else ' ' + usage['cost_unit']}"


def pass_hat_k(record: dict) -> passk.SuiteValue:
    """The headline pass^k of one record: planned trials, the plan's attempts as n (eval/analyze.py)."""
    k = record["plan"]["k"]
    return passk.suite_metric(analyze.headline_counts(record), lambda c: passk.pass_hat_k(c, k),
                              allow_not_estimable=False)


def cost_per_solved(record: dict) -> str:
    usage = analyze.usage_totals(record)
    cost = usage["cost"]
    if cost["unknown"] or cost["sum_of_known"] is None or len(usage["cost_units"]) > 1:
        return UNKNOWN
    solved = sum(1 for t in record["trials"]
                 if t["error"]["class"] != "not_run" and t["grade"]["verdict"] == "resolved")
    if not solved:
        return "none-solved"
    unit = usage["cost_units"][0] if usage["cost_units"] else None
    return f"{_number(cost['sum_of_known'] / solved)}{'' if unit is None else ' ' + unit}"


def check(records: list) -> None:
    """Refuse an invalid record, one of another protocol or analysis, or two records of one route."""
    if not records:
        raise ValueError("give at least one record")
    seen: dict = {}
    for record in records:
        record_mod.require_valid(record)
        analyze.require_supported(record)
        route = route_of(record)
        if route in seen:
            raise ValueError(f"records {seen[route]} and {record['record_id']} are both route {route}: "
                             "give one record per route")
        seen[route] = record["record_id"]


def lines(records: list) -> list:
    """The table's lines: every row line, then one route line per record, in the order given."""
    check(records)
    out = []
    for record in records:
        label, route = record["label"], route_of(record)
        for trial in sorted(record["trials"], key=lambda t: (t["task_id"], t["attempt"]["index"])):
            attempt = trial["attempt"]
            out.append(f"{label}\trow\troute={route}\ttask={trial['task_id']}\trepeat={attempt['index']}\t"
                       f"planned={'yes' if attempt['planned'] else 'no'}\tverdict={trial['grade']['verdict']}\t"
                       f"tier={trial['error']['tier']}\tcost={_cost(trial)}")
    for record in records:
        label, route = record["label"], route_of(record)
        value = pass_hat_k(record)
        shown = "not-estimable" if value.value is None else _number(value.value)
        out.append(f"{label}\troute\troute={route}\trecord={record['record_id']}\tk={record['plan']['k']}\t"
                   f"tasks={len(record['plan']['tasks'])}\tpass^k={shown}\t"
                   f"cost_per_solved={cost_per_solved(record)}")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.routes", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("records", nargs="+", help="one recursion-eval-run/1 record per route")
    args = parser.parse_args(argv)
    try:
        table = lines([record_mod.load(path) for path in args.records])
    except (OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for line in table:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
