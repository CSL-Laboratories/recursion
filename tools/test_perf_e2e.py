#!/usr/bin/env python3
"""Checks for the provider-free W1 turn profile. Smoke only; no timing gate."""
from __future__ import annotations

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

PHASES = ("spawn", "first_event", "stream_parse", "validate", "persist", "total")
SAMPLE_KEYS = (
    "spawn_ns",
    "first_event_ns",
    "stream_parse_ns",
    "validate_ns",
    "persist_ns",
)
DIMENSIONS = (
    "authority, safety, privacy, effects",
    "verified quality",
    "recovery and replay",
    "owner/operator cost",
    "quota, tokens and money",
    "context/cache efficiency",
    "latency and throughput",
    "host resources",
    "terminal interaction",
    "portability/configurability",
    "code and maintenance surface",
    "extensibility/idle cost",
)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perf_e2e = load("perf_e2e", HERE / "perf_e2e.py")


class WorkloadTests(unittest.TestCase):
    def test_workload_lists_twelve_dimensions_with_null_reasons(self) -> None:
        workload = perf_e2e.load_workload(ROOT)
        self.assertEqual(workload["schema"], "recursion-perf-e2e-workload/1")
        self.assertEqual([row["name"] for row in workload["dimensions"]], list(DIMENSIONS))
        self.assertEqual(len(workload["dimensions"]), 12)
        for row in workload["dimensions"]:
            self.assertIn("null_reason", row)
            self.assertIn("required_observation", row)
            self.assertIsInstance(row["observable_provider_free"], bool)
            if row["observable_provider_free"]:
                self.assertIsNone(row["null_reason"])
            else:
                self.assertIsInstance(row["null_reason"], str)
                self.assertTrue(row["null_reason"].strip())
        proposed = workload["proposed_by_agent"]
        self.assertTrue(proposed["proposed_by_agent"])
        self.assertEqual(proposed["pilot_cap"]["max_measured_turns"], 30)
        self.assertEqual(proposed["pilot_cap"]["max_seconds_per_fixture"], 20)
        self.assertEqual(workload["pilot"]["rounds"], 5)
        self.assertEqual(workload["pilot"]["Z"], 1.96)
        self.assertEqual(workload["pilot"]["W"], 0.05)
        hosts = {row["id"]: row for row in workload["hosts"]}
        self.assertFalse(hosts["linux-x64"]["measured"])
        self.assertFalse(hosts["windows-11-x64"]["measured"])
        self.assertFalse(hosts["macos-arm"]["measured"])
        self.assertFalse(hosts["macos-arm"]["support_commitment"])
        self.assertIn("not a support commitment", hosts["macos-arm"]["note"])

    def test_fixture_digests_match_lf_normalised_bytes(self) -> None:
        workload = perf_e2e.load_workload(ROOT)
        self.assertEqual(len(workload["fixtures"]), 5)
        for spec in workload["fixtures"]:
            path = ROOT / spec["path"]
            data = path.read_bytes()
            normalised = perf_e2e.lf_normalise(data)
            self.assertEqual(perf_e2e.sha256_lf(data), spec["sha256_lf"])
            self.assertEqual(perf_e2e.require_digest(path, spec["sha256_lf"]), normalised)
            header = normalised.splitlines()[0].decode("utf-8")
            self.assertIn(spec["provider"], header)
            self.assertIn(spec["evidence"], normalised.decode("utf-8"))

    def test_crlf_copy_hashes_to_the_same_digest(self) -> None:
        workload = perf_e2e.load_workload(ROOT)
        for spec in workload["fixtures"]:
            data = (ROOT / spec["path"]).read_bytes()
            normalised = perf_e2e.lf_normalise(data)
            crlf = normalised.replace(b"\n", b"\r\n")
            self.assertNotEqual(normalised, crlf)
            self.assertIn(b"\r\n", crlf)
            self.assertEqual(perf_e2e.sha256_lf(crlf), spec["sha256_lf"])
            with tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / "fixture.txt"
                copy.write_bytes(crlf)
                self.assertEqual(perf_e2e.require_digest(copy, spec["sha256_lf"]), normalised)


class SmokeTests(unittest.TestCase):
    def test_smoke_reports_every_phase_and_phase_sum_within_total(self) -> None:
        workload = perf_e2e.load_workload(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "e2e.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    "-B",
                    str(HERE / "perf_e2e.py"),
                    "--smoke",
                    "--out",
                    str(out),
                ],
                cwd=ROOT,
                check=False,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["schema"], "recursion-perf-e2e/1")
        self.assertEqual(report["provider_calls"], 0)
        self.assertFalse(report["provider_argv_executed"])
        self.assertEqual(report["mode"], "smoke")
        self.assertEqual(report["metadata"]["pilot_outcome"], "smoke")
        self.assertEqual(report["child_flags"], ["-X", "utf8", "-B", "-c"])
        names = [row["name"] for row in report["fixtures"]]
        self.assertEqual(names, [spec["name"] for spec in workload["fixtures"]])
        rows = [report["baseline"], *report["fixtures"]]
        self.assertEqual(report["baseline"]["name"], "trivial_spawn")
        for row in rows:
            self.assertEqual(row["n"], 3)
            self.assertEqual(row["pilot"]["outcome"], "smoke")
            self.assertEqual(tuple(row["phases"]), PHASES)
            self.assertEqual(len(row["samples"]), 3)
            for phase in PHASES:
                stats = row["phases"][phase]
                self.assertEqual(stats["n"], 3)
                self.assertIsNone(stats["p99_ns"])
                self.assertIn("n < 100", stats["p99_null_reason"])
                self.assertGreaterEqual(stats["median_ns"], 0)
            for sample in row["samples"]:
                self.assertLessEqual(
                    sum(sample[key] for key in SAMPLE_KEYS),
                    sample["total_ns"],
                )
        for row in report["fixtures"]:
            overhead = row["overhead"]
            self.assertEqual(
                overhead["harness_overhead_ns"],
                overhead["fixture_total_median_ns"] - overhead["trivial_spawn_median_ns"],
            )
            self.assertEqual(
                overhead["expression"],
                "fixture_total_median_ns - trivial_spawn_median_ns",
            )
            self.assertEqual(
                overhead["trivial_spawn_median_ns"],
                report["baseline"]["phases"]["spawn"]["median_ns"],
            )
            self.assertEqual(
                overhead["trivial_total_median_ns"],
                report["baseline"]["phases"]["total"]["median_ns"],
            )
            self.assertIn("not adapter-only overhead", overhead["note"])
        for row in rows:
            for sample in row["samples"]:
                token = sample["verdict_token"]
                if row["name"] in {"claude-stream-json", "grok-review"}:
                    self.assertEqual(token, "approve")
                    self.assertTrue(sample["verdict_token_exercised"])
                    self.assertEqual(sample["verdict"], "approve")
                else:
                    self.assertIsNone(token)
                    self.assertFalse(sample["verdict_token_exercised"])
                    self.assertEqual(sample["verdict"], "revise")


class VerdictTests(unittest.TestCase):
    def test_present_token_must_match_and_absent_token_is_revise(self) -> None:
        expected = {
            "claude-stream-json": "approve",
            "grok-review": "approve",
            "codex-exec-jsonl": None,
            "codex-cumulative": None,
            "grok-streaming-json": None,
        }
        workload = perf_e2e.load_workload(ROOT)
        seen = set()
        for spec in workload["fixtures"]:
            raw = (ROOT / spec["path"]).read_text(encoding="utf-8")
            parsed = perf_e2e._stream_parse(raw)
            checked = perf_e2e._validate(parsed, perf_e2e.PROMPT)
            self.assertEqual(checked["verdict_token"], expected[spec["name"]])
            self.assertEqual(parsed["verdict"], expected[spec["name"]] or "revise")
            self.assertEqual(checked["verdict_token_exercised"], expected[spec["name"]] is not None)
            seen.add(spec["name"])
        self.assertEqual(seen, set(expected))
        baseline = perf_e2e._stream_parse("ok\n")
        checked = perf_e2e._validate(baseline, perf_e2e.PROMPT)
        self.assertIsNone(checked["verdict_token"])
        self.assertFalse(checked["verdict_token_exercised"])
        self.assertEqual(baseline["verdict"], "revise")


class FailureTests(unittest.TestCase):
    def test_changed_byte_fails_digest(self) -> None:
        spec = perf_e2e.load_workload(ROOT)["fixtures"][0]
        mutated = bytearray((ROOT / spec["path"]).read_bytes())
        mutated[0] ^= 0x01
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "fixture.txt"
            copy.write_bytes(mutated)
            with self.assertRaises(perf_e2e.PerfE2EError) as caught:
                perf_e2e.require_digest(copy, spec["sha256_lf"])
        self.assertIn("digest", str(caught.exception).lower())

    def test_network_guard_is_active(self) -> None:
        self.assertEqual(perf_e2e.install_network_guard.__module__, "perf_bench")
        perf_e2e.install_network_guard()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with self.assertRaises(RuntimeError):
            sock.connect(("127.0.0.1", 9))
        sock.close()

    def test_timing_values_do_not_fail_the_process(self) -> None:
        report = {
            "metadata": {"pilot_outcome": "unstable"},
            "fixtures": [
                {
                    "phases": {
                        "total": {"median_ns": 10**15, "relative_iqr": 50, "p99_ns": 10**15},
                    }
                }
            ],
        }
        self.assertEqual(perf_e2e.exit_code(report), 0)


if __name__ == "__main__":
    unittest.main()
