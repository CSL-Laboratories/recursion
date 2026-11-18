"""Route costs re-priced under named price schedules: modelled, not billed.

    python -m eval.price_schedules --schedules SCHEDULES.json RECORD [RECORD ...]

Which route is cheaper depends on the price list: a route that answers from the cache wins where
cached input is cheap against output, and loses where it is not. One tokens-saved or dollars-saved
figure hides that. This command reads several ``recursion-eval-run/1`` records, one per route (a
route is the arm's engine, model, route and effort), and a JSON file of named price schedules:

    {"schedules": [{"name": "list-a", "unit": "usd",
                    "per_million": {"uncached_input": 1, "cache_write": 1, "cache_read": 0.1, "output": 8}}]}

It re-costs the four token classes of every trial that ran under every schedule and prints
tab-separated lines; each starts with the record's label (dev, shakedown, screen, pilot, full; the
labels of all records on a line that spans records) and the words ``modelled, not billed``:

* one ``cost`` line per schedule and route: modelled cost per trial (mean over the trials that ran)
  and per solved task (total over the trials that ran / the passing ones among them), and beside
  it, never merged, the record's own cost per solved task and its ``cost_basis``;
* one ``cheaper`` line per schedule: the route with the lower modelled cost per solved task;
* one ``flips`` line when the cheaper route is not the same under every schedule.

A null token class makes that trial's modelled cost, and every figure built on it, ``unknown``,
never 0. A route that solved nothing is ``none-solved``. Nothing is spent, no engine is started and
nothing is written; the schedules are whatever the file says, not a vendor's bill.
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
NONE_SOLVED = "none-solved"
PER = 1_000_000


def route_of(record: dict) -> str:
    """The route a record ran on: engine/model/route/effort, '-' where the record has none."""
    arm = record["arm"]
    return "/".join(arm[key] if arm[key] is not None else "-" for key in ("engine", "model", "route", "effort"))


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


def _solved(trials: list) -> int:
    return sum(1 for t in trials if t["grade"]["verdict"] == "resolved")


def trial_cost(trial: dict, schedule: dict):
    """The modelled cost of one trial under one schedule, or None when a token class is unknown."""
    usage = trial["usage"]
    if any(usage[key] is None for key in record_mod.TOKEN_FIELDS):
        return None
    return sum(Fraction(usage[key]) * schedule["per_million"][key] for key in record_mod.TOKEN_FIELDS) / PER


def modelled(record: dict, schedule: dict) -> dict:
    """Modelled cost per trial and per solved task (Fractions), None where unknown."""
    trials = _ran(record)
    costs = [trial_cost(t, schedule) for t in trials]
    known = all(c is not None for c in costs)
    total = sum(costs) if known else None
    solved = _solved(trials)
    return {"trials": len(trials), "unknown": sum(1 for c in costs if c is None), "solved": solved,
            "per_trial": total / len(trials) if known and trials else None,
            "per_solved": total / solved if known and solved else None}


def _amount(value, unit) -> str:
    if isinstance(value, Fraction):
        value = float(value)
    return f"{value:.6f}{'' if unit is None else ' ' + unit}"


def _per_solved_text(value, solved: int, unit) -> str:
    if not solved:
        return NONE_SOLVED
    return UNKNOWN if value is None else _amount(value, unit)


def recorded(record: dict) -> tuple:
    """The record's own cost per solved task and its cost bases, as it reported them (never re-priced)."""
    trials = _ran(record)
    solved = _solved(trials)
    bases = ",".join(sorted({t["usage"]["cost_basis"] for t in trials})) or UNKNOWN
    units = sorted({t["usage"]["cost_unit"] for t in trials if t["usage"]["cost_unit"]})
    costs = [t["usage"]["cost"] for t in trials]
    if not solved:
        return NONE_SOLVED, bases
    if any(c is None for c in costs) or len(units) != 1:
        return UNKNOWN, bases
    return _amount(Fraction(str(sum(costs))) / solved, units[0]), bases


def check(records: list) -> None:
    """Refuse an invalid record or two records of one route."""
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


def cheaper(records: list, schedule: dict) -> str:
    """The route with the lowest modelled cost per solved task; unknown if any route's cost is unknown."""
    best, best_routes = None, []
    for record in records:
        figures = modelled(record, schedule)
        if figures["solved"] == 0:
            if figures["unknown"]:
                return UNKNOWN
            continue
        if figures["per_solved"] is None:
            return UNKNOWN
        if best is None or figures["per_solved"] < best:
            best, best_routes = figures["per_solved"], [route_of(record)]
        elif figures["per_solved"] == best:
            best_routes.append(route_of(record))
    if not best_routes:
        return NONE_SOLVED
    return best_routes[0] if len(best_routes) == 1 else "tie:" + ",".join(best_routes)


def lines(records: list, schedules: list) -> list:
    check(records)
    labels = "+".join(dict.fromkeys(r["label"] for r in records))
    out = []
    for schedule in schedules:
        unit = schedule["unit"]
        for record in records:
            figures = modelled(record, schedule)
            per_trial = UNKNOWN if figures["per_trial"] is None else _amount(figures["per_trial"], unit)
            if not figures["trials"]:
                per_trial = "none-ran"
            rec_per_solved, rec_basis = recorded(record)
            out.append(f"{record['label']}\tcost\t{MARK}\tschedule={schedule['name']}\troute={route_of(record)}\t"
                       f"record={record['record_id']}\ttrials={figures['trials']}\tunknown={figures['unknown']}\t"
                       f"solved={figures['solved']}\tper_trial={per_trial}\t"
                       f"per_solved={_per_solved_text(figures['per_solved'], figures['solved'], unit)}\t"
                       f"recorded_per_solved={rec_per_solved}\trecorded_basis={rec_basis}")
    winners = {}
    for schedule in schedules:
        winners[schedule["name"]] = cheaper(records, schedule)
        out.append(f"{labels}\tcheaper\t{MARK}\tschedule={schedule['name']}\troute={winners[schedule['name']]}")
    decided = {w for w in winners.values() if w not in (UNKNOWN, NONE_SOLVED) and not w.startswith("tie:")}
    if len(decided) > 1:
        out.append(f"{labels}\tflips\t{MARK}\t" + "\t".join(f"{name}={w}" for name, w in winners.items()))
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
