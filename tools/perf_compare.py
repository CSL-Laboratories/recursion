#!/usr/bin/env python3
"""Compare two provider-free benchmark reports.

Lower time is better. A row is a regression only when the candidate median
is slower by more than the ratio limit (default 1.10, about 10%) and by more
than an absolute floor (default 25 microseconds). A large percentage on a
tiny delta is runner jitter, not a warning. The comparison never decides
that CI should fail; callers warn instead.
"""
from __future__ import annotations

import math
from typing import Any

SCHEMA = "recursion-perf-bench/1"
DEFAULT_RATIO_LIMIT = 1.10
# Fast benches are ~100–160 us, so a 10% move is 10–16 us. 25 us sits above
# paired-median jitter yet still flags a 40% move (40 us) on a 100 us bench.
DEFAULT_ABSOLUTE_FLOOR_S = 25e-6


class CompareError(ValueError):
    """A report is missing, duplicated, or not a finite positive time."""


def _reports(payload: dict[str, Any], label: str) -> dict[str, float]:
    if not isinstance(payload, dict):
        raise CompareError(f"{label} report is not an object")
    if payload.get("schema") != SCHEMA:
        raise CompareError(f"{label} report schema is {payload.get('schema')!r}")
    rows = payload.get("benchmarks")
    if not isinstance(rows, list) or not rows:
        raise CompareError(f"{label} report has no benchmarks")
    found: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CompareError(f"{label} benchmark row is not an object")
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise CompareError(f"{label} benchmark is missing a name")
        if name in found:
            raise CompareError(f"{label} benchmark {name} is duplicated")
        median = row.get("median_s")
        if isinstance(median, bool) or not isinstance(median, (int, float)):
            raise CompareError(f"{label} benchmark {name} median_s is not a number")
        if not math.isfinite(median) or median <= 0:
            raise CompareError(f"{label} benchmark {name} median_s must be positive")
        found[name] = float(median)
    return found


def compare_reports(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    ratio_limit: float = DEFAULT_RATIO_LIMIT,
    absolute_floor_s: float = DEFAULT_ABSOLUTE_FLOOR_S,
) -> dict[str, Any]:
    """Return a JSON-ready report. Regressions are listed; nothing here fails CI."""
    if not math.isfinite(ratio_limit) or ratio_limit <= 1:
        raise CompareError("ratio limit must be a finite number greater than 1")
    if isinstance(absolute_floor_s, bool) or not isinstance(absolute_floor_s, (int, float)):
        raise CompareError("absolute floor must be a number of seconds")
    if not math.isfinite(absolute_floor_s) or absolute_floor_s < 0:
        raise CompareError("absolute floor must be zero or a positive finite number")
    base = _reports(baseline, "baseline")
    head = _reports(candidate, "candidate")
    rows: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    for name in sorted(set(base) | set(head)):
        base_s = base.get(name)
        head_s = head.get(name)
        if base_s is None:
            rows.append({
                "name": name, "baseline_s": None, "candidate_s": head_s,
                "ratio": None, "delta_pct": None, "status": "new",
            })
            continue
        if head_s is None:
            rows.append({
                "name": name, "baseline_s": base_s, "candidate_s": None,
                "ratio": None, "delta_pct": None, "status": "removed",
            })
            continue
        ratio = head_s / base_s
        delta_s = head_s - base_s
        delta_pct = (ratio - 1.0) * 100.0
        # Both gates are strict: equal to 10% or equal to the floor is not over it.
        over_ratio = ratio > ratio_limit
        over_floor = delta_s > absolute_floor_s
        if over_ratio and over_floor:
            status = "regression"
        elif over_ratio:
            status = "within_floor"
        elif ratio < 1:
            status = "faster"
        else:
            status = "ok"
        row = {
            "name": name,
            "baseline_s": base_s,
            "candidate_s": head_s,
            "ratio": ratio,
            "delta_s": delta_s,
            "delta_pct": delta_pct,
            "status": status,
        }
        rows.append(row)
        if status == "regression":
            regressions.append(row)
    rows.sort(key=lambda item: item["name"])
    return {
        "schema": "recursion-perf-compare/1",
        "ratio_limit": ratio_limit,
        "absolute_floor_s": absolute_floor_s,
        "flagged": bool(regressions),
        "regressions": regressions,
        "rows": rows,
    }


def job_exit_code(*, bench_ok: bool) -> int:
    """A slow result is a warning. Only a broken harness fails the job."""
    return 0 if bench_ok else 1


def format_seconds(seconds: float) -> str:
    if seconds < 1e-3:
        return f"{seconds * 1e6:.1f} us"
    if seconds < 1:
        return f"{seconds * 1e3:.2f} ms"
    return f"{seconds:.3f} s"


def render_markdown(report: dict[str, Any], *, baseline_label: str, candidate_label: str) -> str:
    limit_pct = (report["ratio_limit"] - 1.0) * 100.0
    floor = format_seconds(report["absolute_floor_s"])
    lines = [
        "## Performance vs master",
        "",
        f"Threshold: warn when the median {candidate_label} time is slower than "
        f"{baseline_label} by more than {limit_pct:.0f}% "
        f"(ratio > {report['ratio_limit']:.2f}) and by more than {floor}. "
        "A larger percentage on a smaller absolute gap is treated as jitter. "
        "This check does not fail the job.",
        "",
        f"| benchmark | {baseline_label} | {candidate_label} | delta | status |",
        "|---|---:|---:|---:|---|",
    ]
    for row in report["rows"]:
        base = "—" if row["baseline_s"] is None else format_seconds(row["baseline_s"])
        head = "—" if row["candidate_s"] is None else format_seconds(row["candidate_s"])
        if row["delta_pct"] is None:
            delta = "—"
        else:
            delta = f"{row['delta_pct']:+.1f}% ({format_seconds(abs(row['delta_s']))})"
        lines.append(f"| `{row['name']}` | {base} | {head} | {delta} | {row['status']} |")
    if not report["flagged"]:
        lines.extend(["", "No benchmark was slower than the threshold."])
    else:
        lines.extend(["", "Flagged regressions (warning only):"])
        for row in report["regressions"]:
            lines.append(
                f"- `{row['name']}` {row['delta_pct']:+.1f}% "
                f"({format_seconds(row['baseline_s'])} -> {format_seconds(row['candidate_s'])})"
            )
    lines.append("")
    return "\n".join(lines)


def warning_lines(report: dict[str, Any]) -> list[str]:
    lines = []
    for row in report["regressions"]:
        lines.append(
            f"{row['name']} median is {row['delta_pct']:.1f}% slower than the PR base "
            f"({format_seconds(row['baseline_s'])} -> {format_seconds(row['candidate_s'])} per op, "
            f"{format_seconds(row['delta_s'])} absolute). "
            f"Threshold is {(report['ratio_limit'] - 1) * 100:.0f}% and "
            f"{format_seconds(report['absolute_floor_s'])}."
        )
    for row in report["rows"]:
        if row["status"] == "removed":
            lines.append(f"{row['name']} ran on the PR base and is missing from this branch.")
    return lines
