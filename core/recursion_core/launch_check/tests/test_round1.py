"""Lane LAUNCH-CHECK, #424 round 1: the witnesses of the three reads at 9cd220c3 (Codex astra, Codex sol,
DeepSeek), each kept as a test that fails at 9cd220c3 and passes after the answering commit.

Every binary is a fake built in a temp dir (a copy of a system binary under an engine's name, or a
``/bin/sh`` script for the probe); no real engine, network or credential is used.

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest recursion_core.launch_check.tests.test_round1 -v``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import unittest
from unittest import mock

from recursion_core.launch_check import (
    FLOORS,
    REFUSAL_CODES,
    LaunchRefused,
    ProbeResult,
    detect,
    grant,
    launch_check,
    launch_plan,
    parse_version,
    run_version_probe,
    verify,
)
from recursion_core.launch_check import probe as probe_mod
from recursion_core.launch_check.floors import check_floors
from recursion_core.launch_check.tests.fixtures import Factory

F: Factory
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setUpModule() -> None:
    global F
    F = Factory()


def tearDownModule() -> None:
    F.cleanup()


def script(text: str) -> str:
    d = F.dir()
    path = os.path.join(d, "claude")
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


class Base(unittest.TestCase):
    def refused(self, code: str, fn, *a, **kw) -> LaunchRefused:
        with self.assertRaises(LaunchRefused) as cm:
            fn(*a, **kw)
        self.assertEqual(code, cm.exception.code, cm.exception)
        self.assertIn(cm.exception.code, REFUSAL_CODES)
        return cm.exception

    @staticmethod
    def line(raw: str):
        return lambda spec, realpath, timeout: ProbeResult(raw, 0, 0)

    def claude_grant(self):
        return grant("claude_code", F.make("claude", "2.1.318 (Claude Code)"), probe=self.line("2.1.318 (Claude Code)"))


# -- DeepSeek F1 (HIGH): grok was granted to any native binary named grok -------------------------------------

class GrokIdentityTests(Base):
    """Grok's real version line was never read and it has no pin, so nothing ties a file named ``grok`` to
    Grok. It is ``identity_unverified`` before anything runs, until a recorded line or a pin exists (which
    of the two, if either, stays Lucas's open question)."""

    def test_a_copy_of_another_program_named_grok_is_not_granted(self) -> None:
        ran = []

        def probe(spec, realpath, timeout):
            ran.append(realpath)
            return ProbeResult("Python 3.12.3", 0, 0)

        for line in ("Python 3.12.3", "git version 2.43.0", "grok 1.0.0"):
            with self.subTest(line=line):
                self.refused("identity_unverified", grant, "grok", F.make("grok", line), probe=probe)
        self.assertEqual([], ran)

    @unittest.skipUnless(os.name == "posix", "POSIX fixtures")
    def test_the_whole_pass_records_grok_refused(self) -> None:
        d = F.dir()
        F.make("grok", "grok 1.0.0", where=d)
        grants, records = launch_check(d, probe=self.line("grok 1.0.0"))
        self.assertNotIn("grok", grants)
        rec = next(r for r in records if r["engine"] == "grok")
        self.assertEqual(("refused", "identity_unverified"), (rec["outcome"], rec["refusal"]))


# -- astra R3, sol R1: a clock time must not evade a date-only floor's commit ----------------------------------

class DateFloorTimeTests(Base):
    def test_a_build_later_on_the_floor_day_with_another_commit_is_refused(self) -> None:
        spec = FLOORS["cursor"]  # security floor 2025.09.17-25b418f: a day and a commit, no time
        for line in ("2025.09.17-00-00-01-aaaaaa", "2025.09.17-23-59-59-aaaaaa", "2025.09.17-12-00-00"):
            with self.subTest(line=line):
                self.refused("version_refused", check_floors, spec, parse_version("date", line))

    def test_the_floor_commit_at_any_time_of_its_day_passes(self) -> None:
        spec = FLOORS["cursor"]
        for line in ("2025.09.17-12-00-00-25b418f", "2025.09.18-00-00-00-aaaaaa"):
            with self.subTest(line=line):
                check_floors(spec, parse_version("date", line))


# -- astra R2, sol R3: an out-of-range timeout, and text that cannot be encoded ---------------------------------

class RangeAndEncodingTests(Base):
    def test_a_timeout_too_large_to_be_a_deadline_is_a_typed_refusal(self) -> None:
        path = F.make("claude", "2.1.318 (Claude Code)")
        for bad in (10 ** 400, 10 ** 30, probe_mod.MAX_PROBE_TIMEOUT_S + 1):
            with self.subTest(timeout=bad):
                self.refused("version_probe_failed", grant, "claude_code", path, timeout=bad,
                             probe=self.line("2.1.318 (Claude Code)"))
                self.refused("version_probe_failed", run_version_probe, FLOORS["claude_code"], path, bad)
        grant("claude_code", path, timeout=probe_mod.MAX_PROBE_TIMEOUT_S, probe=self.line("2.1.318 (Claude Code)"))

    @unittest.skipUnless(os.name == "posix", "POSIX fixtures")
    def test_a_huge_timeout_keeps_one_record_per_engine(self) -> None:
        d = F.dir()
        F.make("claude", "2.1.318 (Claude Code)", where=d)
        grants, records = launch_check(d, timeout=10 ** 400, probe=self.line("2.1.318 (Claude Code)"))
        self.assertEqual(set(FLOORS), {r["engine"] for r in records})
        rec = next(r for r in records if r["engine"] == "claude_code")
        self.assertEqual(("refused", "version_probe_failed"), (rec["outcome"], rec["refusal"]))

    def test_text_that_cannot_be_encoded_is_spawn_too_large(self) -> None:
        g = self.claude_grant()
        self.refused("spawn_too_large", launch_plan, g, ["claude", "\ud800"])
        self.refused("spawn_too_large", launch_plan, g, ["claude"], {"K": "\ud800"})
        self.refused("spawn_too_large", launch_plan, g, ["claude"], {"\ud800": "v"})

    def test_a_probe_result_subclass_is_refused(self) -> None:
        class Sub(ProbeResult):
            pass

        path = F.make("claude", "2.1.318 (Claude Code)")
        self.refused("version_probe_failed", grant, "claude_code", path,
                     probe=lambda *a: Sub("2.1.318 (Claude Code)", 0, 0))


# -- astra R1, DeepSeek F2: the probe child is released on every exceptional exit -------------------------------

@unittest.skipUnless(os.name == "posix", "POSIX processes")
class ProbeCleanupTests(Base):
    def run_probe(self, *patches):
        """Run the real probe on a script that sleeps, with ``patches`` applied; return (exception, Popen)."""
        hang = script("#!/bin/sh\nexec sleep 30\n")
        seen = []
        real = subprocess.Popen

        def capture(*a, **kw):
            p = real(*a, **kw)
            seen.append(p)
            return p

        with mock.patch.object(probe_mod.subprocess, "Popen", side_effect=capture):
            for p in patches:
                p.start()
            try:
                with self.assertRaises(BaseException) as cm:
                    run_version_probe(FLOORS["claude_code"], hang, 10.0)
            finally:
                for p in reversed(patches):
                    p.stop()
        self.assertEqual(1, len(seen))
        return cm.exception, seen[0]

    def assert_released(self, proc) -> None:
        self.assertIsNotNone(proc.returncode, "the probe child was left running")
        with self.assertRaises(ProcessLookupError):
            os.killpg(proc.pid, 0)
        for stream in (proc.stdout, proc.stderr):
            self.assertTrue(stream.closed)

    def test_an_interrupt_while_waiting_releases_the_child(self) -> None:
        exc, proc = self.run_probe(mock.patch.object(probe_mod.threading.Event, "wait", side_effect=KeyboardInterrupt))
        self.assertIsInstance(exc, KeyboardInterrupt)  # the initiating exception is kept
        self.assert_released(proc)

    def test_a_poll_error_releases_the_child(self) -> None:
        exc, proc = self.run_probe(mock.patch.object(probe_mod.subprocess.Popen, "poll", side_effect=OSError("poll")))
        self.assertIsInstance(exc, OSError)
        self.assert_released(proc)

    def test_a_reader_that_cannot_start_releases_the_child(self) -> None:
        exc, proc = self.run_probe(mock.patch.object(probe_mod._CappedReader, "start", side_effect=RuntimeError("thread")))
        self.assertIsInstance(exc, RuntimeError)
        self.assert_released(proc)


# -- sol R2: control characters in real output are judged before any stripping ----------------------------------

@unittest.skipUnless(os.name == "posix", "POSIX processes")
class RealOutputControlTests(Base):
    def raw(self, printf: str) -> str:
        return run_version_probe(FLOORS["claude_code"], script(f"#!/bin/sh\nprintf '{printf}'\n"), 5.0).raw

    def test_control_characters_reach_the_parse_and_are_refused(self) -> None:
        for printf in ("\\t2.1.318 (Claude Code)\\n", "2.1.318 (Claude Code)\\t\\n", "2.1.318\\v(Claude Code)\\n",
                       "2.1.318 (Claude Code)\\rX\\n", "\\033[1m2.1.318 (Claude Code)\\n"):
            with self.subTest(printf=printf):
                self.assertIsNone(parse_version("semver", self.raw(printf)))

    def test_lf_and_crlf_framing_and_spaces_are_kept_as_framing(self) -> None:
        for printf in ("2.1.318 (Claude Code)\\n", "2.1.318 (Claude Code)\\r\\n", "\\n  2.1.318 (Claude Code)  \\n"):
            with self.subTest(printf=printf):
                self.assertEqual("2.1.318 (Claude Code)", self.raw(printf))


# -- DeepSeek F3: a failure at the public surface is a LaunchRefused --------------------------------------------

class PublicSurfaceTests(Base):
    def test_a_path_string_that_is_not_text_is_refused(self) -> None:
        for bad in (None, b"/usr/bin", 5, ["/usr/bin"]):
            with self.subTest(path_string=type(bad).__name__):
                self.refused("path_not_absolute", detect, "claude_code", bad)
                self.refused("path_not_absolute", launch_check, bad)

    def test_verify_refuses_what_grant_did_not_issue(self) -> None:
        g = self.claude_grant()
        for bad in (object(), None, launch_plan(g, ["claude"])):
            with self.subTest(arg=type(bad).__name__):
                self.refused("grant_forged", verify, bad)

    def test_a_forged_field_that_is_not_json_is_grant_forged(self) -> None:
        g = self.claude_grant()
        for forged in (dataclasses.replace(g, entry=(b"x",)), dataclasses.replace(g, size={1, 2}),
                       dataclasses.replace(g, version_raw=float("nan"))):
            with self.subTest(forged=forged.size if isinstance(forged.size, set) else forged.entry):
                self.refused("grant_forged", launch_plan, forged, ["claude"])
                self.refused("grant_forged", verify, forged)


# -- DeepSeek F4: every record has the same keys -----------------------------------------------------------------

class RecordShapeTests(Base):
    @unittest.skipUnless(os.name == "posix", "POSIX fixtures")
    def test_absent_records_carry_hits_and_detection(self) -> None:
        d = F.dir()
        F.make("claude", "2.1.318 (Claude Code)", where=d)
        _grants, records = launch_check(d, probe=self.line("2.1.318 (Claude Code)"))
        absent = [r for r in records if r["outcome"] == "absent"]
        self.assertTrue(absent)
        for rec in absent:
            self.assertEqual([], rec["hits"])
            self.assertEqual("absent from PATH", rec["detection"])
        self.assertEqual(1, len({frozenset(k for k in r if k in ("hits", "detection")) for r in records}))


# -- the documents: sol R6 (Gemini's absence), astra R5 and sol R5 (the parent refusal's wire form) -------------

class DocumentTests(unittest.TestCase):
    def read(self, name: str) -> str:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return f.read()

    def test_gemini_absence_is_held_pending_d10_not_called_an_owner_decision(self) -> None:
        for name in ("README.md", "floors.py"):
            with self.subTest(file=name):
                text = self.read(name)
                self.assertNotRegex(text, r"Lucas dropped the Gemini|owner call of 2026-11-09: (drop|the row is)")
                self.assertIn("D10", text)

    def test_the_documented_parent_refusal_fits_the_start_error_code_pattern(self) -> None:
        text = self.read("DECODER.md")
        examples = re.findall(r"`(\{\"code\": [^`]+\})`", text)
        self.assertTrue(examples)
        for example in examples:
            err = json.loads(example)
            self.assertRegex(err["code"], r"^[a-z][a-z0-9_]{0,63}$")
        self.assertIn('{"code": "launch_refused", "detail": "launch_check:binary_changed"}', text)
        self.assertNotIn("`launch_check:<code>`, as `start_failed` with `start_error.code`", text)

    def test_the_decoder_design_does_not_call_the_exec_line_a_proof(self) -> None:
        text = self.read("DECODER.md")
        self.assertNotIn('"exec" then end of file means the engine was executed', text)
        self.assertIn("intent", text)


if __name__ == "__main__":
    unittest.main()
