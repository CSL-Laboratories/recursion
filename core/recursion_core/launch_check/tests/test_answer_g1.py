"""Lane LAUNCH-CHECK, #424 answer: non-ASCII digits, a falsy env, and two bounds.

A decimal digit outside ASCII 0-9 must leave the version unparsed. ``launch_plan``
refuses a falsy value that is not a mapping. The Windows command-line bound is the
exact unit count, and ``_SELECT_READS`` follows the host.

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest recursion_core.launch_check.tests.test_answer_g1``.
"""

from __future__ import annotations

import os
import subprocess
import threading
import unittest
from collections.abc import Mapping
from unittest import mock

from recursion_core.launch_check import (
    REFUSAL_CODES,
    LaunchRefused,
    ProbeResult,
    grant,
    launch_plan,
    parse_version,
)
from recursion_core.launch_check import binary as binary_mod
from recursion_core.launch_check import probe as probe_mod
from recursion_core.launch_check.grant import MAX_WINDOWS_COMMAND_LINE
from recursion_core.launch_check.tests.fixtures import Factory

F: Factory


def setUpModule() -> None:
    global F
    F = Factory()


def tearDownModule() -> None:
    F.cleanup()


def _units(vector: list[str]) -> int:
    return len(subprocess.list2cmdline(vector).encode("utf-16-le", "surrogatepass")) // 2 + 1


class AsciiDigitTests(unittest.TestCase):
    def probe(self, raw: str):
        return lambda spec, realpath, timeout: ProbeResult(raw, 0, 0)

    def refused(self, engine: str, name: str, raw: str) -> LaunchRefused:
        with self.assertRaises(LaunchRefused) as cm:
            grant(engine, F.make(name, raw), probe=self.probe(raw))
        self.assertEqual("version_unparsed", cm.exception.code, cm.exception)
        self.assertIn(cm.exception.code, REFUSAL_CODES)
        self.assertIn(raw[:40], cm.exception.detail)
        return cm.exception

    def test_a_non_ascii_digit_is_unparsed(self) -> None:
        cases = (
            ("semver", "2.1.259\u0660 (Claude Code)"),
            ("semver", "1\u0660.0.0 (Claude Code)"),
            ("semver", "2.1.313\u0660 (Claude Code)"),
            ("semver", "codex-cli 0.130\u0661.0"),
            ("date", "2025.09.1\u0667-25b418f"),
        )
        for scheme, raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_version(scheme, raw))
        self.refused("claude_code", "claude", "2.1.259\u0660 (Claude Code)")
        self.refused("claude_code", "claude", "2.1.313\u0660 (Claude Code)")
        self.refused("codex", "codex", "codex-cli 0.130\u0661.0")
        # A second caller still grants an ASCII line after those refusals.
        raw = "2.1.318 (Claude Code)"
        parsed = parse_version("semver", raw)
        self.assertIsNotNone(parsed)
        self.assertEqual("2.1.318", parsed.text)
        granted = grant("claude_code", F.make("claude", raw), probe=self.probe(raw))
        self.assertEqual("2.1.318", granted.version)
        self.assertEqual(raw, granted.version_raw)

    def test_ascii_lines_still_parse(self) -> None:
        self.assertEqual("2.1.318", parse_version("semver", "2.1.318 (Claude Code)").text)
        self.assertEqual((0, 131, 0), parse_version("semver", "codex-cli 0.131.0").key)
        self.assertEqual("2025.9.17", parse_version("date", "2025.09.17-25b418f").text)

    def test_a_second_caller_parses_both_shapes(self) -> None:
        errors: list[BaseException] = []

        def work() -> None:
            try:
                for _ in range(40):
                    if parse_version("semver", "2.1.259\u0660 (Claude Code)") is not None:
                        raise AssertionError("folded")
                    if parse_version("semver", "2.1.318 (Claude Code)").text != "2.1.318":
                        raise AssertionError("ascii")
            except BaseException as exc:  # noqa: BLE001 - recorded for the join
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


class FalsyEnvTests(unittest.TestCase):
    def grant_claude(self):
        raw = "2.1.318 (Claude Code)"
        return grant(
            "claude_code",
            F.make("claude", raw),
            probe=lambda spec, realpath, timeout: ProbeResult(raw, 0, 0),
        )

    def test_a_falsy_non_mapping_is_refused(self) -> None:
        g = self.grant_claude()
        for env in (0, False, "", [], (), b"", 0.0):
            with self.subTest(env=env):
                with self.assertRaises(LaunchRefused) as cm:
                    launch_plan(g, ["claude"], env)
                self.assertEqual("spawn_too_large", cm.exception.code)
                self.assertIn("not a mapping", cm.exception.detail)
        # None and an empty mapping are still an empty environment. A second call agrees.
        self.assertEqual({}, launch_plan(g, ["claude"]).exec_env())
        self.assertEqual({}, launch_plan(g, ["claude"], None).exec_env())
        self.assertEqual({"HOME": "/tmp/h"}, launch_plan(g, ["claude"], {"HOME": "/tmp/h"}).exec_env())
        self.assertEqual({}, launch_plan(g, ["claude"], {}).exec_env())

    def test_an_interruption_during_the_copy_propagates(self) -> None:
        g = self.grant_claude()

        class InterruptMap(Mapping):
            def __iter__(self):
                raise KeyboardInterrupt

            def __len__(self) -> int:
                return 0

            def __getitem__(self, key: str) -> str:
                raise KeyError(key)

        with self.assertRaises(KeyboardInterrupt):
            launch_plan(g, ["claude"], InterruptMap())
        # The refusal path did not stick: a later caller still gets a plan.
        self.assertEqual({}, launch_plan(g, ["claude"], None).exec_env())


class BoundTests(unittest.TestCase):
    def test_select_reads_follows_the_host(self) -> None:
        self.assertIs(probe_mod._SELECT_READS, os.name != "nt")

    def test_windows_command_line_bound_is_exact(self) -> None:
        raw = "2.1.318 (Claude Code)"
        g = grant(
            "claude_code",
            F.make("claude", raw),
            probe=lambda spec, realpath, timeout: ProbeResult(raw, 0, 0),
        )
        self.assertEqual(32767, MAX_WINDOWS_COMMAND_LINE)

        def units(n: int) -> int:
            return _units([g.realpath, "-p", "x" * n])

        n = MAX_WINDOWS_COMMAND_LINE
        n += MAX_WINDOWS_COMMAND_LINE - units(n)
        while units(n) < MAX_WINDOWS_COMMAND_LINE:
            n += 1
        while units(n) > MAX_WINDOWS_COMMAND_LINE:
            n -= 1
        self.assertEqual(MAX_WINDOWS_COMMAND_LINE, units(n))
        self.assertEqual(MAX_WINDOWS_COMMAND_LINE + 1, units(n + 1))
        with mock.patch.object(binary_mod, "is_windows", return_value=True):
            launch_plan(g, ["claude", "-p", "x" * n])
            with self.assertRaises(LaunchRefused) as cm:
                launch_plan(g, ["claude", "-p", "x" * (n + 1)])
        self.assertEqual("spawn_too_large", cm.exception.code)
        self.assertIn(str(MAX_WINDOWS_COMMAND_LINE), cm.exception.detail)


if __name__ == "__main__":
    unittest.main()
