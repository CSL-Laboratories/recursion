"""A route table: several ``recursion-eval-run/1`` records, one per route, side by side.

    python -m eval.routes RECORD [RECORD ...]

PROTOCOL.md compares two arms on the SAME route and model; which route to use is a different
question, so this table is not part of the frozen protocol and makes no claim. It reads one record
per route and prints tab-separated lines, each starting with the record's label (dev, shakedown,
screen, pilot, full), so a pilot stays labelled a pilot on every line:

* ``row``: one per task, route and repeat (every trial in the record, planned and replacement, with
  its attempt index as the repeat): planned or not, verdict, error tier, cost and cost basis;
* ``legend``: one per record, saying what each figure of its route line counts;
* ``route``: one per record:
  - ``pass^k_planned``: the headline pass^k, over PLANNED trials only (the plan is the denominator;
    eval/passk.py on the counts eval/analyze.py takes, both imported unchanged); replacements do
    not count and a planned trial that never ran counts as failed;
  - the cost figures, over EVERY trial that ran (replacements included, ``not_run`` left out):
    ``known_cost``; ``cost_per_passing_trial`` = known cost / passing trials (the meaning of
    eval/analyze.py's cost per resolved trial); ``cost_per_solved_task`` = known cost / the number of
    distinct tasks with at least one passing trial.

A route is its engine, model, route and effort, printed as a JSON list (a model name holding "/" stays
one name), plus the hash of the arm's shared settings (``settings``, its first 12 hex), so two records
that differ only in permission mode or engine arguments are two routes. Two records of one route,
an invalid record, a record file nested too deeply to read, and a record of another protocol or analysis
version are refused (exit 2). The table's fields are plain tokens separated by TAB, never quoted: a
record whose record_id, task_id or cost_unit holds a TAB, a character str.isprintable() refuses (CR,
LF, U+2028 among them) or nothing is refused (exit 2) before any line prints, the refusal naming the
field, never the value.

Costs are never added across what cannot be added: an unknown cost prints ``unknown``, never 0, and
makes every cost figure of its route ``unknown`` (``cost_unknown`` counts those trials); costs in more
than one unit print ``mixed-units:<units>``; billed and not-billed costs print
``mixed-basis:<bases>``. A known cost keeps at least four significant digits, so a small cost never
prints as 0.0000. A route that passed nothing prints ``none-passing`` and ``none-solved``. Nothing is
written and no engine is started.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from fractions import Fraction

from eval import analyze, passk
from eval import record as record_mod

UNKNOWN = "unknown"
SETTINGS_SHOWN = 12

LEGEND = (
    ("pass^k_planned", "headline pass^k over planned trials only; replacements not counted, a planned trial "
                       "that never ran counts as failed"),
    ("costs", "known_cost and both cost figures count every trial that ran, replacements included, not_run "
              "left out"),
    ("cost_per_passing_trial", "known cost / passing trials"),
    ("cost_per_solved_task", "known cost / distinct tasks with at least one passing trial"),
)


def route_key(record: dict) -> tuple:
    """What makes a route: engine, model, route, effort and the hash of the arm's shared settings."""
    arm = record["arm"]
    return (arm["engine"], arm["model"], arm["route"], arm["effort"], arm["settings_sha256"])


def route_name(record: dict) -> str:
    """The route as a JSON list (engine, model, route, effort): a "/" in a name stays inside it."""
    return json.dumps(list(route_key(record)[:4]))


def _number(value) -> str:
    """At least four decimals, and at least four significant digits for a small non-zero value."""
    if isinstance(value, Fraction):
        value = float(value)
    places = 4
    if value:
        places = max(places, 3 - math.floor(math.log10(abs(value))))
    return f"{value:.{places}f}"


def _money(value, unit) -> str:
    return _number(value) + ("" if unit is None else " " + unit)


def _cost(trial: dict) -> str:
    usage = trial["usage"]
    return UNKNOWN if usage["cost"] is None else _money(usage["cost"], usage["cost_unit"])


def pass_hat_k(record: dict) -> passk.SuiteValue:
    """The headline pass^k of one record: planned trials, the plan's attempts as n (eval/analyze.py)."""
    k = record["plan"]["k"]
    return passk.suite_metric(analyze.headline_counts(record), lambda c: passk.pass_hat_k(c, k),
                              allow_not_estimable=False)


def cost_figures(record: dict) -> dict:
    """The route line's counts and cost figures, over every trial that ran (replacements included)."""
    ran = [t for t in record["trials"] if t["error"]["class"] != "not_run"]
    passing = [t for t in ran if t["grade"]["verdict"] == "resolved"]
    known = [t["usage"] for t in ran if t["usage"]["cost"] is not None]
    units = sorted({u["cost_unit"] for u in known if u["cost_unit"] is not None})
    bases = sorted({u["cost_basis"] for u in known})
    out = {"trials_ran": str(len(ran)), "passing_trials": str(len(passing)),
           "solved_tasks": str(len({t["task_id"] for t in passing})),
           "cost_unknown": str(len(ran) - len(known)), "cost_basis": "+".join(bases) or "-"}
    if not ran or len(known) < len(ran):
        verdict = UNKNOWN
    elif len(units) > 1:
        verdict = "mixed-units:" + "+".join(units)
    elif len(bases) > 1:
        verdict = "mixed-basis:" + "+".join(bases)
    else:
        verdict = None
    if verdict is not None:
        out.update(known_cost=verdict, cost_per_passing_trial=verdict, cost_per_solved_task=verdict)
        return out
    unit = units[0] if units else None
    total = sum(u["cost"] for u in known)
    solved = int(out["solved_tasks"])
    out["known_cost"] = _money(total, unit)
    out["cost_per_passing_trial"] = _money(total / len(passing), unit) if passing else "none-passing"
    out["cost_per_solved_task"] = _money(total / solved, unit) if solved else "none-solved"
    return out


def _plain(value) -> bool:
    """A value the table may print raw: a non-empty string with no character str.isprintable() refuses. That
    refuses TAB, the table's field separator, and CR, LF, every C0 control, U+007F, U+0085, U+2028 and U+2029."""
    return isinstance(value, str) and value != "" and value.isprintable()


def require_plain(record: dict, where: str) -> None:
    """Refuse a record whose values printed raw (record_id, each task_id, each cost_unit) are not plain; the
    refusal names the field and never the value. Label, verdict, tier and basis are choices record.py checks,
    the settings hash a pattern, the route a JSON list (ASCII), the counts integers."""
    if not _plain(record["record_id"]):
        raise ValueError(f"{where}: record_id holds a tab, a character that does not print, or nothing")
    for i, trial in enumerate(record["trials"]):
        if not _plain(trial["task_id"]):
            raise ValueError(f"{where}: trials[{i}].task_id holds a tab, a character that does not print, or nothing")
        unit = trial["usage"]["cost_unit"]
        if unit is not None and not _plain(unit):
            raise ValueError(f"{where}: trials[{i}].usage.cost_unit holds a tab, a character that does not print, "
                             f"or nothing")


def check(records: list) -> None:
    """Refuse no record, an invalid record, a value the table cannot print as one plain field, one of another
    protocol or analysis, or two records of one route."""
    if not records:
        raise ValueError("give at least one record")
    seen: dict = {}
    for n, record in enumerate(records, start=1):
        record_mod.require_valid(record)
        require_plain(record, f"record {n}")
        analyze.require_supported(record)
        key = route_key(record)
        if key in seen:
            raise ValueError(f"records {seen[key]} and {record['record_id']} are both route {route_name(record)} "
                             f"with settings {key[4][:SETTINGS_SHOWN]}: give one record per route")
        seen[key] = record["record_id"]


def _route_fields(record: dict) -> str:
    return f"route={route_name(record)}\tsettings={record['arm']['settings_sha256'][:SETTINGS_SHOWN]}"


def lines(records: list) -> list:
    """The table's lines: every row line, then a legend and a route line per record, in the order given."""
    check(records)
    out = []
    for record in records:
        label, route = record["label"], _route_fields(record)
        for trial in sorted(record["trials"], key=lambda t: (t["task_id"], t["attempt"]["index"])):
            attempt = trial["attempt"]
            out.append(f"{label}\trow\t{route}\ttask={trial['task_id']}\trepeat={attempt['index']}\t"
                       f"planned={'yes' if attempt['planned'] else 'no'}\tverdict={trial['grade']['verdict']}\t"
                       f"tier={trial['error']['tier']}\tcost={_cost(trial)}\tbasis={trial['usage']['cost_basis']}")
    for record in records:
        label = record["label"]
        out.append(f"{label}\tlegend\t" + "\t".join(f"{key}={text}" for key, text in LEGEND))
        value = pass_hat_k(record)
        shown = "not-estimable" if value.value is None else _number(value.value)
        figures = cost_figures(record)
        out.append(f"{label}\troute\t{_route_fields(record)}\trecord={record['record_id']}\tk={record['plan']['k']}\t"
                   f"tasks={len(record['plan']['tasks'])}\tpass^k_planned={shown}\t"
                   + "\t".join(f"{key}={figures[key]}" for key in (
                       "trials_ran", "passing_trials", "solved_tasks", "cost_unknown", "cost_basis", "known_cost",
                       "cost_per_passing_trial", "cost_per_solved_task")))
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
    except RecursionError:
        print("refused: a record file nests too deeply to read", file=sys.stderr)
        return 2
    for line in table:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
