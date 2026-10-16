#!/usr/bin/env python3
"""Fast tests for the PR performance check. Timing uses a tiny sample."""
from __future__ import annotations

import ast
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perf_compare = load("perf_compare", HERE / "perf_compare.py")
perf_bench = load("perf_bench", HERE / "perf_bench.py")


def _report(pairs: dict[str, float]) -> dict:
    return {
        "schema": perf_compare.SCHEMA,
        "provider_calls": 0,
        "benchmarks": [{"name": name, "unit": "s/op", "median_s": value} for name, value in pairs.items()],
    }


class CompareTests(unittest.TestCase):
    def test_above_ten_percent_is_a_regression(self) -> None:
        report = perf_compare.compare_reports(
            _report({"canonical_digest": 1.0, "decode_command": 1.0}),
            _report({"canonical_digest": 1.11, "decode_command": 1.09}),
        )
        self.assertTrue(report["flagged"])
        self.assertEqual([row["name"] for row in report["regressions"]], ["canonical_digest"])
        self.assertEqual(perf_compare.job_exit_code(bench_ok=True), 0)

    def test_ten_percent_exactly_is_not_over_the_limit(self) -> None:
        report = perf_compare.compare_reports(
            _report({"canonical_digest": 1.0}),
            _report({"canonical_digest": 1.10}),
        )
        self.assertFalse(report["flagged"])
        self.assertEqual(report["rows"][0]["status"], "ok")

    def test_new_and_removed_are_not_speed_regressions(self) -> None:
        report = perf_compare.compare_reports(
            _report({"kept": 1.0, "gone": 1.0}),
            _report({"kept": 0.9, "added": 1.0}),
        )
        self.assertFalse(report["flagged"])
        statuses = {row["name"]: row["status"] for row in report["rows"]}
        self.assertEqual(statuses, {"added": "new", "gone": "removed", "kept": "faster"})
        warnings = perf_compare.warning_lines(report)
        self.assertEqual(warnings, ["gone ran on the PR base and is missing from this branch."])

    def test_default_limit_is_about_ten_percent(self) -> None:
        self.assertEqual(perf_compare.DEFAULT_RATIO_LIMIT, 1.10)
        self.assertEqual(perf_compare.DEFAULT_ABSOLUTE_FLOOR_S, 25e-6)

    def test_large_percent_on_fast_bench_is_flagged(self) -> None:
        # +40% of 100 us is 40 us, over the 25 us floor.
        report = perf_compare.compare_reports(
            _report({"decode_command": 100e-6}),
            _report({"decode_command": 140e-6}),
        )
        self.assertTrue(report["flagged"])

    def test_percent_without_absolute_floor_is_not_a_regression(self) -> None:
        # +20% of 100 us is 20 us, under the 25 us floor.
        report = perf_compare.compare_reports(
            _report({"decode_command": 100e-6}),
            _report({"decode_command": 120e-6}),
        )
        self.assertFalse(report["flagged"])
        self.assertEqual(report["rows"][0]["status"], "within_floor")

    def test_percent_and_absolute_floor_both_required(self) -> None:
        report = perf_compare.compare_reports(
            _report({"canonical_digest": 1e-3}),
            _report({"canonical_digest": 1.2e-3}),
        )
        self.assertTrue(report["flagged"])
        self.assertEqual(report["rows"][0]["status"], "regression")
        self.assertGreater(report["rows"][0]["delta_s"], perf_compare.DEFAULT_ABSOLUTE_FLOOR_S)

    def test_absolute_floor_is_strict(self) -> None:
        # Binary-exact seconds so the equality check is not a float artifact.
        floor = 0.125
        baseline = 0.5
        equal = perf_compare.compare_reports(
            _report({"canonical_digest": baseline}),
            _report({"canonical_digest": baseline + floor}),
            absolute_floor_s=floor,
        )
        self.assertFalse(equal["flagged"])
        self.assertEqual(equal["rows"][0]["status"], "within_floor")
        over = perf_compare.compare_reports(
            _report({"canonical_digest": baseline}),
            _report({"canonical_digest": baseline + floor + 0.0625}),
            absolute_floor_s=floor,
        )
        self.assertTrue(over["flagged"])

    def test_zero_median_is_rejected(self) -> None:
        with self.assertRaises(perf_compare.CompareError):
            perf_compare.compare_reports(_report({"canonical_digest": 0.0}), _report({"canonical_digest": 1.0}))


class BenchTests(unittest.TestCase):
    def test_paired_run_times_both_sides(self) -> None:
        baseline, candidate = perf_bench.run_paired(
            ROOT, ROOT, rounds=2, repeats=1, round_s=0.005, warmup_s=0.001,
        )
        self.assertEqual(baseline["provider_calls"], 0)
        self.assertEqual(candidate["provider_calls"], 0)
        self.assertEqual(
            [row["name"] for row in baseline["benchmarks"]],
            [row["name"] for row in candidate["benchmarks"]],
        )

    def test_measure_pair_reports_median_of_repeats(self) -> None:
        def work() -> int:
            total = 0
            for value in range(40):
                total += value
            return total

        base, head = perf_bench.measure_pair(
            work, work, rounds=2, repeats=3, round_s=0.002, warmup_s=0.0,
        )
        self.assertEqual(base["repeats"], 3)
        self.assertEqual(len(base["pass_medians_s"]), 3)
        self.assertEqual(base["median_s"], perf_bench._positive_median(base["pass_medians_s"]))
        self.assertEqual(head["median_s"], perf_bench._positive_median(head["pass_medians_s"]))

    def test_short_suite_is_provider_free(self) -> None:
        report = perf_bench.run_suite(ROOT, rounds=2, round_s=0.005, warmup_s=0.005)
        self.assertEqual(report["provider_calls"], 0)
        self.assertEqual(
            [row["name"] for row in report["benchmarks"]],
            ["canonical_digest", "decode_command", "validate_policy", "validate_dag"],
        )
        for row in report["benchmarks"]:
            self.assertGreater(row["median_s"], 0)

    def test_cli_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bench.json"
            result = subprocess.run(
                [
                    sys.executable, "-X", "utf8", "-B", str(HERE / "perf_bench.py"),
                    "--rounds", "1", "--round-ms", "5", "--warmup-ms", "5",
                    "--out", str(out),
                ],
                cwd=ROOT,
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["provider_calls"], 0)

    def test_network_guard_blocks_connect(self) -> None:
        perf_bench.install_network_guard()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with self.assertRaises(RuntimeError):
            sock.connect(("127.0.0.1", 9))
        sock.close()

    def test_sources_do_not_import_providers(self) -> None:
        banned = {"requests", "httpx", "urllib", "openai", "anthropic", "aiohttp", "urllib3"}
        for name in ("perf_bench.py", "perf_compare.py", "perf_ci.py"):
            tree = ast.parse((HERE / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module.split(".")[0]]
                else:
                    continue
                self.assertFalse(banned.intersection(modules), f"{name} imports {modules}")


class WorkflowTests(unittest.TestCase):
    def test_linux_job_warns_on_pull_requests_without_a_hard_fail(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("tools/perf_ci.py", text)
        self.assertIn("Performance regression check", text)
        start = text.index("Performance regression check")
        step = text[start:text.index("Docs-only lightweight checks", start)]
        self.assertIn("pull_request", step)
        self.assertIn("timeout-minutes: 1\n", step)
        self.assertIn("continue-on-error: true", step)
        self.assertIn("::warning::", step)
        self.assertNotIn("exit 1", step)
        self.assertNotIn("fail-on-alert", step)
        # The required linux job stays green when the script warns and returns 0.
        self.assertEqual(perf_compare.job_exit_code(bench_ok=True), 0)


if __name__ == "__main__":
    unittest.main()
