#!/usr/bin/env python3
"""Small provider-free benchmark of the M1A planning kernel.

Times canonical digest, strict decode, policy validation, and DAG validation.
The workload is frozen in tools/perf_workload.json plus a fixed in-memory
document, so a PR and its base run the same inputs. No network and no live
AI provider is used.

    python -X utf8 -B tools/perf_bench.py --out /tmp/head.json
    python -X utf8 -B tools/perf_bench.py --root /path/to/tree --out /tmp/base.json
"""
from __future__ import annotations

import argparse
import gc
import json
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

SCHEMA = "recursion-perf-bench/1"
HARNESS_ROOT = Path(__file__).resolve().parents[1]
WORKLOAD_PATH = Path(__file__).with_name("perf_workload.json")
DEFAULT_ROUNDS = 5
DEFAULT_ROUND_MS = 50
DEFAULT_WARMUP_MS = 30
# Paired PR comparison. Rounds alternate which side goes first so a warm cache
# does not land on only one tree. 8 * 50ms * 2 sides * 4 benches is a few seconds.
PAIRED_ROUNDS = 8
PAIRED_ROUND_S = 0.05
PAIRED_WARMUP_S = 0.02
PAIRED_DEADLINE_S = 40
DAG_NODES = 32

_GUARD_INSTALLED = False


class BenchError(RuntimeError):
    """The suite could not produce a measurement."""


def install_network_guard() -> None:
    """Refuse connect/send so a benchmark cannot call a live provider."""
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return
    original = socket.socket

    class GuardedSocket(original):
        def connect(self, address):  # noqa: ANN001 - socket API
            raise RuntimeError("perf benchmarks must not open network connections")

        def connect_ex(self, address):  # noqa: ANN001
            raise RuntimeError("perf benchmarks must not open network connections")

        def sendto(self, data, *args):  # noqa: ANN001
            raise RuntimeError("perf benchmarks must not open network connections")

    def blocked(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("perf benchmarks must not open network connections")

    socket.socket = GuardedSocket
    socket.create_connection = blocked
    _GUARD_INSTALLED = True


def _drop_imported_product() -> None:
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            del sys.modules[name]


def load_product(root: Path):
    """Import the planning kernel from ``root``, not from a previously imported tree."""
    sys.dont_write_bytecode = True
    root_s = str(root.resolve())
    sys.path = [path for path in sys.path if path != root_s]
    sys.path.insert(0, root_s)
    _drop_imported_product()
    from scripts.v3.planning.validation.canonical import (  # noqa: WPS433
        decode, digest, validate_policy_fixture,
    )
    from scripts.v3.planning.validation.contracts import validate_dag  # noqa: WPS433

    return SimpleNamespace(
        decode=decode,
        digest=digest,
        validate_policy_fixture=validate_policy_fixture,
        validate_dag=validate_dag,
    )


def _document() -> dict:
    rows = []
    for index in range(64):
        rows.append({
            "id": f"task-{index:04d}",
            "version": index,
            "flags": [index % 2 == 0, index % 3 == 0],
            "nested": {
                "path": f"goals/{index}/spec",
                "tokens": index * 10,
                "note": "x" * 32,
            },
        })
    return {"schema": "bench", "rows": rows, "tags": {str(index): index for index in range(32)}}


def _dag(nodes: int = DAG_NODES):
    tasks = []
    edges = []
    plan_ref = {"object_type": "plan-revision", "id": "plan-1", "version": 1}
    for index in range(nodes):
        node_id = f"n{index:03d}"
        deps = [f"n{index - 1:03d}"] if index else []
        if index:
            edges.append({"from_node_id": deps[0], "to_node_id": node_id})
        tasks.append({
            "object_type": "task-node",
            "id": node_id,
            "version": 1,
            "content_digest": "ab" * 32,
            "payload": {
                "plan_ref": dict(plan_ref),
                "dependency_node_ids": deps,
                "write_resource_paths": [f"out/{index}"],
                "read_resource_paths": [f"in/{index}"],
                "effect_keys": [f"effect-{index}"],
            },
        })
    refs = sorted(
        (
            {
                "object_type": task["object_type"],
                "id": task["id"],
                "version": task["version"],
                "content_digest": task["content_digest"],
            }
            for task in tasks
        ),
        key=lambda item: item["id"],
    )
    plan = {
        "object_type": "plan-revision",
        "id": "plan-1",
        "version": 1,
        "payload": {"task_node_refs": refs, "dependency_edges": edges},
    }
    return plan, tasks


def load_workload() -> dict:
    try:
        payload = json.loads(WORKLOAD_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchError(f"cannot read {WORKLOAD_PATH.name}: {exc}") from exc
    if not isinstance(payload.get("command_json"), str) or not payload["command_json"]:
        raise BenchError("workload command_json is missing")
    policies = payload.get("policies")
    if not isinstance(policies, list) or not policies:
        raise BenchError("workload policies are missing")
    return payload


def build_cases(product, workload: dict) -> list[tuple[str, object]]:
    document = _document()
    command_json = workload["command_json"]
    policies = workload["policies"]
    plan, tasks = _dag()

    def canonical_digest() -> None:
        digest = product.digest(document)
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeError("digest was not a sha256 hex string")

    def decode_command() -> None:
        decoded = product.decode(command_json)
        if decoded.get("command_type") != "record-owner-intent":
            raise RuntimeError("frozen command did not decode")

    def validate_policy() -> None:
        for policy in policies:
            product.validate_policy_fixture(policy)

    def validate_dag() -> None:
        error, _witness = product.validate_dag(plan, tasks)
        if error is not None:
            raise RuntimeError(error)

    return [
        ("canonical_digest", canonical_digest),
        ("decode_command", decode_command),
        ("validate_policy", validate_policy),
        ("validate_dag", validate_dag),
    ]


def measure(fn, *, rounds: int, round_s: float, warmup_s: float) -> dict:
    """Median seconds per call. Warmup and GC pauses sit outside the samples."""
    if rounds < 1:
        raise BenchError("rounds must be >= 1")
    deadline = time.perf_counter() + warmup_s
    while time.perf_counter() < deadline:
        fn()
    started = time.perf_counter()
    counted = 0
    while counted < 1 or (time.perf_counter() - started < min(0.01, round_s) and counted < 10000):
        fn()
        counted += 1
    per_call = (time.perf_counter() - started) / counted
    if per_call <= 0:
        per_call = 1e-9
    iterations = max(1, min(20000, int(round_s / per_call)))
    samples: list[float] = []
    for _ in range(rounds):
        gc.collect()
        gc.disable()
        try:
            start = time.perf_counter()
            for _index in range(iterations):
                fn()
            elapsed = time.perf_counter() - start
        finally:
            gc.enable()
        samples.append(elapsed / iterations)
    ordered = sorted(samples)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        raise BenchError("median sample was not positive")
    return {
        "median_s": median,
        "rounds": rounds,
        "iterations_per_round": iterations,
        "samples_s": samples,
    }


def _positive_median(samples: list[float]) -> float:
    ordered = sorted(samples)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        raise BenchError("median sample was not positive")
    return median


def _calibrate_iterations(fn, round_s: float) -> int:
    started = time.perf_counter()
    counted = 0
    while counted < 1 or (time.perf_counter() - started < min(0.01, round_s) and counted < 10000):
        fn()
        counted += 1
    per_call = (time.perf_counter() - started) / counted
    if per_call <= 0:
        per_call = 1e-9
    return max(1, min(20000, int(round_s / per_call)))


def _timed_call(fn, iterations: int) -> float:
    gc.disable()
    try:
        start = time.perf_counter()
        for _index in range(iterations):
            fn()
        elapsed = time.perf_counter() - start
    finally:
        gc.enable()
    return elapsed / iterations


def measure_pair(base_fn, head_fn, *, rounds: int, round_s: float, warmup_s: float) -> tuple[dict, dict]:
    """Interleave both sides and return median seconds per call for each."""
    if rounds < 2 or rounds % 2:
        raise BenchError("paired rounds must be an even number >= 2")
    for fn in (base_fn, head_fn):
        deadline = time.perf_counter() + warmup_s
        while time.perf_counter() < deadline:
            fn()
    for _ in range(8):
        base_fn()
        head_fn()
    # Per-side counts keep each round near round_s, so a large regression on one
    # side cannot multiply the fast side's iteration count and blow the deadline.
    base_iters = _calibrate_iterations(base_fn, round_s)
    head_iters = _calibrate_iterations(head_fn, round_s)
    base_samples: list[float] = []
    head_samples: list[float] = []
    for index in range(rounds):
        if index % 2 == 0:
            base_samples.append(_timed_call(base_fn, base_iters))
            head_samples.append(_timed_call(head_fn, head_iters))
        else:
            head_samples.append(_timed_call(head_fn, head_iters))
            base_samples.append(_timed_call(base_fn, base_iters))
    return (
        {"median_s": _positive_median(base_samples), "samples_s": base_samples,
         "rounds": rounds, "iterations_per_round": base_iters},
        {"median_s": _positive_median(head_samples), "samples_s": head_samples,
         "rounds": rounds, "iterations_per_round": head_iters},
    )


def _report(root: Path, benchmarks: list[dict]) -> dict:
    return {
        "schema": SCHEMA,
        "provider_calls": 0,
        "python": sys.version.split()[0],
        "root": str(root),
        "workload": WORKLOAD_PATH.name,
        "benchmarks": benchmarks,
    }


def _check_cases(cases: list[tuple[str, object]]) -> None:
    for name, fn in cases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - surface the kernel error and stop
            raise BenchError(f"{name} failed before timing: {exc}") from exc


def run_paired(
    baseline_root: Path,
    candidate_root: Path,
    *,
    rounds: int = PAIRED_ROUNDS,
    round_s: float = PAIRED_ROUND_S,
    warmup_s: float = PAIRED_WARMUP_S,
    deadline_s: float = PAIRED_DEADLINE_S,
) -> tuple[dict, dict]:
    """Time the same workload on two trees in one process.

    Separate processes on a noisy host false-flag identical code. Alternating
    rounds keeps both kernels on the same CPU window.
    """
    install_network_guard()
    started = time.perf_counter()
    baseline_root = baseline_root.resolve()
    candidate_root = candidate_root.resolve()
    for root in (baseline_root, candidate_root):
        if not (root / "scripts" / "v3" / "planning" / "validation" / "canonical.py").is_file():
            raise BenchError(f"{root} has no M1A planning kernel")
    workload = load_workload()
    # Capture baseline callables before the candidate import replaces sys.modules.
    baseline_cases = build_cases(load_product(baseline_root), workload)
    _check_cases(baseline_cases)
    candidate_cases = build_cases(load_product(candidate_root), workload)
    _check_cases(candidate_cases)
    if [name for name, _fn in baseline_cases] != [name for name, _fn in candidate_cases]:
        raise BenchError("baseline and candidate benchmark names differ")
    base_rows = []
    head_rows = []
    for (name, base_fn), (_head_name, head_fn) in zip(baseline_cases, candidate_cases):
        if time.perf_counter() - started > deadline_s:
            raise BenchError(f"paired benchmark exceeded {deadline_s:.0f}s")
        try:
            base_sample, head_sample = measure_pair(
                base_fn, head_fn, rounds=rounds, round_s=round_s, warmup_s=warmup_s,
            )
        except BenchError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BenchError(f"{name} failed during timing: {exc}") from exc
        base_rows.append({"name": name, "unit": "s/op", **base_sample})
        head_rows.append({"name": name, "unit": "s/op", **head_sample})
    return _report(baseline_root, base_rows), _report(candidate_root, head_rows)


def run_suite(
    root: Path,
    *,
    rounds: int = DEFAULT_ROUNDS,
    round_s: float = DEFAULT_ROUND_MS / 1000,
    warmup_s: float = DEFAULT_WARMUP_MS / 1000,
) -> dict:
    install_network_guard()
    root = root.resolve()
    if not (root / "scripts" / "v3" / "planning" / "validation" / "canonical.py").is_file():
        raise BenchError(f"{root} has no M1A planning kernel")
    product = load_product(root)
    workload = load_workload()
    cases = build_cases(product, workload)
    for name, fn in cases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - surface the kernel error and stop
            raise BenchError(f"{name} failed before timing: {exc}") from exc
    benchmarks = []
    for name, fn in cases:
        try:
            sample = measure(fn, rounds=rounds, round_s=round_s, warmup_s=warmup_s)
        except BenchError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BenchError(f"{name} failed during timing: {exc}") from exc
        benchmarks.append({"name": name, "unit": "s/op", **sample})
    return {
        "schema": SCHEMA,
        "provider_calls": 0,
        "python": sys.version.split()[0],
        "root": str(root),
        "workload": WORKLOAD_PATH.name,
        "benchmarks": benchmarks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=HARNESS_ROOT, help="tree whose kernel is timed")
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--round-ms", type=float, default=DEFAULT_ROUND_MS)
    parser.add_argument("--warmup-ms", type=float, default=DEFAULT_WARMUP_MS)
    args = parser.parse_args(argv)
    if args.round_ms <= 0 or args.warmup_ms < 0:
        raise SystemExit("round-ms must be positive and warmup-ms must be >= 0")
    try:
        report = run_suite(
            args.root,
            rounds=args.rounds,
            round_s=args.round_ms / 1000,
            warmup_s=args.warmup_ms / 1000,
        )
    except BenchError as exc:
        print(f"PERF_BENCH_FAIL: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for row in report["benchmarks"]:
        print(
            f"PERF_BENCH name={row['name']} median_s={row['median_s']:.9f} "
            f"rounds={row['rounds']} iters={row['iterations_per_round']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"PERF_BENCH_FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
