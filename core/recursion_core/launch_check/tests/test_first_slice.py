"""Lane LAUNCH-CHECK, first slice: the open review findings of #323 and #335 inside launch_check/, the
break-it pass over the package's arguments, and the registrant that registers nothing.

Each test names the finding it answers. Every binary is a fake built in a temp dir; no real engine,
network or credential is used.

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest recursion_core.launch_check.tests.test_first_slice -v``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import unittest
from unittest import mock

from recursion_core.launch_check import (
    FLOORS,
    REFUSAL_CODES,
    LaunchRefused,
    ProbeResult,
    codex_vendored_binary,
    detect,
    grant,
    launch_check,
    launch_plan,
    npm_hermes_agent_root,
    parse_version,
    run_version_probe,
)
from recursion_core.launch_check import binary as binary_mod
from recursion_core.launch_check.detect import _triples
from recursion_core.launch_check.floors import check_floors
from recursion_core.launch_check.tests.fixtures import Factory

F: Factory


def setUpModule() -> None:
    global F
    F = Factory()


def tearDownModule() -> None:
    F.cleanup()


def windows():
    """Simulate Windows through the package's one seam; os.name and os.path stay POSIX."""
    return mock.patch.object(binary_mod, "is_windows", return_value=True)


def write(path: str, text: str, mode: int = 0o755) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


class Base(unittest.TestCase):
    def refused(self, code: str, fn, *a, **kw) -> LaunchRefused:
        with self.assertRaises(LaunchRefused) as cm:
            fn(*a, **kw)
        self.assertEqual(code, cm.exception.code, cm.exception)
        self.assertIn(cm.exception.code, REFUSAL_CODES)
        return cm.exception

    def claude(self) -> str:
        return F.make("claude", "2.1.318 (Claude Code)")

    @staticmethod
    def line(raw: str, code: int = 0):
        def probe(spec, realpath, timeout):
            return ProbeResult(raw, code, 0)
        return probe


# -- #323, Devin r4521280830: a bare-name shim hides a pinned Windows launcher ---------------------------

@unittest.skipUnless(os.name == "posix", "POSIX fixtures; Windows is simulated through binary.is_windows()")
class WindowsSearchOrderTests(Base):
    """Windows resolves a command through PATHEXT and never runs an extensionless file found on PATH.

    npm's cmd-shim writes three files per bin: an extensionless sh script, ``<name>.cmd`` and
    ``<name>.ps1``. The bare name is now searched last on Windows, so the sh shim no longer shadows
    the launcher Windows would run.
    """

    def test_npm_trio_offers_the_cmd_launcher_on_a_pinned_route(self) -> None:
        d = F.dir()
        write(os.path.join(d, "pi"), '#!/bin/sh\nexec node "$basedir/node_modules/pi/cli.js" "$@"\n')
        write(os.path.join(d, "pi.cmd"), "@echo off\r\nnode cli.js %*\r\n")
        write(os.path.join(d, "pi.ps1"), "& node cli.js @args\n")
        with windows():
            found = detect("pi", d)
        self.assertEqual(os.path.join(d, "pi.cmd"), found.candidate)
        self.assertEqual([os.path.join(d, n) for n in ("pi.cmd", "pi.ps1", "pi")], [h.path for h in found.hits])

    def test_bare_name_is_still_found_when_it_is_the_only_file(self) -> None:
        d = F.dir()
        write(os.path.join(d, "pi"), "#!/bin/sh\n")
        with windows():
            self.assertEqual(os.path.join(d, "pi"), detect("pi", d).candidate)

    def test_npm_shim_beside_a_native_exe_offers_the_exe(self) -> None:
        d = F.dir()
        write(os.path.join(d, "claude"), "#!/bin/sh\nexec node cli.js\n")
        native = F.make("claude.exe", "2.1.318 (Claude Code)", where=d)
        with windows():
            found = detect("claude_code", d)
        self.assertEqual(native, found.candidate)

    def test_posix_search_is_unchanged(self) -> None:
        d = F.dir()
        write(os.path.join(d, "pi"), "#!/bin/sh\n")
        write(os.path.join(d, "pi.cmd"), "@echo off\r\n")
        self.assertEqual([os.path.join(d, "pi")], [h.path for h in detect("pi", d).hits])


# -- #323, review of 7c15fa97 (Nov 9, 2:03 PM), point 2: the security floor's commit was not compared ------

class DateFloorCommitTests(Base):
    """Cursor's security floor is ``2025.09.17-25b418f``. The commit does not order, so a build of the
    same day is not known to contain the fix unless it is that commit: it is refused (fail closed)."""

    def test_same_day_build_with_another_commit_is_refused(self) -> None:
        spec = FLOORS["cursor"]
        for line in ("2025.09.17-aaaaaa", "2025.09.17-25b418e", "2025.09.17", "2025.09.17-00-00-00-aaaaaa"):
            with self.subTest(line=line):
                r = self.refused("version_refused", check_floors, spec, parse_version("date", line))
                self.assertIn("25b418f", r.detail)

    def test_the_floor_commit_itself_and_later_days_pass(self) -> None:
        spec = FLOORS["cursor"]
        for line in ("2025.09.17-25b418f", "2025.09.18-aaaaaa", "2025.09.17-00-00-01-25b418f", "2026.11.05-86047df"):
            with self.subTest(line=line):
                check_floors(spec, parse_version("date", line))

    def test_earlier_days_stay_refused(self) -> None:
        self.refused("version_refused", check_floors, FLOORS["cursor"], parse_version("date", "2025.09.16-25b418f"))


# -- #323, review of 7c15fa97 (Nov 9, 2:03 PM), point 3: one Codex install seen from two bases ------------

@unittest.skipUnless(os.name == "posix" and _triples(), "POSIX fixtures with a known vendor triple")
class CodexOneInstallTwoBasesTests(Base):
    def _layout(self) -> tuple[str, str, str, str]:
        prefix = F.dir()
        openai = os.path.join(prefix, "lib", "node_modules", "@openai")
        pkg = os.path.join(openai, "codex")
        js = write(os.path.join(pkg, "bin", "codex.js"), "#!/usr/bin/env node\n")
        plat = os.path.join(openai, "codex-linux-x64")
        vdir = os.path.join(plat, "vendor", _triples()[0], "codex")
        os.makedirs(vdir)
        native = F.make("codex", "codex-cli 0.160.0", where=vdir)
        return js, pkg, plat, native

    def test_the_same_file_seen_twice_is_one_candidate(self) -> None:
        js, pkg, plat, native = self._layout()
        os.makedirs(os.path.join(pkg, "node_modules", "@openai"))
        os.symlink(plat, os.path.join(pkg, "node_modules", "@openai", "codex-linux-x64"))
        got = codex_vendored_binary(js)
        self.assertIsNotNone(got)
        self.assertEqual(os.path.realpath(native), os.path.realpath(got))

    def test_two_different_natives_stay_ambiguous(self) -> None:
        if len(_triples()) < 2:
            self.skipTest("one vendor triple on this platform")
        js, pkg, plat, native = self._layout()
        other = os.path.join(plat, "vendor", _triples()[1], "codex")
        os.makedirs(other)
        F.make("codex", "codex-cli 0.160.0", where=other)
        self.assertIsNone(codex_vendored_binary(js))


# -- #335, reviews of 7b049c62 (Nov 9, 2:01 PM and 2:16 PM): the bridge's package.json ---------------------

@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class BridgePackageJsonTests(Base):
    def _tree(self, text: str) -> tuple[str, str]:
        prefix = F.dir()
        src = os.path.join(prefix, "src", "hermes-agent")
        vendor = os.path.join(src, "vendor")
        os.makedirs(vendor)
        with open(os.path.join(src, "package.json"), "w") as f:
            f.write(text)
        F.make("hermes", "hermes 0.9.1", where=vendor)
        F.make("hermes-acp", "hermes-acp 0.9.1", where=vendor)
        return src, os.path.join(vendor, "hermes")

    def test_a_name_key_given_twice_is_read_both_times(self) -> None:
        src, hermes = self._tree('{"name": "hermes-agent", "name": "not-the-bridge"}')
        self.assertEqual(src, npm_hermes_agent_root(hermes))
        src, hermes = self._tree('{"name": "not-the-bridge", "name": "hermes-agent"}')
        self.assertEqual(src, npm_hermes_agent_root(hermes))

    def test_reviewers_padded_and_fifo_trees_are_not_granted_on_this_head(self) -> None:
        # The reviewer's witness granted hermes at 7b049c62, when hermes was probed. Since #323's merge
        # hermes is a pinned route with no recorded pin, so the same trees end at pin_mismatch: no grant,
        # and nothing is run. A guard for that, not a change: this passes on the base too.
        src, hermes = self._tree('{"name": "hermes-agent", "pad": "%s"}' % ("Z" * 65536))
        self.assertIsNone(npm_hermes_agent_root(hermes))  # still not recognised by the file (see the report)
        ran = []
        self.refused("pin_mismatch", grant, "hermes", hermes, probe=lambda *a: ran.append(a))
        os.remove(os.path.join(src, "package.json"))
        os.mkfifo(os.path.join(src, "package.json"))
        self.refused("pin_mismatch", grant, "hermes", hermes, probe=lambda *a: ran.append(a))
        self.assertEqual([], ran)


# -- break-it: the probe timeout --------------------------------------------------------------------------

class TimeoutTests(Base):
    BAD = (float("nan"), float("inf"), -1.0, 0, 0.0, True, False, "5", None, 1j)

    def test_grant_refuses_a_timeout_that_is_not_a_finite_positive_number(self) -> None:
        path = self.claude()
        called = []

        def probe(spec, realpath, timeout):
            called.append(timeout)
            return ProbeResult("2.1.318 (Claude Code)", 0, 0)

        for bad in self.BAD:
            with self.subTest(timeout=bad):
                self.refused("version_probe_failed", grant, "claude_code", path, timeout=bad, probe=probe)
        self.assertEqual([], called)
        self.assertEqual("2.1.318", grant("claude_code", path, timeout=3, probe=probe).version)

    @unittest.skipUnless(os.name == "posix", "POSIX fixtures")
    def test_the_probe_itself_refuses_a_nan_timeout(self) -> None:
        # A NaN deadline is never reached: before the change a probe that never exits ran for ever.
        path = self.claude()
        for bad in (float("nan"), float("inf"), True):
            with self.subTest(timeout=bad):
                self.refused("version_probe_failed", run_version_probe, FLOORS["claude_code"], path, bad)

    def test_finite_positive_numbers_are_kept(self) -> None:
        path = self.claude()
        for good in (1, 0.5, 20.0):
            with self.subTest(timeout=good):
                grant("claude_code", path, timeout=good, probe=self.line("2.1.318 (Claude Code)"))


# -- break-it: a probe (a fixture) that lies ----------------------------------------------------------------

class LyingProbeTests(Base):
    def test_malformed_results_are_refused_not_raised(self) -> None:
        path = self.claude()

        class Sub(str):
            pass

        class Shape:  # duck-typed, not a ProbeResult
            raw, exit_code, ms = "2.1.318 (Claude Code)", 0, 0

        results = [
            ProbeResult(None, 0, 0), ProbeResult(b"2.1.318 (Claude Code)", 0, 0), ProbeResult(5, 0, 0),
            ProbeResult("2.1.318 (Claude Code)", "0", 0), ProbeResult("2.1.318 (Claude Code)", False, 0),
            ProbeResult("2.1.318 (Claude Code)", 0.0, 0), ProbeResult(Sub("2.1.318 (Claude Code)"), 0, 0),
            Shape(), None, ("2.1.318 (Claude Code)", 0, 0),
        ]
        for result in results:
            with self.subTest(result=result):
                self.refused("version_probe_failed", grant, "claude_code", path, probe=lambda *a, r=result: r)

    def test_a_probe_that_raises_is_a_typed_refusal(self) -> None:
        path = self.claude()
        for exc in (RuntimeError("boom"), KeyError("digest"), OSError("tmp"), RecursionError()):
            with self.subTest(exc=type(exc).__name__):
                def probe(spec, realpath, timeout, exc=exc):
                    raise exc
                r = self.refused("version_probe_failed", grant, "claude_code", path, probe=probe)
                self.assertIn(type(exc).__name__, r.detail)

    def test_a_typed_refusal_from_the_probe_is_kept(self) -> None:
        def probe(spec, realpath, timeout):
            raise LaunchRefused("binary_missing", "claude_code", "spawn failed")
        self.refused("binary_missing", grant, "claude_code", self.claude(), probe=probe)

    @unittest.skipUnless(os.name == "posix", "POSIX fixtures")
    def test_one_lying_probe_does_not_abort_the_pass_for_every_engine(self) -> None:
        d = F.dir()
        F.make("claude", "2.1.318 (Claude Code)", where=d)
        F.make("codex", "codex-cli 0.160.0", where=d)

        def probe(spec, realpath, timeout):
            if spec.engine == "claude_code":
                return ProbeResult(None, 0, 0)
            return ProbeResult("codex-cli 0.160.0", 0, 0)

        grants, records = launch_check(d, probe=probe)
        self.assertEqual(set(FLOORS), {r["engine"] for r in records})
        self.assertEqual({"codex"}, set(grants))
        rec = next(r for r in records if r["engine"] == "claude_code")
        self.assertEqual(("refused", "version_probe_failed"), (rec["outcome"], rec["refusal"]))


# -- break-it: version lines that are not plain printable text ---------------------------------------------

class VersionLineTextTests(Base):
    LINES = (
        "2.1.318 (Claude Code)\x1b]0;owned\x07",  # a terminal escape riding on a valid line
        "2.1.318 (Claude Code)\x1b[31m",
        "2.1.318 (Claude\ufffd Code)",  # a non-UTF-8 byte, as the probe decodes it
        "2.1.318 (Claude Code)\r",
        "2.1.318\t(Claude Code)",
        "2.1.318 (Claude Code)\x00",
        "2.1.318 (Claude Code)\u202e",  # a bidirectional override
    )

    def test_a_line_that_is_not_printable_text_is_unparsed(self) -> None:
        for raw in self.LINES:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_version("semver", raw))

    def test_grant_refuses_such_a_line_instead_of_recording_it(self) -> None:
        path = self.claude()
        for raw in self.LINES:
            with self.subTest(raw=raw):
                self.refused("version_unparsed", grant, "claude_code", path, probe=self.line(raw))

    def test_printable_non_ascii_is_still_text(self) -> None:
        self.assertEqual((2, 1, 318), parse_version("semver", "2.1.318 (Claude Code) \u00e9").key)

    @unittest.skipUnless(os.name == "posix", "POSIX fixtures")
    def test_the_real_probe_reads_non_utf8_output_as_replacement_characters(self) -> None:
        d = F.dir()
        script = write(os.path.join(d, "claude"), "#!/bin/sh\nprintf '2.1.318 (Claude\\377 Code)\\n'\n")
        result = run_version_probe(FLOORS["claude_code"], script, 5.0)
        self.assertIn("\ufffd", result.raw)
        self.assertIsNone(parse_version("semver", result.raw))


# -- break-it: wrong types and str subclasses for every argument -------------------------------------------

class Lying(str):
    """A str whose encode lies about its size."""

    def encode(self, *a, **kw):  # noqa: D401
        return b""


class Unformatted(str):
    """A str that formats as nothing, so an f-string that measures it sees zero bytes."""

    def __format__(self, spec):
        return ""


class AlwaysEqual(str):
    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


class LookAlike(str):
    """Hashes and compares like 'claude_code' whatever it holds."""

    def __eq__(self, other):
        return other == "claude_code"

    def __hash__(self):
        return hash("claude_code")


class ArgumentTypeTests(Base):
    def test_engine_must_be_exactly_str(self) -> None:
        path = self.claude()
        for engine in (LookAlike("evil"), b"claude_code", None, 5, ["claude_code"]):
            with self.subTest(engine=type(engine).__name__):
                self.refused("engine_unknown", grant, engine, path, probe=self.line("2.1.318 (Claude Code)"))
                self.refused("engine_unknown", detect, engine, "/usr/bin")

    def test_path_must_be_exactly_str(self) -> None:
        path = self.claude()

        class P(str):
            pass

        for bad in (P(path), path.encode(), None, 5):
            with self.subTest(path=type(bad).__name__):
                self.refused("path_not_absolute", grant, "claude_code", bad, probe=self.line("2.1.318 (Claude Code)"))

    def test_a_str_subclass_cannot_hide_its_size_from_the_spawn_cap(self) -> None:
        g = grant("claude_code", self.claude(), probe=self.line("2.1.318 (Claude Code)"))
        with self.subTest(where="argv item"):
            self.refused("spawn_too_large", launch_plan, g, ["claude", Lying("x" * 200000)])
        with self.subTest(where="env value"):
            self.refused("spawn_too_large", launch_plan, g, ["claude"], {"BIG": Unformatted("x" * 200000)})
        with self.subTest(where="env name"):
            self.refused("spawn_too_large", launch_plan, g, ["claude"], {Unformatted("K" * 200000): "v"})

    def test_parse_version_reads_only_exact_text(self) -> None:
        class Printable(str):
            def isprintable(self):
                return True

        self.assertIsNone(parse_version("semver", Printable("2.1.318 (Claude Code)\x1b[31m")))
        self.assertIsNone(parse_version("semver", Printable("2.1.318")))

    def test_argv0_that_compares_equal_to_anything_is_refused(self) -> None:
        g = grant("claude_code", self.claude(), probe=self.line("2.1.318 (Claude Code)"))
        self.refused("argv0_mismatch", launch_plan, g, [AlwaysEqual("not-claude")])

    def test_plain_text_is_still_accepted(self) -> None:
        g = grant("claude_code", self.claude(), probe=self.line("2.1.318 (Claude Code)"))
        plan = launch_plan(g, ["claude", "-p", "hi"], {"HOME": "/tmp/h"})
        self.assertEqual({"HOME": "/tmp/h"}, plan.exec_env())


# -- the registrant: launch_check/hooks.py registers nothing ------------------------------------------------

class RegistrantTests(unittest.TestCase):
    def test_register_touches_nothing_on_the_hooks_it_is_given(self) -> None:
        hooks_mod = importlib.import_module("recursion_core.launch_check.hooks")
        touched = []

        class Recording:
            def __getattribute__(self, name):
                touched.append(name)
                raise AssertionError(f"register() read hooks.{name}")

        self.assertIsNone(hooks_mod.register(Recording()))
        self.assertEqual([], touched)

    def test_the_registrant_imports_nothing_from_control_or_the_daemon(self) -> None:
        hooks_mod = importlib.import_module("recursion_core.launch_check.hooks")
        tree = ast.parse(inspect.getsource(hooks_mod))
        names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertFalse([n for n in names if n.startswith(("recursion_core.control", "recursion_core.daemon"))])

    def test_the_registrant_names_the_single_valued_hook_it_will_set(self) -> None:
        hooks_mod = importlib.import_module("recursion_core.launch_check.hooks")
        self.assertEqual("launch_check", hooks_mod.HOOK)
        self.assertEqual("recursion_core.launch_check.hooks", hooks_mod.__name__)


if __name__ == "__main__":
    unittest.main()
