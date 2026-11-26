#!/usr/bin/env python3
"""Fast tests for the PR performance check. Timing uses a tiny sample."""
from __future__ import annotations

import ast
import gc
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
# perf_ci imports its siblings by name, so register them before loading it.
sys.modules.setdefault("perf_compare", perf_compare)
sys.modules.setdefault("perf_bench", perf_bench)
perf_ci = load("perf_ci", HERE / "perf_ci.py")


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
            ["canonical_digest", "decode_command", "validate_policy", "validate_dag", "replay_reduce"],
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


class EnvOverrideTests(unittest.TestCase):
    NON_FINITE = ("nan", "NaN", "-nan", "inf", "+inf", "-inf", "Infinity", "1e999")

    def _with_env(self, name: str, raw: str, fn):
        with mock.patch.dict(os.environ, {name: raw}):
            return fn()

    def test_unset_or_blank_uses_defaults(self) -> None:
        for raw in ("", "   "):
            self.assertEqual(
                self._with_env("PERF_REGRESSION_RATIO", raw, perf_ci._ratio_limit),
                perf_ci.DEFAULT_RATIO,
            )
            self.assertEqual(
                self._with_env("PERF_ABSOLUTE_FLOOR_S", raw, perf_ci._absolute_floor),
                perf_compare.DEFAULT_ABSOLUTE_FLOOR_S,
            )

    def test_finite_values_are_accepted(self) -> None:
        self.assertEqual(self._with_env("PERF_REGRESSION_RATIO", " 1.25 ", perf_ci._ratio_limit), 1.25)
        self.assertEqual(self._with_env("PERF_ABSOLUTE_FLOOR_S", "0", perf_ci._absolute_floor), 0.0)
        self.assertEqual(self._with_env("PERF_ABSOLUTE_FLOOR_S", "5e-5", perf_ci._absolute_floor), 5e-5)

    def test_non_numbers_and_bounds_still_raise(self) -> None:
        with self.assertRaisesRegex(perf_ci.PerfCIError, "is not a number"):
            self._with_env("PERF_REGRESSION_RATIO", "fast", perf_ci._ratio_limit)
        with self.assertRaisesRegex(perf_ci.PerfCIError, "greater than 1"):
            self._with_env("PERF_REGRESSION_RATIO", "1", perf_ci._ratio_limit)
        with self.assertRaisesRegex(perf_ci.PerfCIError, ">= 0"):
            self._with_env("PERF_ABSOLUTE_FLOOR_S", "-0.001", perf_ci._absolute_floor)

    def test_ratio_rejects_non_finite(self) -> None:
        for raw in self.NON_FINITE:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    perf_ci.PerfCIError, r"^PERF_REGRESSION_RATIO=.* must be a finite number$",
                ):
                    self._with_env("PERF_REGRESSION_RATIO", raw, perf_ci._ratio_limit)

    def test_floor_rejects_non_finite(self) -> None:
        for raw in self.NON_FINITE:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    perf_ci.PerfCIError, r"^PERF_ABSOLUTE_FLOOR_S=.* must be a finite number$",
                ):
                    self._with_env("PERF_ABSOLUTE_FLOOR_S", raw, perf_ci._absolute_floor)


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


class SweepRegressionTests(unittest.TestCase):
    def test_f01_even_samples_use_the_statistical_median(self):
        base = [0.001] * 4 + [0.003] * 4
        head = [0.0025] * 8
        result = perf_compare.compare_reports(
            _report({"sample": perf_bench._positive_median(base)}),
            _report({"sample": perf_bench._positive_median(head)}),
        )
        self.assertEqual(result["rows"][0]["status"], "regression")
        self.assertAlmostEqual(perf_bench._positive_median(base), 0.002)

    def test_f02_nonfinite_durations_are_refused_before_work(self):
        for option in ("round_s", "warmup_s"):
            for value in (float("inf"), float("-inf"), float("nan")):
                for paired in (False, True):
                    with self.subTest(option=option, value=value, paired=paired):
                        fn = mock.Mock(side_effect=AssertionError("callback ran before refusal"))
                        options = dict(rounds=2, round_s=0.001, warmup_s=0.0)
                        options[option] = value
                        with self.assertRaises(perf_bench.BenchError):
                            if paired:
                                perf_bench.measure_pair(fn, fn, repeats=1, **options)
                            else:
                                perf_bench.measure(fn, **options)
                        fn.assert_not_called()

    def test_f02_cli_rejects_infinite_warmup_before_suite(self):
        import contextlib
        import io
        with (mock.patch.object(perf_bench, "run_suite", return_value={"benchmarks": []}) as suite,
              contextlib.redirect_stdout(io.StringIO())):
            try:
                code = perf_bench.main(["--warmup-ms", "inf"])
            except SystemExit as exc:
                code = exc.code
        self.assertNotEqual(code, 0)
        suite.assert_not_called()

    def test_f03_final_case_cannot_return_success_after_deadline(self):
        clock = [0.0]

        def finish_late(*args, **kwargs):
            clock[0] += 2.0
            return {"median_s": 1.0}, {"median_s": 1.0}

        with (mock.patch.object(perf_bench, "install_network_guard"),
              mock.patch.object(perf_bench, "load_workload", return_value={}),
              mock.patch.object(perf_bench, "load_product", return_value=object()),
              mock.patch.object(perf_bench, "build_cases", side_effect=lambda *a: [("last", lambda: None)]),
              mock.patch.object(perf_bench, "measure_pair", side_effect=finish_late),
              mock.patch.object(perf_bench.time, "perf_counter", side_effect=lambda: clock[0])):
            with self.assertRaises(perf_bench.BenchError):
                perf_bench.run_paired(ROOT, ROOT, deadline_s=1.0)

    def test_f07_incomplete_root_does_not_reuse_previous_tree(self):
        saved_path = sys.path[:]
        saved_modules = {
            key: value for key, value in sys.modules.items()
            if key == "scripts" or key.startswith("scripts.")
        }
        try:
            perf_bench.load_product(ROOT)
            with tempfile.TemporaryDirectory() as tmp:
                candidate = Path(tmp)
                canonical = candidate / "scripts/v3/planning/validation/canonical.py"
                canonical.parent.mkdir(parents=True)
                flag = candidate / "candidate-ran"
                canonical.write_text(
                    f"open({str(flag)!r}, 'w', encoding='utf-8').write('ran')\n",
                    encoding="utf-8",
                )
                before = sys.path[:]
                with self.assertRaises(perf_bench.BenchError) as caught:
                    perf_bench.load_product(candidate)
                self.assertIn("no scripts package", str(caught.exception))
                self.assertEqual(sys.path, before)
                self.assertFalse(flag.exists())
        finally:
            perf_bench._drop_imported_product()
            sys.modules.update(saved_modules)
            sys.path[:] = saved_path

    def test_f08_worktree_removal_failure_is_reported(self):
        failed = subprocess.CompletedProcess([], 128, "", "fatal: worktree is locked")
        with mock.patch.object(perf_ci, "_run", return_value=failed):
            with self.assertRaises(perf_ci.PerfCIError):
                perf_ci._remove_worktree(Path("/unused/synthetic-worktree"))

    def test_f01_odd_count_uses_the_middle_sample(self):
        self.assertEqual(perf_bench._positive_median([0.2, 0.1, 0.4]), 0.2)

    def test_sign_and_type_of_durations_are_refused_before_work(self):
        cases = (
            ("round_s", -1),
            ("round_s", 0),
            ("round_s", -0.0),
            ("warmup_s", -1),
            ("round_s", True),
            ("round_s", False),
            ("round_s", None),
            ("round_s", "0.01"),
            ("warmup_s", True),
            ("warmup_s", False),
            ("warmup_s", None),
            ("warmup_s", "0.01"),
            ("warmup_s", 1e18),
            ("round_s", 1e308),
        )
        for option, value in cases:
            for paired in (False, True):
                with self.subTest(option=option, value=value, paired=paired):
                    fn = mock.Mock(side_effect=AssertionError("callback ran before refusal"))
                    options = dict(rounds=2, round_s=0.001, warmup_s=0.0)
                    options[option] = value
                    with self.assertRaises(perf_bench.BenchError):
                        if paired:
                            perf_bench.measure_pair(fn, fn, repeats=1, **options)
                        else:
                            perf_bench.measure(fn, **options)
                    fn.assert_not_called()

    def test_rounds_must_be_an_integer_count(self):
        fn = mock.Mock(side_effect=AssertionError("callback ran before refusal"))
        for rounds in (2.0, "2", True):
            with self.subTest(rounds=rounds):
                with self.assertRaises(perf_bench.BenchError):
                    perf_bench.measure(fn, rounds=rounds, round_s=0.001, warmup_s=0.0)
                fn.assert_not_called()
                with self.assertRaises(perf_bench.BenchError):
                    perf_bench.measure_pair(fn, fn, rounds=rounds, round_s=0.001, warmup_s=0.0, repeats=1)
                fn.assert_not_called()

    def test_nan_or_infinite_sample_is_refused(self):
        for samples in ([float("nan")], [float("inf")], [0.001, float("nan"), 0.003]):
            with self.subTest(samples=samples):
                with self.assertRaises(perf_bench.BenchError):
                    perf_bench._positive_median(samples)

    def test_cli_duration_gate_covers_round_and_warmup(self):
        import contextlib
        import io
        refused = (
            ["--round-ms", "inf"],
            ["--round-ms", "nan"],
            ["--round-ms", "0"],
            ["--round-ms", "-5"],
            ["--warmup-ms", "nan"],
            ["--warmup-ms", "-1"],
        )
        for argv in refused:
            with self.subTest(argv=argv):
                with (mock.patch.object(perf_bench, "run_suite", return_value={"benchmarks": []}) as suite,
                      contextlib.redirect_stdout(io.StringIO())):
                    try:
                        code = perf_bench.main(argv)
                    except SystemExit as exc:
                        code = exc.code
                self.assertNotEqual(code, 0)
                suite.assert_not_called()
        with (mock.patch.object(perf_bench, "run_suite", return_value={"benchmarks": []}) as suite,
              contextlib.redirect_stdout(io.StringIO())):
            code = perf_bench.main(["--warmup-ms", "0"])
        self.assertEqual(code, 0)
        suite.assert_called_once()

    def test_deadline_must_be_a_finite_positive_duration(self):
        with (mock.patch.object(perf_bench, "install_network_guard"),
              mock.patch.object(perf_bench, "load_workload", return_value={}),
              mock.patch.object(perf_bench, "load_product", return_value=object()),
              mock.patch.object(perf_bench, "build_cases", return_value=[]),
              mock.patch.object(perf_bench.time, "perf_counter", return_value=0.0)):
            for value in (float("nan"), float("inf"), -1, 0, None, "10", 1e18):
                with self.subTest(value=value):
                    with self.assertRaises(perf_bench.BenchError) as caught:
                        perf_bench.run_paired(ROOT, ROOT, deadline_s=value)
                    self.assertIn("deadline_s", str(caught.exception))
                    self.assertNotIn("exceeded", str(caught.exception))

    def test_kernel_outside_the_root_is_refused(self):
        saved_path = sys.path[:]
        saved_modules = {
            key: value for key, value in sys.modules.items()
            if key == "scripts" or key.startswith("scripts.")
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                candidate = Path(tmp) / "tree"
                candidate.mkdir()
                (candidate / "scripts").symlink_to(ROOT / "scripts")
                with self.assertRaises(perf_bench.BenchError) as caught:
                    perf_bench.load_product(candidate)
                message = str(caught.exception)
                self.assertIn("planning kernel loaded from", message)
                self.assertIn(str(ROOT / "scripts"), message)
                self.assertNotIn(str(candidate), message.split("loaded from", 1)[-1].split(", not", 1)[0])
        finally:
            perf_bench._drop_imported_product()
            sys.modules.update(saved_modules)
            sys.path[:] = saved_path

    def test_main_returns_nonzero_when_worktree_remove_fails(self):
        import contextlib
        import io
        tree = Path("/unused/synthetic-worktree")
        locked = subprocess.CompletedProcess([], 128, "", "fatal: worktree is locked")
        ok = subprocess.CompletedProcess([], 0, "", "")

        def fake_run(cmd, **kwargs):
            if "remove" in cmd:
                return locked
            return ok

        report = _report({"sample": 0.001})
        report["benchmarks"][0]["rounds"] = 1
        report["benchmarks"][0]["iterations_per_round"] = 1
        stderr = io.StringIO()
        with (mock.patch.dict(os.environ, {"CI_BASE_SHA": "abcdef1234567890"}, clear=False),
              mock.patch.object(perf_ci, "_worktree", return_value=tree),
              mock.patch.object(perf_ci, "run_paired", return_value=(report, report)),
              mock.patch.object(perf_ci, "_run", side_effect=fake_run),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(stderr)):
            code = perf_ci.main()
        self.assertEqual(code, 1)
        self.assertIn(
            "PERF_CI_FAIL: git worktree remove failed: fatal: worktree is locked",
            stderr.getvalue(),
        )
        self.assertNotIn("Traceback", stderr.getvalue())

        stderr = io.StringIO()
        with (mock.patch.dict(os.environ, {"CI_BASE_SHA": "abcdef1234567890"}, clear=False),
              mock.patch.object(perf_ci, "_worktree", return_value=tree),
              mock.patch.object(perf_ci, "run_paired", side_effect=perf_bench.BenchError("paired benchmark exceeded 40s")),
              mock.patch.object(perf_ci, "_run", return_value=subprocess.CompletedProcess([], 1, "", "")),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(stderr)):
            code = perf_ci.main()
        text = stderr.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("PERF_CI_FAIL: paired benchmark exceeded 40s", text)
        self.assertIn("PERF_CI_FAIL: git worktree remove failed: exit 1", text)

        stderr = io.StringIO()
        with (mock.patch.dict(os.environ, {"CI_BASE_SHA": "abcdef1234567890"}, clear=False),
              mock.patch.object(perf_ci, "_worktree", return_value=tree),
              mock.patch.object(perf_ci, "run_paired", return_value=(report, report)),
              mock.patch.object(perf_ci, "_run", return_value=ok),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(stderr)):
            code = perf_ci.main()
        self.assertEqual(code, 0, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
