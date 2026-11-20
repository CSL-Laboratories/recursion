#!/usr/bin/env python3
"""Tests for the offline host start-time and memory measurement tool."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("host_measure", ROOT / "tools" / "host_measure.py")
host_measure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host_measure)

ROW_NAMES = {"host_start_ms", "host_idle_rss_kib", "host_peak_rss_kib"}
ROW_KEYS = {"name", "status", "detail"}


class HostMeasureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def invoke_with_reading(self, reading: int) -> tuple[int, list[dict]]:
        """Run the output path with deterministic, injected measurements."""
        fake_run = (1.0, reading, reading, None)
        output = io.StringIO()
        with mock.patch.object(host_measure, "measure_once", return_value=fake_run):
            with contextlib.redirect_stdout(output):
                code = host_measure.main(["--runs", "1", "--idle-s", "0"])
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        return code, rows

    def test_json_shape_and_zero_exit_for_small_and_big_readings(self) -> None:
        for reading in (1, 10**12):
            with self.subTest(reading=reading):
                code, rows = self.invoke_with_reading(reading)
                self.assertEqual(code, 0)
                self.assertEqual({row["name"] for row in rows}, ROW_NAMES)
                self.assertEqual(len(rows), len(ROW_NAMES))
                for row in rows:
                    self.assertEqual(set(row), ROW_KEYS)
                    self.assertIn(row["status"], {"ok", "unavailable"})
                    self.assertIn(sys.executable, row["detail"])
                    self.assertIn("-I", row["detail"])
                    self.assertIn(host_measure.platform.release(), row["detail"])
                    self.assertIn("measured, one host, not a budget", row["detail"])

    def test_child_without_ready_line_is_unavailable_and_ended(self) -> None:
        command = [
            sys.executable, "-I", "-X", "utf8", "-B", "-c",
            "import time; time.sleep(60)",
        ]
        children = []
        original_start = host_measure.start_host

        def remember_child(cmd: list[str]):
            child = original_start(cmd)
            children.append(child)
            return child

        def clean_children() -> None:
            for child in children:
                if child.poll() is None:
                    host_measure.stop_host(child)

        self.addCleanup(clean_children)
        with mock.patch.object(host_measure, "start_host", side_effect=remember_child):
            results = host_measure.measure_runs(
                command, runs=1, idle_s=0, proc_root=self.tmp, ready_timeout_s=0.1,
            )
        rows = host_measure.result_rows(results, command)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertTrue(all(row["status"] == "unavailable" for row in rows))
        self.assertTrue(all("no ready line within" in row["detail"] for row in rows))

    def test_missing_status_file_makes_only_memory_rows_unavailable(self) -> None:
        command = host_measure.builtin_command()
        results = host_measure.measure_runs(
            command, runs=1, idle_s=0, proc_root=self.tmp, ready_timeout_s=5,
        )
        rows = {row["name"]: row for row in host_measure.result_rows(results, command)}
        self.assertEqual(rows["host_start_ms"]["status"], "ok")
        for name in ("host_idle_rss_kib", "host_peak_rss_kib"):
            self.assertEqual(rows[name]["status"], "unavailable")
            self.assertIn("status file", rows[name]["detail"])
            self.assertIn("measured, one host, not a budget", rows[name]["detail"])

    def test_fake_status_file_supplies_rss_and_peak(self) -> None:
        pid = 4242
        status = self.tmp / str(pid) / "status"
        status.parent.mkdir()
        rss, peak = 123, 456
        status.write_text(
            f"Name:\tfake\nVmRSS:\t{rss} kB\nVmHWM:\t{peak} kB\n",
            encoding="utf-8",
        )
        self.assertEqual(host_measure.read_memory(pid, self.tmp), (rss, peak))

    @unittest.skipUnless(Path("/proc").is_dir(), "requires a real /proc filesystem")
    def test_real_proc_reports_memory_when_available(self) -> None:
        command = host_measure.builtin_command()
        results = host_measure.measure_runs(command, runs=1, idle_s=0, ready_timeout_s=5)
        rows = {row["name"]: row for row in host_measure.result_rows(results, command)}
        self.assertEqual(rows["host_idle_rss_kib"]["status"], "ok")
        self.assertEqual(rows["host_peak_rss_kib"]["status"], "ok")


class FollowUpTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("sh"), "requires a POSIX shell")
    def test_bound_holds_when_a_grandchild_keeps_the_pipe_open(self) -> None:
        command = ["sh", "-c", "sleep 6 & sleep 6"]
        began = time.monotonic()
        results = host_measure.measure_runs(command, runs=1, idle_s=0, ready_timeout_s=1)
        elapsed = time.monotonic() - began
        rows = host_measure.result_rows(results, command)
        self.assertLess(elapsed, 3)
        self.assertTrue(all(row["status"] == "unavailable" for row in rows))

    def test_the_bound_number_is_the_one_given(self) -> None:
        command = [sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"]
        results = host_measure.measure_runs(command, runs=1, idle_s=0, ready_timeout_s=0.25)
        rows = host_measure.result_rows(results, command)
        self.assertTrue(all("within 0.25 seconds" in row["detail"] for row in rows))

    def test_median_not_maximum_and_partial_success_keeps_the_numbers(self) -> None:
        results = [(10.0, 100, 150, None), (20.0, 200, 250, None), (1000.0, 900, 950, None)]
        rows = {r["name"]: r for r in host_measure.result_rows(results, ["x"])}
        self.assertIn("median_ms=20.000", rows["host_start_ms"]["detail"])
        self.assertIn("range_ms=10.000..1000.000", rows["host_start_ms"]["detail"])
        self.assertIn("median_kib=200.000", rows["host_idle_rss_kib"]["detail"])
        self.assertIn("median_kib=250.000", rows["host_peak_rss_kib"]["detail"])
        partial = results[:2] + [(None, None, None, "child failed")]
        rows = {r["name"]: r for r in host_measure.result_rows(partial, ["x"])}
        for row in rows.values():
            self.assertEqual(row["status"], "ok")
            self.assertIn("successful_runs=2/3", row["detail"])

    def test_the_double_dash_is_not_part_of_the_command(self) -> None:
        fake = (1.0, 1, 1, None)
        with mock.patch.object(host_measure, "measure_once", return_value=fake) as once:
            with contextlib.redirect_stdout(io.StringIO()):
                code = host_measure.main(["--runs", "1", "--idle-s", "0", "--", "stand-in", "arg"])
        self.assertEqual(code, 0)
        self.assertEqual(once.call_args[0][0], ["stand-in", "arg"])

    @unittest.skipUnless(Path("/proc/self/fd/0").exists(), "requires /proc/self/fd")
    def test_the_child_stdin_is_closed(self) -> None:
        program = (
            "import os, time\n"
            "print('ready' if os.readlink('/proc/self/fd/0') == os.devnull else 'stdin-open', flush=True)\n"
            "time.sleep(60)"
        )
        child = host_measure.start_host([sys.executable, "-I", "-B", "-c", program])
        try:
            line = child.stdout.readline()
        finally:
            host_measure.stop_host(child)
            child.stdout.close()
        self.assertEqual(line.strip(), b"ready")


if __name__ == "__main__":
    unittest.main()
