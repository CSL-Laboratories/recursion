"""pass^k, pass@1 and their task-level intervals, from per-task trial counts.

Definitions (PROTOCOL.md, "Metrics"):

* A task's pass^k is the chance that k independent trials all pass. From n counted trials with c
  passes the unbiased estimate is C(c, k) / C(n, k), using THAT task's n. A suite's pass^k is the
  unweighted mean over its tasks. Dividing every task by one shared trial count is the error the
  contract warns about (PRODUCT 4671-4672); nothing here does it.
* n < k is "not estimable": the task's value is refused (NotEstimable), never scored 0 and never
  dropped without being named.
* pass@1 is c / n per task, averaged over tasks. pass@k is 1 - C(n - c, k) / C(n, k).
* Intervals resample TASKS, never trials: a task-level percentile bootstrap (B >= 1,000, default
  10,000, fixed seed) and a normal (CLT) interval over the per-task values. Resampling trials
  inside a task counts within-task variance about twice and, for k > 1, centres on the biased
  p-hat^k (vault 20261108-csl757, line 13), so it is not offered.
* A paired comparison aligns the two runs' task ids, takes d_i = b_i - a_i, and resamples the same
  task index for both arms. The difference lies in [-1, 1] and is never clipped.
* A clustered bootstrap resamples clusters (source repositories), then tasks inside each drawn
  cluster, keeping each task's estimate fixed.

Exact arithmetic: every per-task value is a Fraction; means are Fractions until the bootstrap.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from fractions import Fraction
from statistics import NormalDist

DEFAULT_RESAMPLES = 10_000
MIN_RESAMPLES = 1_000
DEFAULT_SEED = 7
DEFAULT_LEVEL = 0.95


class NotEstimable(ValueError):
    """A value that cannot be estimated from the trials given (for example n < k)."""


@dataclass(frozen=True)
class Counts:
    """One task's counted trials (n) and passes among them (c)."""

    n: int
    c: int

    def __post_init__(self) -> None:
        for name in ("n", "c"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
        if self.c > self.n:
            raise ValueError(f"c ({self.c}) exceeds n ({self.n})")


def _check_k(k: int) -> None:
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError(f"k must be a positive integer, got {k!r}")


def pass_hat_k(counts: Counts, k: int) -> Fraction:
    """C(c, k) / C(n, k) for one task. Raises NotEstimable when n < k."""
    _check_k(k)
    if counts.n < k:
        raise NotEstimable(f"n={counts.n} < k={k}: pass^{k} is not estimable for this task")
    return Fraction(math.comb(counts.c, k), math.comb(counts.n, k))


def pass_at_k(counts: Counts, k: int) -> Fraction:
    """1 - C(n - c, k) / C(n, k) for one task. Raises NotEstimable when n < k."""
    _check_k(k)
    if counts.n < k:
        raise NotEstimable(f"n={counts.n} < k={k}: pass@{k} is not estimable for this task")
    return 1 - Fraction(math.comb(counts.n - counts.c, k), math.comb(counts.n, k))


def pass_at_1(counts: Counts) -> Fraction:
    return pass_at_k(counts, 1)


@dataclass(frozen=True)
class SuiteValue:
    """A suite-level mean over the tasks it could be computed for, naming the ones it could not."""

    value: Fraction | None
    per_task: dict
    not_estimable: tuple

    @property
    def tasks_counted(self) -> int:
        return len(self.per_task)


def suite_metric(counts_by_task: dict, metric, *, allow_not_estimable: bool) -> SuiteValue:
    """Apply ``metric(counts)`` per task and average.

    With ``allow_not_estimable`` False any not-estimable task makes the whole suite value None (the
    headline: every planned trial exists, so a short task means the run is incomplete). With it
    True the mean is over the estimable tasks and the others are listed (the valid-runs score).
    """
    per_task: dict = {}
    short: list = []
    for task_id in sorted(counts_by_task):
        try:
            per_task[task_id] = metric(counts_by_task[task_id])
        except NotEstimable:
            short.append(task_id)
    if short and not allow_not_estimable:
        return SuiteValue(None, per_task, tuple(short))
    if not per_task:
        return SuiteValue(None, per_task, tuple(short))
    return SuiteValue(sum(per_task.values(), Fraction(0)) / len(per_task), per_task, tuple(short))


def _check_values(values) -> list:
    values = list(values)
    if len(values) < 2:
        raise NotEstimable(f"an interval needs at least 2 tasks, got {len(values)}")
    return values


def _check_resamples(resamples: int) -> None:
    if not isinstance(resamples, int) or resamples < MIN_RESAMPLES:
        raise ValueError(f"resamples must be an integer of at least {MIN_RESAMPLES}, got {resamples!r}")


def _check_level(level: float) -> float:
    if not 0 < level < 1:
        raise ValueError(f"level must be between 0 and 1, got {level!r}")
    return 1 - level


def _percentile(sorted_means: list, alpha: float) -> tuple:
    b = len(sorted_means)
    lo = sorted_means[math.floor(b * alpha / 2)]
    hi = sorted_means[math.ceil(b * (1 - alpha / 2)) - 1]
    return lo, hi


def bootstrap_interval(values, *, resamples: int = DEFAULT_RESAMPLES, seed: int = DEFAULT_SEED,
                       level: float = DEFAULT_LEVEL) -> tuple:
    """Task-level percentile bootstrap of the mean of per-task values. Returns (low, high)."""
    values = [float(v) for v in _check_values(values)]
    _check_resamples(resamples)
    alpha = _check_level(level)
    rng = random.Random(seed)
    m = len(values)
    means = sorted(sum(rng.choices(values, k=m)) / m for _ in range(resamples))
    return _percentile(means, alpha)


def clt_interval(values, *, level: float = DEFAULT_LEVEL) -> dict:
    """Normal interval over per-task values: mean +- z * sd / sqrt(m), raw and clipped to [0, 1]."""
    values = [float(v) for v in _check_values(values)]
    alpha = _check_level(level)
    m = len(values)
    mean = sum(values) / m
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (m - 1))
    half = NormalDist().inv_cdf(1 - alpha / 2) * sd / math.sqrt(m)
    return {
        "mean": mean,
        "sd": sd,
        "se": sd / math.sqrt(m),
        "raw": (mean - half, mean + half),
        "clipped": (max(0.0, mean - half), min(1.0, mean + half)),
        "se_is_zero": sd == 0.0,
    }


def align(a: dict, b: dict) -> list:
    """The common task ids of two runs, refusing runs whose task sets differ."""
    if set(a) != set(b):
        only_a = sorted(set(a) - set(b))
        only_b = sorted(set(b) - set(a))
        raise ValueError(f"the two runs cover different tasks: only in a {only_a}, only in b {only_b}")
    return sorted(a)


def paired(a: dict, b: dict, *, resamples: int = DEFAULT_RESAMPLES, seed: int = DEFAULT_SEED,
           level: float = DEFAULT_LEVEL) -> dict:
    """Paired task-level comparison of per-task values a and b (d_i = b_i - a_i), never clipped."""
    ids = align(a, b)
    diffs = [float(b[i]) - float(a[i]) for i in ids]
    _check_values(diffs)
    _check_resamples(resamples)
    alpha = _check_level(level)
    rng = random.Random(seed)
    m = len(diffs)
    indexes = range(m)
    means = sorted(sum(diffs[j] for j in rng.choices(indexes, k=m)) / m for _ in range(resamples))
    clt = clt_interval(diffs, level=level)
    return {
        "tasks": m,
        "mean_difference": sum(diffs) / m,
        "bootstrap": _percentile(means, alpha),
        "clt": clt["raw"],
        "sd_difference": clt["sd"],
        "variance_difference": clt["sd"] ** 2,
        "per_task_difference": dict(zip(ids, diffs)),
    }


def clustered_bootstrap_interval(values: dict, cluster_of: dict, *, resamples: int = DEFAULT_RESAMPLES,
                                 seed: int = DEFAULT_SEED, level: float = DEFAULT_LEVEL) -> tuple:
    """Resample clusters, then tasks inside each drawn cluster; task estimates stay fixed."""
    _check_resamples(resamples)
    alpha = _check_level(level)
    clusters: dict = {}
    for task_id in sorted(values):
        clusters.setdefault(cluster_of[task_id], []).append(float(values[task_id]))
    names = sorted(clusters)
    if len(names) < 2:
        raise NotEstimable(f"a clustered interval needs at least 2 clusters, got {len(names)}")
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        total = 0.0
        count = 0
        for name in rng.choices(names, k=len(names)):
            members = clusters[name]
            drawn = rng.choices(members, k=len(members))
            total += sum(drawn)
            count += len(drawn)
        means.append(total / count)
    means.sort()
    return _percentile(means, alpha)


def leave_one_cluster_out(values: dict, cluster_of: dict) -> dict:
    """The suite mean with each cluster left out in turn."""
    names = sorted({cluster_of[t] for t in values})
    out = {}
    for name in names:
        kept = [float(v) for t, v in values.items() if cluster_of[t] != name]
        out[name] = sum(kept) / len(kept) if kept else None
    return out


def tasks_for_difference(sd_difference: float, *, difference: float = 0.10, level: float = DEFAULT_LEVEL,
                         power: float = 0.80, design_effect: float = 1.0) -> int:
    """Paired tasks needed to see ``difference`` at the given level and power: an ESTIMATE from a pilot's
    paired standard deviation, ((z_a + z_b) * sd / difference)^2 * design effect. Not a power claim."""
    if difference <= 0 or sd_difference < 0:
        raise ValueError("difference must be positive and the standard deviation non-negative")
    z = NormalDist().inv_cdf(1 - (1 - level) / 2) + NormalDist().inv_cdf(power)
    return math.ceil(((z * sd_difference / difference) ** 2) * design_effect)
