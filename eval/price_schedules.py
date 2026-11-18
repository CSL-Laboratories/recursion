"""Route costs re-priced under named price schedules: modelled, not billed.

    python -m eval.price_schedules --schedules SCHEDULES.json RECORD [RECORD ...]

Which route is cheaper depends on the price list: a route that answers from the cache wins where
cached input is cheap against output, and loses where it is not. One tokens-saved or dollars-saved
figure hides that. This command reads several ``recursion-eval-run/1`` records, one per route, and a
JSON file of named price schedules:

    {"schedules": [{"name": "list-a", "unit": "usd",
                    "per_million": {"uncached_input": 1, "cache_write": 1, "cache_read": 0.1, "output": 8}}]}

A route is the arm and its settings: ``<arm>:<engine>/<model>/<route>/<effort>`` (``-`` where the
record has none), so the direct arm and the recursion arm of one comparison are two routes. When the
records come from more than one arm, a ``note`` line says so: the table compares them as routes, not
as the protocol's paired arm comparison (eval/analyze.py does that). Records must share the suite
(id, version, manifest) and the task set; otherwise nothing is compared and the refusal names the
difference.

Every line is tab-separated and starts with the record's label (dev, shakedown, screen, pilot, full;
the labels of all records, joined with ``+``, on a line that spans records), then the line's kind,
then what its figures are:

* ``cost`` (``modelled, not billed``), one per schedule and route: the four token classes of every
  trial that ran, priced by the schedule; ``per_trial_ran`` (mean over the trials that ran),
  ``per_passing_trial`` (total / passing trials) and ``per_solved_task`` (total / distinct tasks with
  at least one passing trial);
* ``recorded`` (``recorded, <basis>``), one per route: the record's own cost, as it reported it, never
  re-priced and never under the modelled mark; per passing trial and per solved task, or ``unknown``
  when a trial has no cost, the costs are in more than one unit, or billed and not-billed costs would
  be added together;
* ``cheaper`` (``modelled, not billed``), one per schedule, by modelled cost per solved task:
  ``result=CHEAPER`` with the route, ``result=TIE`` with the routes, or ``result=UNKNOWN`` with the
  reason (an unknown cost, no route solved a task, one route only). A route that solved no task is
  never the cheaper one;
* ``flips`` (``modelled, not billed``), once when two or more routes are priced under two or more
  schedules: ``flips=yes`` when the cheapest routes are not the same under every schedule whose
  answer is known, ``flips=unknown`` when they agree but some answer is unknown, else ``flips=no``,
  with the count of CHEAPER, TIE and UNKNOWN answers and each schedule's answer.

A null token class makes that trial's modelled cost, and every figure built on it, ``unknown``,
never 0. Nothing is spent, no engine is started and nothing is written; the schedules are whatever
the file says, not a vendor's bill.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from fractions import Fraction

from eval import record as record_mod

MARK = "modelled, not billed"
UNKNOWN = "unknown"
PER = 1_000_000
PASSING = "resolved"   # the schema's verdict value for a passing trial (eval/record.py VERDICTS)


def route_of(record: dict) -> str:
    """The route of a record: its arm, then engine/model/route/effort, '-' where the record has none."""
    arm = record["arm"]
    settings = "/".join(arm[key] if arm[key] is not None else "-" for key in ("engine", "model", "route", "effort"))
    return f"{arm['name']}:{settings}"


def _price(value, where: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: a price must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where}: a price must be finite")
    if value < 0:
        raise ValueError(f"{where}: a price must not be negative")
    return Fraction(str(value)) if isinstance(value, float) else Fraction(value)


def _name(value, where: str) -> str:
    if not isinstance(value, str) or not value or any(ch in value for ch in "\t\r\n="):
        raise ValueError(f"{where}: must be a non-empty text without tabs, line breaks or '='")
    return value


def parse_schedules(data) -> list:
    """The named schedules of a schedules file, each {'name', 'unit', 'per_million': {class: Fraction}}."""
    if not isinstance(data, dict) or set(data) != {"schedules"}:
        raise ValueError("a schedules file is an object with the one key 'schedules'")
    items = data["schedules"]
    if not isinstance(items, list) or not items:
        raise ValueError("'schedules' must be a non-empty list")
    out, names = [], set()
    for i, item in enumerate(items):
        where = f"schedules[{i}]"
        if not isinstance(item, dict) or set(item) != {"name", "unit", "per_million"}:
            raise ValueError(f"{where}: needs exactly the keys name, unit, per_million")
        name = _name(item["name"], f"{where}.name")
        unit = _name(item["unit"], f"{where}.unit")
        if name in names:
            raise ValueError(f"{where}: the name {name!r} is given twice")
        names.add(name)
        prices = item["per_million"]
        if not isinstance(prices, dict):
            raise ValueError(f"{where}.per_million: must be an object")
        missing = [key for key in record_mod.TOKEN_FIELDS if key not in prices]
        extra = sorted(set(prices) - set(record_mod.TOKEN_FIELDS))
        if missing or extra:
            raise ValueError(f"{where}.per_million: needs exactly {', '.join(record_mod.TOKEN_FIELDS)}"
                             f" (missing: {', '.join(missing) or 'none'}; unknown: {', '.join(extra) or 'none'})")
        out.append({"name": name, "unit": unit,
                    "per_million": {key: _price(prices[key], f"{where}.per_million.{key}")
                                    for key in record_mod.TOKEN_FIELDS}})
    return out


def load_schedules(path) -> list:
    with open(path, encoding="utf-8") as handle:
        return parse_schedules(record_mod.loads(handle.read()))


def _ran(record: dict) -> list:
    return [t for t in record["trials"] if t["error"]["class"] != "not_run"]


def _passing(trials: list) -> list:
    return [t for t in trials if t["grade"]["verdict"] == PASSING]


def _counts(trials: list) -> tuple:
    """(passing trials, distinct tasks with at least one passing trial)."""
    passing = _passing(trials)
    return len(passing), len({t["task_id"] for t in passing})


def trial_cost(trial: dict, schedule: dict):
    """The modelled cost of one trial under one schedule, or None when a token class is unknown."""
    usage = trial["usage"]
    if any(usage[key] is None for key in record_mod.TOKEN_FIELDS):
        return None
    return sum(Fraction(usage[key]) * schedule["per_million"][key] for key in record_mod.TOKEN_FIELDS) / PER


def _per(total, count: int):
    return None if total is None or not count else total / count


def modelled(record: dict, schedule: dict) -> dict:
    """Modelled cost per trial that ran, per passing trial and per solved task (Fractions), None where unknown."""
    trials = _ran(record)
    costs = [trial_cost(t, schedule) for t in trials]
    total = sum(costs) if all(c is not None for c in costs) else None
    passing, solved = _counts(trials)
    return {"trials": len(trials), "unknown": sum(1 for c in costs if c is None), "passing": passing,
            "solved": solved, "per_trial": _per(total, len(trials)), "per_passing": _per(total, passing),
            "per_solved": _per(total, solved)}


def _amount(value, unit) -> str:
    if isinstance(value, Fraction):
        value = float(value)
    return f"{value:.6f}{'' if unit is None else ' ' + unit}"


def _figure(value, count: int, unit, none_word: str) -> str:
    if not count:
        return none_word
    return UNKNOWN if value is None else _amount(value, unit)


def recorded(record: dict) -> dict:
    """The record's own cost per passing trial and per solved task, as it reported it (never re-priced).
    Unknown when a trial has no cost, the costs are in more than one unit, or more than one basis would be
    added together."""
    trials = _ran(record)
    passing, solved = _counts(trials)
    bases = sorted({t["usage"]["cost_basis"] for t in trials})
    units = sorted({t["usage"]["cost_unit"] for t in trials if t["usage"]["cost_unit"]})
    costs = [t["usage"]["cost"] for t in trials]
    known = trials and all(c is not None for c in costs) and len(units) == 1 and len(bases) == 1
    total = Fraction(str(math.fsum(costs))) if known else None
    unit = units[0] if known else None
    return {"basis": ",".join(bases) or UNKNOWN,
            "per_passing": _figure(_per(total, passing), passing, unit, "none-passed"),
            "per_solved": _figure(_per(total, solved), solved, unit, "none-solved")}


def check(records: list) -> None:
    """Refuse an invalid record, two records of one route, or records of different suites or task sets."""
    if not records:
        raise ValueError("give at least one record")
    seen: dict = {}
    for record in records:
        record_mod.require_valid(record)
        route = route_of(record)
        if route in seen:
            raise ValueError(f"records {seen[route]} and {record['record_id']} are both route {route}: "
                             "give one record per route")
        seen[route] = record["record_id"]
    first = records[0]
    for record in records[1:]:
        for key in ("id", "version", "manifest_sha256"):
            if record["suite"][key] != first["suite"][key]:
                raise ValueError(f"records {first['record_id']} and {record['record_id']} ran different suites "
                                 f"(suite {key} {first['suite'][key]!r} against {record['suite'][key]!r}): "
                                 "no route is called cheaper across suites")
        tasks_a = {t["id"]: t["task_sha256"] for t in first["plan"]["tasks"]}
        tasks_b = {t["id"]: t["task_sha256"] for t in record["plan"]["tasks"]}
        if tasks_a != tasks_b:
            only_a = sorted(set(tasks_a) - set(tasks_b))
            only_b = sorted(set(tasks_b) - set(tasks_a))
            changed = sorted(t for t in set(tasks_a) & set(tasks_b) if tasks_a[t] != tasks_b[t])
            raise ValueError(f"records {first['record_id']} and {record['record_id']} ran different task sets "
                             f"(only in {first['record_id']}: {', '.join(only_a) or 'none'}; only in "
                             f"{record['record_id']}: {', '.join(only_b) or 'none'}; task content differs: "
                             f"{', '.join(changed) or 'none'}): no route is called cheaper across task sets")


def cheaper(records: list, schedule: dict) -> dict:
    """The cheaper route by modelled cost per solved task: {'result': CHEAPER|TIE|UNKNOWN, ...}."""
    if len(records) < 2:
        return {"result": "UNKNOWN", "reason": "one route only, nothing to compare"}
    best, best_routes, unknown_routes = None, [], []
    for record in records:
        figures = modelled(record, schedule)
        if not figures["solved"]:
            continue   # never the cheaper one per solved task, whatever its cost
        if figures["per_solved"] is None:
            unknown_routes.append(route_of(record))
        elif best is None or figures["per_solved"] < best:
            best, best_routes = figures["per_solved"], [route_of(record)]
        elif figures["per_solved"] == best:
            best_routes.append(route_of(record))
    if unknown_routes:
        return {"result": "UNKNOWN", "reason": "a cost is unknown for " + ",".join(unknown_routes)}
    if not best_routes:
        return {"result": "UNKNOWN", "reason": "no route solved a task"}
    if len(best_routes) == 1:
        return {"result": "CHEAPER", "route": best_routes[0]}
    return {"result": "TIE", "routes": ",".join(best_routes)}


def _answer(answer: dict) -> str:
    if answer["result"] == "CHEAPER":
        return answer["route"]
    if answer["result"] == "TIE":
        return "TIE(" + answer["routes"] + ")"
    return "UNKNOWN"


def lines(records: list, schedules: list) -> list:
    check(records)
    labels = "+".join(dict.fromkeys(r["label"] for r in records))
    out = []
    arms = list(dict.fromkeys(r["arm"]["name"] for r in records))
    if len(arms) > 1:
        out.append(f"{labels}\tnote\tnot a figure\tarms={','.join(arms)}\tmeaning=the records come from more "
                   "than one arm; they are compared as routes, not as the paired arm comparison of the protocol")
    for schedule in schedules:
        unit = schedule["unit"]
        for record in records:
            f = modelled(record, schedule)
            out.append(f"{record['label']}\tcost\t{MARK}\tschedule={schedule['name']}\troute={route_of(record)}\t"
                       f"record={record['record_id']}\ttrials={f['trials']}\tunknown={f['unknown']}\t"
                       f"passing_trials={f['passing']}\tsolved_tasks={f['solved']}\t"
                       f"per_trial_ran={_figure(f['per_trial'], f['trials'], unit, 'none-ran')}\t"
                       f"per_passing_trial={_figure(f['per_passing'], f['passing'], unit, 'none-passed')}\t"
                       f"per_solved_task={_figure(f['per_solved'], f['solved'], unit, 'none-solved')}")
    for record in records:
        r = recorded(record)
        out.append(f"{record['label']}\trecorded\trecorded, {r['basis']}\troute={route_of(record)}\t"
                   f"record={record['record_id']}\tbasis={r['basis']}\tper_passing_trial={r['per_passing']}\t"
                   f"per_solved_task={r['per_solved']}")
    answers = {}
    for schedule in schedules:
        answer = cheaper(records, schedule)
        answers[schedule["name"]] = answer
        fields = "\t".join(f"{key}={value}" for key, value in answer.items())
        out.append(f"{labels}\tcheaper\t{MARK}\tschedule={schedule['name']}\tby=per_solved_task\t{fields}")
    if len(records) > 1 and len(schedules) > 1:
        counts = {kind: sum(1 for a in answers.values() if a["result"] == kind)
                  for kind in ("CHEAPER", "TIE", "UNKNOWN")}
        known = {_answer(a) for a in answers.values() if a["result"] != "UNKNOWN"}
        flips = "yes" if len(known) > 1 else ("unknown" if counts["UNKNOWN"] else "no")
        out.append(f"{labels}\tflips\t{MARK}\tflips={flips}\tcheaper={counts['CHEAPER']}\tties={counts['TIE']}\t"
                   f"unknowns={counts['UNKNOWN']}\t"
                   + "\t".join(f"{name}={_answer(a)}" for name, a in answers.items()))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.price_schedules", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schedules", required=True, help="a JSON file of named price schedules")
    parser.add_argument("records", nargs="+", help="one recursion-eval-run/1 record per route")
    args = parser.parse_args(argv)
    try:
        schedules = load_schedules(args.schedules)
        table = lines([record_mod.load(path) for path in args.records], schedules)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for line in table:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
