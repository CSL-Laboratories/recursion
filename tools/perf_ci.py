#!/usr/bin/env python3
"""PR performance check. Warns above about 10%; does not fail the job for that.

Times the PR head against a detached worktree of the base commit. Both kernels
stay in one process and rounds alternate which side runs first, so a noisy
host does not look like a slowdown. The harness and the frozen workload come
from this checkout; each tree supplies only its planning kernel.

    python -X utf8 -B tools/perf_ci.py

CI sets CI_BASE_SHA to the pull request base. PERF_REGRESSION_RATIO overrides
the default 1.10 limit. PERF_ABSOLUTE_FLOOR_S overrides the default 50 us
floor. A warning requires both. Regressions print GitHub warning annotations
and a step summary, then exit 0. A harness crash, missing kernel, or invalid
report exits 1.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import perf_compare
from perf_bench import BenchError, HARNESS_ROOT, run_paired, run_suite

GIT = ["git", "-c", "core.longpaths=true", "-c", "core.quotePath=false"]
DEFAULT_RATIO = perf_compare.DEFAULT_RATIO_LIMIT


class PerfCIError(RuntimeError):
    """The check could not measure both sides."""


def _ratio_limit() -> float:
    raw = os.environ.get("PERF_REGRESSION_RATIO", "").strip()
    if not raw:
        return DEFAULT_RATIO
    try:
        value = float(raw)
    except ValueError as exc:
        raise PerfCIError(f"PERF_REGRESSION_RATIO={raw!r} is not a number") from exc
    if value <= 1:
        raise PerfCIError("PERF_REGRESSION_RATIO must be greater than 1")
    return value


def _absolute_floor() -> float:
    raw = os.environ.get("PERF_ABSOLUTE_FLOOR_S", "").strip()
    if not raw:
        return perf_compare.DEFAULT_ABSOLUTE_FLOOR_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise PerfCIError(f"PERF_ABSOLUTE_FLOOR_S={raw!r} is not a number") from exc
    if value < 0:
        raise PerfCIError("PERF_ABSOLUTE_FLOOR_S must be >= 0")
    return value


def _run(cmd: list[str], *, cwd: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        timeout=timeout,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def _worktree(sha: str) -> Path:
    probe = _run([*GIT, "cat-file", "-e", f"{sha}^{{commit}}"], cwd=HARNESS_ROOT)
    if probe.returncode != 0:
        raise PerfCIError(
            f"base commit {sha} is not in this clone; use a full-history checkout"
        )
    path = Path(tempfile.mkdtemp(prefix="perf-base-"))
    path.rmdir()
    added = _run([*GIT, "worktree", "add", "--detach", str(path), sha], cwd=HARNESS_ROOT)
    if added.returncode != 0:
        sys.stderr.write(added.stderr)
        raise PerfCIError(f"git worktree add failed for {sha[:12]}")
    return path


def _remove_worktree(path: Path | None) -> None:
    if path is None:
        return
    _run([*GIT, "worktree", "remove", "--force", str(path)], cwd=HARNESS_ROOT)


def _command_text(text: str) -> str:
    return (
        text.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def _emit(report: dict, *, baseline_label: str, candidate_label: str) -> None:
    markdown = perf_compare.render_markdown(
        report, baseline_label=baseline_label, candidate_label=candidate_label,
    )
    print(markdown, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(markdown)
    warnings = perf_compare.warning_lines(report)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for line in warnings:
            print(f"::warning title=Performance regression::{_command_text(line)}", flush=True)
    else:
        for line in warnings:
            print(f"WARNING: {line}", flush=True)


def _print_side(label: str, report: dict) -> None:
    for row in report["benchmarks"]:
        print(
            f"PERF_BENCH side={label} name={row['name']} median_s={row['median_s']:.9f} "
            f"rounds={row['rounds']} repeats={row.get('repeats', 1)} "
            f"iters={row['iterations_per_round']}",
            flush=True,
        )


def main() -> int:
    started = time.perf_counter()
    base_sha = os.environ.get("CI_BASE_SHA", "").strip()
    tree: Path | None = None
    try:
        limit = _ratio_limit()
        floor = _absolute_floor()
        if not base_sha or base_sha == "0" * 40:
            head = run_suite(HARNESS_ROOT)
            _print_side("head", head)
            print("PERF_CI_NO_BASE no CI_BASE_SHA; recorded head only", flush=True)
            print(f"PERF_CI_SECONDS={time.perf_counter() - started:.3f}", flush=True)
            return job_ok()
        tree = _worktree(base_sha)
        baseline, head = run_paired(tree, HARNESS_ROOT)
        _print_side("base", baseline)
        _print_side("head", head)
        report = perf_compare.compare_reports(
            baseline, head, ratio_limit=limit, absolute_floor_s=floor,
        )
        _emit(report, baseline_label=base_sha[:12], candidate_label="head")
        print(f"PERF_CI_SECONDS={time.perf_counter() - started:.3f}", flush=True)
        print(f"PERF_REGRESSIONS={len(report['regressions'])}", flush=True)
        return job_ok()
    except (PerfCIError, BenchError) as exc:
        print(f"PERF_CI_FAIL: {exc}", file=sys.stderr)
        return perf_compare.job_exit_code(bench_ok=False)
    finally:
        _remove_worktree(tree)


def job_ok() -> int:
    return perf_compare.job_exit_code(bench_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"PERF_CI_FAIL: {exc}", file=sys.stderr)
        raise SystemExit(perf_compare.job_exit_code(bench_ok=False))
