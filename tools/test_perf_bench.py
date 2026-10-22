#!/usr/bin/env python3
"""Fast tests for the PR performance check. Timing uses a tiny sample."""
from __future__ import annotations

import ast
import gc
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

    def test_timed_call_restores_the_callers_gc_state(self) -> None:
        gc_was_enabled = gc.isenabled()
        try:
            gc.enable()
            perf_bench._timed_call(lambda: None, 1)
            self.assertTrue(gc.isenabled())
            gc.disable()
            perf_bench._timed_call(lambda: None, 1)
            self.assertFalse(gc.isenabled())
        finally:
            if gc_was_enabled:
                gc.enable()
            else:
                gc.disable()

    def test_timed_call_keeps_gc_on_when_the_callback_enables_it(self) -> None:
        # timeit parity: one-way restore. A callback that enables GC is not
        # re-disabled, even when the caller entered with GC off.
        gc_was_enabled = gc.isenabled()
        try:
            gc.disable()
            perf_bench._timed_call(gc.enable, 1)
            self.assertTrue(gc.isenabled())
        finally:
            if gc_was_enabled:
                gc.enable()
            else:
                gc.disable()

    def test_timed_call_restores_gc_when_the_callback_raises(self) -> None:
        def boom() -> None:
            raise RuntimeError("boom")

        gc_was_enabled = gc.isenabled()
        try:
            gc.enable()
            with self.assertRaises(RuntimeError):
                perf_bench._timed_call(boom, 1)
            self.assertTrue(gc.isenabled())
            gc.disable()
            with self.assertRaises(RuntimeError):
                perf_bench._timed_call(boom, 1)
            self.assertFalse(gc.isenabled())
        finally:
            if gc_was_enabled:
                gc.enable()
            else:
                gc.disable()

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
        # CSL-255: the perf step now lives in linux-tests, directly before the required linux job.
        step = text[start:text.index("\n  linux:\n", start)]
        self.assertIn("pull_request", step)
        self.assertIn("timeout 55 ", step)
        self.assertIn("timeout-minutes: 1\n", step)
        self.assertIn("continue-on-error: true", step)
        self.assertIn("id: perf", step)
        self.assertIn("always() && steps.perf.outcome == 'failure'", step)
        self.assertIn("::warning::", step)
        self.assertNotIn("exit 1", step)
        self.assertNotIn("fail-on-alert", step)
        # The required linux job stays green when the script warns and returns 0.
        self.assertEqual(perf_compare.job_exit_code(bench_ok=True), 0)

    def test_unrelated_label_events_do_not_post_linux(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        preamble = text.split("jobs:", 1)[0]
        self.assertNotIn("linux-label-skip:", text)
        self.assertIn("types: [opened, synchronize, reopened, ready_for_review]", preamble)
        self.assertNotIn("types: [opened, synchronize, reopened, ready_for_review, labeled", preamble)
        self.assertNotRegex(preamble, r"(?m)^if:")
        self.assertIn("ready_for_review", preamble)
        self.assertIn("cancel-in-progress: true", preamble)
        self.assertNotIn("label-churn", text)
        self.assertEqual(text.count("name: linux"), 1)
        linux = text[text.index("  linux:\n") : text.index("  windows:")]
        # CSL-255: the required linux job is a free aggregate; the paid suite is linux-tests.
        self.assertIn("runs-on: ubuntu-latest", linux)
        tests = text[text.index("  linux-tests:\n") : text.index("  linux:\n")]
        self.assertIn("runs-on: ubuntu-24.04", tests)
        self.assertIn("if: always()\n", linux)
        self.assertNotRegex(linux, r"(?m)^    if: .*draft")
        self.assertNotIn("github.event.action != 'labeled'", linux)
        windows = text[text.index("  windows:") : text.index("  macos:")]
        self.assertIn("needs: [preflight, settle, linux]", windows)
        self.assertIn("needs.linux.result == 'success'", windows)
        self.assertIn("'ci:full'", windows)
        label_wf = (ROOT / ".github" / "workflows" / "ci-full-label.yml").read_text(
            encoding="utf-8"
        )
        on_block = label_wf[label_wf.index("on:") : label_wf.index("concurrency:")]
        self.assertIn("types: [labeled]", on_block)
        self.assertNotIn("unlabeled", on_block)
        self.assertNotRegex(label_wf, r"(?m)^  linux:")
        self.assertNotRegex(label_wf, r"(?m)^  windows:")
        self.assertNotRegex(label_wf, r"(?m)^  macos:")
        self.assertNotIn("name: linux", label_wf)
        self.assertNotIn("name: windows", label_wf)
        self.assertNotIn("name: macos", label_wf)
        self.assertIn("name: ci-full-extra-os", label_wf)
        self.assertIn("github.event.label.name == 'ci:full'", label_wf)
        self.assertIn("uses: ./.github/workflows/ci-full-matrix.yml", label_wf)
        self.assertNotIn("needs: linux", label_wf)
        for line in label_wf.splitlines():
            if line.lstrip().startswith("run:"):
                self.assertNotIn(
                    "github.event.label.name",
                    line,
                    "label name must not enter a shell step",
                )
        matrix_wf = (ROOT / ".github" / "workflows" / "ci-full-matrix.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_call:", matrix_wf)
        self.assertNotRegex(matrix_wf, r"(?m)^  linux:")
        self.assertNotIn("name: linux", matrix_wf)
        self.assertIn("name: windows", matrix_wf)
        self.assertIn("name: macos", matrix_wf)
        self.assertNotIn("needs: linux", matrix_wf)
        # CSL-408: the ci:full Windows job runs on the free GitHub-hosted image.
        self.assertIn("runs-on: windows-2025", matrix_wf)
        pr_lint = (ROOT / ".github" / "workflows" / "pr-lint.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-latest", pr_lint)
        self.assertNotIn("blacksmith", pr_lint)
        self.assertIn("ready_for_review", pr_lint)
        self.assertIn("github.event.pull_request.draft == false", pr_lint)
        nightly = (ROOT / ".github" / "workflows" / "ci-macos-nightly.yml").read_text(
            encoding="utf-8"
        )
        recent = nightly[nightly.index("  recent:") : nightly.index("  macos:")]
        self.assertIn("name: recent-commits", recent)
        self.assertIn("runs-on: ubuntu-latest", recent)
        self.assertNotIn("blacksmith", recent)


if __name__ == "__main__":
    unittest.main()
