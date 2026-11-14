"""The frozen analysis: every score recomputed from the trials of ``recursion-eval-run/1`` records.

    python -m eval.analyze RECORD                 one suite run
    python -m eval.analyze RECORD_A RECORD_B      two runs of one pinned suite, paired by task (B - A)

This file and eval/passk.py are hashed in eval/FROZEN.sha256 with PROTOCOL.md; a record carries the
hash of the analysis it was made under. The rules below are PROTOCOL.md's, sections "Scoring" and
"Claims".

Scoring.
* Headline: the plan is the denominator. A task's n is the plan's attempts per task; c counts its
  PLANNED trials that resolved. A trial that failed for any reason, infra, grader, unknown or not run
  included, is a failure here. Replacement trials do not enter the headline.
* Valid runs only: a task's trials, planned and replacement, whose tier is ``none`` or ``agent``. A
  task with fewer than k of them is not estimable for pass^k and is named, not scored 0.
* Bounds: the planned trials the valid score leaves out, scored all failed (the headline) and all
  passed (the upper bound).
* pass@1 is c / n per task averaged over tasks; the first-attempt rate is trial 1 alone.
* Intervals resample tasks (eval/passk.py). A clustered interval and leave-one-cluster-out appear
  when the tasks come from two or more clusters.
* Tokens and cost: sums over the trials that report a value, with the count of trials that do not.
  An unknown value is never added as 0.
"""
from __future__ import annotations

import argparse
import json
import math
import sys

from eval import passk
from eval import record as record_mod

VALID_TIERS = ("none", "agent")
EXCLUDED_TIERS = ("infra", "grader", "unknown")


def _planned_rows(record: dict) -> dict:
    attempts = record["plan"]["attempts_per_task"]
    rows: dict = {t["id"]: {} for t in record["plan"]["tasks"]}
    for trial in record["trials"]:
        attempt = trial["attempt"]
        if attempt["planned"] and 1 <= attempt["index"] <= attempts:
            rows[trial["task_id"]][attempt["index"]] = trial
    return rows


def headline_counts(record: dict) -> dict:
    attempts = record["plan"]["attempts_per_task"]
    out = {}
    for task_id, rows in _planned_rows(record).items():
        passed = sum(1 for t in rows.values() if t["grade"]["verdict"] == "resolved")
        out[task_id] = passk.Counts(attempts, passed)
    return out


def valid_counts(record: dict) -> dict:
    out = {t["id"]: [0, 0] for t in record["plan"]["tasks"]}
    for trial in record["trials"]:
        if trial["error"]["tier"] in VALID_TIERS:
            out[trial["task_id"]][0] += 1
            out[trial["task_id"]][1] += trial["grade"]["verdict"] == "resolved"
    return {task_id: passk.Counts(n, c) for task_id, (n, c) in out.items()}


def excluded(record: dict) -> dict:
    """Planned trials the valid score leaves out, by tier (a planned trial with no row is 'missing')."""
    attempts = record["plan"]["attempts_per_task"]
    by_tier = {tier: 0 for tier in EXCLUDED_TIERS}
    by_tier["missing"] = 0
    per_task = {}
    for task_id, rows in _planned_rows(record).items():
        count = 0
        for index in range(1, attempts + 1):
            trial = rows.get(index)
            if trial is None:
                by_tier["missing"] += 1
                count += 1
            elif trial["error"]["tier"] in EXCLUDED_TIERS:
                by_tier[trial["error"]["tier"]] += 1
                count += 1
        per_task[task_id] = count
    return {"by_tier": by_tier, "per_task": per_task}


def upper_counts(record: dict) -> dict:
    head = headline_counts(record)
    extra = excluded(record)["per_task"]
    return {t: passk.Counts(c.n, c.c + extra[t]) for t, c in head.items()}


def _f(value):
    return None if value is None else float(value)


def _intervals(per_task: dict, cluster_of: dict, *, resamples: int, seed: int) -> dict:
    out: dict = {"bootstrap": None, "clt": None, "clustered": None, "leave_one_cluster_out": None}
    if len(per_task) < 2:
        out["note"] = "fewer than 2 tasks: no interval"
        return out
    values = [per_task[t] for t in sorted(per_task)]
    out["bootstrap"] = passk.bootstrap_interval(values, resamples=resamples, seed=seed)
    clt = passk.clt_interval(values)
    out["clt"] = {"raw": clt["raw"], "clipped": clt["clipped"], "se": clt["se"], "se_is_zero": clt["se_is_zero"]}
    clusters = {cluster_of.get(t) for t in per_task}
    if len(clusters) >= 2 and None not in clusters:
        out["clustered"] = passk.clustered_bootstrap_interval(per_task, cluster_of, resamples=resamples, seed=seed)
        out["leave_one_cluster_out"] = passk.leave_one_cluster_out(per_task, cluster_of)
    return out


def percentile(values: list, p: float):
    """Nearest rank: the smallest value with at least p percent of the values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _ran(record: dict) -> list:
    return [t for t in record["trials"] if t["error"]["class"] != "not_run"]


def usage_totals(record: dict) -> dict:
    trials = _ran(record)
    out = {}
    for key in (*record_mod.TOKEN_FIELDS, "cost"):
        known = [t["usage"][key] for t in trials if t["usage"][key] is not None]
        out[key] = {"sum_of_known": sum(known) if known else None, "known": len(known),
                    "unknown": len(trials) - len(known)}
    out["cost_units"] = sorted({t["usage"]["cost_unit"] for t in trials if t["usage"]["cost_unit"]})
    out["cost_bases"] = sorted({t["usage"]["cost_basis"] for t in trials})
    resolved = sum(1 for t in trials if t["grade"]["verdict"] == "resolved")
    cost = out["cost"]
    out["cost_per_resolved"] = (cost["sum_of_known"] / resolved
                                if resolved and cost["unknown"] == 0 and cost["sum_of_known"] is not None else None)
    return out


def timing(record: dict) -> dict:
    trials = _ran(record)
    walls = [t["timing"]["wall_seconds"] for t in trials if t["timing"]["wall_seconds"] is not None]
    solved = [t["timing"]["wall_seconds"] for t in trials
              if t["timing"]["wall_seconds"] is not None and t["grade"]["verdict"] == "resolved"]
    return {"all": {"p50": percentile(walls, 50), "p95": percentile(walls, 95), "trials": len(walls)},
            "resolved": {"p50": percentile(solved, 50), "p95": percentile(solved, 95), "trials": len(solved)}}


def false_done(record: dict) -> dict:
    """Valid trials where the engine claimed to be done and the hidden tests did not pass."""
    claimed = [t for t in record["trials"] if t["error"]["tier"] in VALID_TIERS and t["engine"]["claimed_done"] is True]
    false = [t for t in claimed if t["grade"]["verdict"] != "resolved"]
    unknown = [t for t in record["trials"] if t["error"]["tier"] in VALID_TIERS and t["engine"]["claimed_done"] is None]
    return {"claimed_done": len(claimed), "false_done": len(false),
            "rate": len(false) / len(claimed) if claimed else None, "claim_unknown": len(unknown)}


def first_attempt(record: dict) -> float | None:
    rows = _planned_rows(record)
    if not rows:
        return None
    return sum(1 for r in rows.values() if r.get(1, {}).get("grade", {}).get("verdict") == "resolved") / len(rows)


def summarize(record: dict, *, resamples: int = passk.DEFAULT_RESAMPLES, seed: int = passk.DEFAULT_SEED) -> dict:
    record_mod.require_valid(record)
    k = record["plan"]["k"]
    cluster_of = {t["id"]: t["cluster"] for t in record["plan"]["tasks"]}
    head = headline_counts(record)
    valid = valid_counts(record)
    upper = upper_counts(record)

    def metric(counts, fn, allow):
        value = passk.suite_metric(counts, fn, allow_not_estimable=allow)
        return value

    head_k = metric(head, lambda c: passk.pass_hat_k(c, k), False)
    head_1 = metric(head, passk.pass_at_1, False)
    valid_k = metric(valid, lambda c: passk.pass_hat_k(c, k), True)
    valid_1 = metric(valid, passk.pass_at_1, True)
    upper_k = metric(upper, lambda c: passk.pass_hat_k(c, k), False)
    return {
        "record_id": record["record_id"],
        "label": record["label"],
        "suite": record["suite"],
        "arm": record["arm"],
        "k": k,
        "attempts_per_task": record["plan"]["attempts_per_task"],
        "tasks": len(record["plan"]["tasks"]),
        "headline": {
            "pass_hat_k": _f(head_k.value),
            "pass_at_1": _f(head_1.value),
            "first_attempt": first_attempt(record),
            "not_estimable": list(head_k.not_estimable),
            "per_task_pass_hat_k": {t: float(v) for t, v in head_k.per_task.items()},
            "intervals_pass_hat_k": _intervals(head_k.per_task, cluster_of, resamples=resamples, seed=seed),
            "intervals_pass_at_1": _intervals(head_1.per_task, cluster_of, resamples=resamples, seed=seed),
        },
        "valid_runs_only": {
            "pass_hat_k": _f(valid_k.value),
            "pass_at_1": _f(valid_1.value),
            "tasks_counted": valid_k.tasks_counted,
            "not_estimable": list(valid_k.not_estimable),
            "excluded": excluded(record)["by_tier"],
        },
        "bounds_pass_hat_k": {"excluded_as_failed": _f(head_k.value), "excluded_as_passed": _f(upper_k.value)},
        "false_done": false_done(record),
        "usage": usage_totals(record),
        "wall_seconds": timing(record),
        "counts": {t: {"headline": [c.n, c.c], "valid": [valid[t].n, valid[t].c]} for t, c in head.items()},
    }


def claim(label: str, interval: tuple, metric: str = "pass^k") -> str:
    """The only wording a difference may be reported in (PROTOCOL.md, "Claims")."""
    lo, hi = interval
    span = f"95% task-level interval {lo * 100:+.1f} to {hi * 100:+.1f} points"
    tag = {"dev": "DEV RUN (stand-in or development tasks; not a measurement)",
           "screen": "SCREEN (labelled; no superiority wording)",
           "pilot": "PILOT (labelled pilot; not the full comparison)",
           "full": "FULL COMPARISON"}[label]
    if lo <= 0 <= hi:
        worst = -lo * 100
        text = (f"no detectable difference in {metric} ({span}); this is not equivalence: "
                f"it rules out only a loss larger than about {worst:.0f} points")
    elif label != "full":
        text = f"an observed difference in {metric} ({span}); a {label} supports no superiority claim"
    else:
        direction = "higher" if lo > 0 else "lower"
        text = f"B is {direction} than A in {metric} ({span})"
    return f"{tag}: {text}"


def compare(a: dict, b: dict, *, resamples: int = passk.DEFAULT_RESAMPLES, seed: int = passk.DEFAULT_SEED) -> dict:
    """Two runs of one pinned suite, paired by task: d = b - a (B is usually the Recursion arm)."""
    record_mod.require_valid(a)
    record_mod.require_valid(b)
    if a["suite"]["manifest_sha256"] != b["suite"]["manifest_sha256"]:
        raise ValueError("the two records ran different suite manifests; a paired comparison needs one pinned suite")
    if a["plan"]["k"] != b["plan"]["k"]:
        raise ValueError(f"the two records use different k ({a['plan']['k']} and {b['plan']['k']})")
    if a["plan"]["attempts_per_task"] != b["plan"]["attempts_per_task"]:
        raise ValueError("the two records planned different attempts per task")
    k = a["plan"]["k"]
    label = a["label"] if a["label"] == b["label"] else min((a["label"], b["label"]), key=record_mod.LABELS.index)
    out = {"a": a["record_id"], "b": b["record_id"], "arm_a": a["arm"], "arm_b": b["arm"], "k": k, "label": label}
    for name, fn in (("pass_hat_k", lambda c: passk.pass_hat_k(c, k)), ("pass_at_1", passk.pass_at_1)):
        va = passk.suite_metric(headline_counts(a), fn, allow_not_estimable=False)
        vb = passk.suite_metric(headline_counts(b), fn, allow_not_estimable=False)
        if va.value is None or vb.value is None:
            out[name] = {"note": "not estimable in one of the runs"}
            continue
        result = passk.paired(va.per_task, vb.per_task, resamples=resamples, seed=seed)
        result["claim"] = claim(label, result["bootstrap"], "pass^%d" % k if name == "pass_hat_k" else "pass@1")
        out[name] = result
    if "sd_difference" in out.get("pass_hat_k", {}):
        sd = out["pass_hat_k"]["sd_difference"]
        out["full_run_size_estimate"] = {
            "tasks_for_10_points": passk.tasks_for_difference(sd) if sd > 0 else None,
            "note": ("an estimate from this run's paired variance (alpha 0.05 two-sided, power 0.8, no design effect); "
                     "not a power claim"),
        }
    return out


def _round(value, places=4):
    if isinstance(value, float):
        return round(value, places)
    if isinstance(value, dict):
        return {k: _round(v, places) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(v, places) for v in value]
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.analyze", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("records", nargs="+", help="one record, or two records of one pinned suite (A then B)")
    parser.add_argument("--resamples", type=int, default=passk.DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=passk.DEFAULT_SEED)
    args = parser.parse_args(argv)
    if len(args.records) > 2:
        parser.error("one or two records")
    loaded = [record_mod.load(path) for path in args.records]
    out = {"summaries": [summarize(r, resamples=args.resamples, seed=args.seed) for r in loaded]}
    if len(loaded) == 2:
        out["paired"] = compare(loaded[0], loaded[1], resamples=args.resamples, seed=args.seed)
    json.dump(_round(out), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
