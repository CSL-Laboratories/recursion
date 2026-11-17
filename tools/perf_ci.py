#!/usr/bin/env python3
"""PR performance check. Warns above about 10%; does not fail the job for that.

Times the PR head against a detached worktree of the base commit. Both kernels
stay in one process and rounds alternate which side runs first, so a noisy
host does not look like a slowdown. The harness and the frozen workload come
from this checkout; each tree supplies only its planning kernel.

    python -X utf8 -B tools/perf_ci.py

CI sets CI_BASE_SHA to the pull request base. PERF_REGRESSION_RATIO overrides
the default 1.10 limit. PERF_ABSOLUTE_FLOOR_S overrides the default 25 us
floor. A warning requires both. Regressions print GitHub warning annotations
and a step summary, then exit 0. A harness crash, missing kernel, or invalid
report exits 1.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import perf_compare
from perf_bench import BenchError, HARNESS_ROOT, run_paired, run_suite

GIT = ["git", "-c", "core.longpaths=true", "-c", "core.quotePath=false"]
# Each git call has its own deadline. Three calls at this limit stay under
# the workflow step's outer limit of 55 seconds.
GIT_TIMEOUT_SECONDS = 15
DEFAULT_RATIO = perf_compare.DEFAULT_RATIO_LIMIT


class PerfCIError(RuntimeError):
    """The check could not measure both sides."""


def _env_float(name: str, default: float) -> float:
    """Parse a finite float env override, or `default` when unset or blank.

    NaN and infinities parse as floats but slip past the callers' bound checks
    (every NaN comparison is false), so they are rejected here.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise PerfCIError(f"{name} is not a number") from exc
    if not math.isfinite(value):
        raise PerfCIError(f"{name}={raw!r} must be a finite number")
    return value


def _ratio_limit() -> float:
    value = _env_float("PERF_REGRESSION_RATIO", DEFAULT_RATIO)
    if value <= 1:
        raise PerfCIError("PERF_REGRESSION_RATIO must be greater than 1")
    return value


def _absolute_floor() -> float:
    value = _env_float("PERF_ABSOLUTE_FLOOR_S", perf_compare.DEFAULT_ABSOLUTE_FLOOR_S)
    if value < 0:
        raise PerfCIError("PERF_ABSOLUTE_FLOOR_S must be >= 0")
    return value


def _run(cmd: list[str], *, cwd: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    if timeout is None:
        timeout = GIT_TIMEOUT_SECONDS
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or float(timeout) <= 0:
        raise PerfCIError("git deadline must be a positive finite number of seconds")
    try:
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
    except subprocess.TimeoutExpired as exc:
        raise PerfCIError(f"git timed out after {timeout} seconds: {_command_text(' '.join(cmd))}") from exc


def _delete_tree(path: Path) -> None:
    """Drop a leftover worktree directory. Does not call ``_run``."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink(missing_ok=True)


def _unregister_worktree(path: Path) -> None:
    """Drop a git worktree registration without going through ``_run``."""
    try:
        subprocess.run(
            [*GIT, "worktree", "remove", "--force", str(path)],
            cwd=HARNESS_ROOT,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            text=True,
            capture_output=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    _delete_tree(path)


def _worktree(sha: str) -> Path:
    probe = _run([*GIT, "cat-file", "-e", f"{sha}^{{commit}}"], cwd=HARNESS_ROOT)
    if probe.returncode != 0:
        raise PerfCIError(
            f"base commit {sha} is not in this clone; use a full-history checkout"
        )
    path = Path(tempfile.mkdtemp(prefix="perf-base-"))
    path.rmdir()
    try:
        added = _run([*GIT, "worktree", "add", "--detach", str(path), sha], cwd=HARNESS_ROOT)
    except subprocess.TimeoutExpired as exc:
        _unregister_worktree(path)
        raise PerfCIError(f"git worktree add timed out for {_command_text(sha[:12])}") from exc
    except PerfCIError:
        _unregister_worktree(path)
        raise
    if added.returncode != 0:
        detail = _command_text(added.stderr)
        if detail:
            sys.stderr.write(detail + "\n")
        _unregister_worktree(path)
        raise PerfCIError(f"git worktree add failed for {_command_text(sha[:12])}")
    return path


def _remove_worktree(path: Path | None) -> None:
    if path is None:
        return
    try:
        removed = _run([*GIT, "worktree", "remove", "--force", str(path)], cwd=HARNESS_ROOT)
    except subprocess.TimeoutExpired as exc:
        _unregister_worktree(path)
        raise PerfCIError(f"git worktree remove timed out: {_command_text(str(path))}") from exc
    except PerfCIError:
        _unregister_worktree(path)
        raise
    if removed.returncode != 0:
        detail = _command_text(removed.stderr.strip()) or f"exit {removed.returncode}"
        raise PerfCIError(f"git worktree remove failed: {detail}")


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
            print(f"::warning title=Performance regression::{line}", flush=True)
    else:
        for line in warnings:
            print(f"WARNING: {line}", flush=True)


def _print_side(label: str, report: dict) -> None:
    for row in report["benchmarks"]:
        print(
            f"PERF_BENCH side={label} name={_command_text(str(row['name']))} median_s={row['median_s']:.9f} "
            f"rounds={row['rounds']} repeats={row.get('repeats', 1)} "
            f"iters={row['iterations_per_round']}",
            flush=True,
        )


def main() -> int:
    started = time.perf_counter()
    base_sha = os.environ.get("CI_BASE_SHA", "").strip()
    tree: Path | None = None
    code = job_ok()
    try:
        limit = _ratio_limit()
        floor = _absolute_floor()
        if not base_sha or base_sha == "0" * 40:
            head = run_suite(HARNESS_ROOT)
            _print_side("head", head)
            print("PERF_CI_NO_BASE no CI_BASE_SHA; recorded head only", flush=True)
            print(f"PERF_CI_SECONDS={time.perf_counter() - started:.3f}", flush=True)
            code = job_ok()
        else:
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
            code = job_ok()
    except (PerfCIError, BenchError, perf_compare.CompareError) as exc:
        print(f"PERF_CI_FAIL: {_command_text(str(exc))}", file=sys.stderr)
        code = perf_compare.job_exit_code(bench_ok=False)
    finally:
        try:
            _remove_worktree(tree)
        except PerfCIError as exc:
            print(f"PERF_CI_FAIL: {_command_text(str(exc))}", file=sys.stderr)
            code = perf_compare.job_exit_code(bench_ok=False)
    return code


def job_ok() -> int:
    return perf_compare.job_exit_code(bench_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"PERF_CI_FAIL: {_command_text(str(exc))}", file=sys.stderr)
        raise SystemExit(perf_compare.job_exit_code(bench_ok=False))
