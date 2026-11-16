"""Lane LAUNCH-CHECK, #424 round 2: the witnesses of the three reads at 4524333d (Codex astra C1 to C6, Codex sol
1 to 4, DeepSeek r2 F1 to F7) and of the review threads open on that head, each kept as a test that fails at
4524333d and passes after the answering commit.

Every binary is a fake built in a temp dir (a copy of a native binary under an engine's name, or a ``/bin/sh``
script for the probe); no real engine, network or credential is used. A script that starts a descendant writes
its pid to a file, and the test kills it whatever the outcome.

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest recursion_core.launch_check.tests.test_round2 -v``.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import recursion_core.launch_check as package
from recursion_core.launch_check import (
    FLOORS,
    REFUSAL_CODES,
    LaunchRefused,
    ProbeResult,
    authenticate,
    grant,
    launch_check,
    launch_plan,
    parse_version,
    run_version_probe,
    verify,
)
from recursion_core.launch_check import binary as binary_mod
from recursion_core.launch_check import probe as probe_mod
from recursion_core.launch_check.detect import path_hits
from recursion_core.launch_check.floors import check_floors
from recursion_core.launch_check.tests import fixtures as fixtures_mod
from recursion_core.launch_check.tests.fixtures import Factory

F: Factory
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSIX = os.name == "posix"


def setUpModule() -> None:
    global F
    F = Factory()


def tearDownModule() -> None:
    F.cleanup()


def text(name: str) -> str:
    """A package file's text with every run of whitespace made one space, so a phrase may wrap."""
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return " ".join(f.read().split())


def script(body: str, name: str = "claude") -> str:
    path = os.path.join(F.dir(), name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return path


def kill_pid_in(pidfile: str) -> None:
    """Kill the descendant a fixture script recorded, if it is still there."""
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip() or "0")
    except (OSError, ValueError):
        return
    if pid > 0:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def state_of(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1].split()[0]
    except OSError:
        return "gone"


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


# -- Codex astra C3, Codex sol 1: a CR at end of output is not CRLF framing ------------------------------------

class FinalCarriageReturnTests(Base):
    """Only a CR that an LF follows belongs to the framing; a lone CR at the end of the output stays in the line,
    so ``parse_version`` refuses it."""

    def test_lone_final_cr_is_kept_for_the_parser(self):
        self.assertEqual("2.1.318 (Claude Code)\r", probe_mod._first_line(b"2.1.318 (Claude Code)\r"))
        self.assertIsNone(parse_version("semver", probe_mod._first_line(b"2.1.318 (Claude Code)\r")))
        self.assertIsNone(parse_version("semver", probe_mod._first_line(b"\n2.1.318 (Claude Code)\r")))

    def test_crlf_and_no_newline_are_still_framing(self):
        for data in (b"2.1.318 (Claude Code)\r\n", b"2.1.318 (Claude Code)", b"\r\n2.1.318 (Claude Code)\n"):
            with self.subTest(data=data):
                self.assertEqual("2.1.318", parse_version("semver", probe_mod._first_line(data)).text)

    @unittest.skipUnless(POSIX, "POSIX shell fixture")
    def test_lone_final_cr_from_a_real_child(self):
        path = script("printf '2.1.318 (Claude Code)\\r'\n")
        result = run_version_probe(FLOORS["claude_code"], path, 5)
        self.assertTrue(result.raw.endswith("\r"), repr(result.raw))
        self.assertIsNone(parse_version("semver", result.raw))


# -- Codex astra C2, Codex sol 2, Devin thread on probe.py:165: an integer too long to print ---------------------

class HugeIntegerTimeoutTests(Base):
    def test_integer_past_the_string_limit_is_typed(self):
        for i, bad in enumerate((10**5000, -(10**5000), 10**4400)):
            with self.subTest(case=i):
                e = self.refused("version_probe_failed", probe_mod.check_timeout, FLOORS["claude_code"], bad)
                self.assertIn("<int>", str(e))

    def test_short_values_are_still_shown(self):
        e = self.refused("version_probe_failed", probe_mod.check_timeout, FLOORS["claude_code"], 3601)
        self.assertIn("3601", str(e))

    def test_whole_pass_keeps_a_record_per_engine(self):
        d = F.dir()
        F.make("claude", "2.1.318 (Claude Code)", where=d)
        grants, records = launch_check(d, timeout=10**5000, probe=self.line("2.1.318 (Claude Code)"))
        self.assertEqual(set(FLOORS), {r["engine"] for r in records})


# -- Codex astra C1, Codex sol 3: the session is released after an exceptional exit, leader reaped or not --------

@unittest.skipUnless(POSIX, "POSIX sessions")
class ReapedLeaderSessionTests(Base):
    def test_descendant_is_killed_when_an_exception_follows_the_reap(self):
        pidfile = os.path.join(F.dir(), "descendant.pid")
        path = script(f"sleep 30 &\necho $! > {pidfile}\nexit 0\n", "leader")
        self.addCleanup(kill_pid_in, pidfile)
        real_poll = subprocess.Popen.poll

        def poll_then_raise(proc):
            code = real_poll(proc)
            if code is not None:
                raise RuntimeError("after-leader-reaped")
            return code

        with mock.patch.object(subprocess.Popen, "poll", poll_then_raise):
            with self.assertRaisesRegex(RuntimeError, "after-leader-reaped"):
                run_version_probe(FLOORS["claude_code"], path, 5)
        with open(pidfile) as f:
            pid = int(f.read())
        end = time.monotonic() + 3
        while state_of(pid) not in ("gone", "Z") and time.monotonic() < end:
            time.sleep(0.02)
        self.assertIn(state_of(pid), ("gone", "Z"), "a same-session descendant outlived the exceptional exit")

    def test_mocked_reap_then_interrupt_still_kills_the_group(self):
        proc = mock.Mock(returncode=None, pid=424242)

        def interrupted_poll():
            proc.returncode = 0  # waitpid completed; the interrupt arrives before the ordinary cleanup
            raise KeyboardInterrupt("after reaping leader")

        proc.poll.side_effect = interrupted_poll
        readers = [mock.Mock(ident=1, is_alive=mock.Mock(return_value=False)),
                   mock.Mock(ident=2, is_alive=mock.Mock(return_value=False))]
        with mock.patch.object(probe_mod.subprocess, "Popen", return_value=proc), \
                mock.patch.object(probe_mod, "_CappedReader", side_effect=readers), \
                mock.patch.object(probe_mod.os, "killpg") as killpg:
            with self.assertRaises(KeyboardInterrupt):
                run_version_probe(FLOORS["claude_code"], "/mock-only", 1)
        killpg.assert_any_call(424242, signal.SIGKILL)


# -- Codex astra C4, Codex sol 4, DeepSeek r2 F1: a forged field never escapes untyped -------------------------

class CannotCopy:
    def __init__(self, exc: type[BaseException]) -> None:
        self.exc = exc

    def __deepcopy__(self, memo):
        raise self.exc("injected copy failure")


class IterRaise(list):
    """A list whose iteration raises something other than a TypeError or a ValueError."""

    def __iter__(self):
        raise RuntimeError("injected iteration failure")


class ForgedFieldTests(Base):
    def forged(self, **change):
        g = self.claude_grant()
        for name, value in change.items():
            object.__setattr__(g, name, value)
        return g

    def test_deepcopy_that_raises_is_grant_forged_at_every_entry_point(self):
        for exc in (RuntimeError, AttributeError):
            for name in ("path", "entry"):
                g = self.forged(**{name: CannotCopy(exc) if name == "path" else (CannotCopy(exc),)})
                with self.subTest(exc=exc.__name__, field=name):
                    self.refused("grant_forged", authenticate, g)
                    self.refused("grant_forged", verify, g)
                    self.refused("grant_forged", launch_plan, g, ["claude"])

    def test_deep_nesting_and_cycles_are_grant_forged(self):
        nested: tuple = ()
        for _ in range(5000):
            nested = (nested,)
        cycle: list = []
        cycle.append(cycle)
        for value in (nested, cycle):
            with self.subTest(kind=type(value).__name__):
                self.refused("grant_forged", verify, self.forged(entry=value))

    def test_a_field_whose_encoding_raises_is_grant_forged(self):
        for name in ("path", "entry"):
            with self.subTest(field=name):
                self.refused("grant_forged", authenticate, self.forged(**{name: IterRaise(["x"])}))

    def test_non_ascii_or_non_text_mac_is_grant_forged(self):
        for mac in ("é" * 64, "é", b"0" * 64, None):
            with self.subTest(mac=mac):
                self.refused("grant_forged", verify, self.forged(mac=mac))

    def test_a_good_grant_still_authenticates(self):
        g = self.claude_grant()
        self.assertIs(FLOORS["claude_code"], authenticate(g))
        verify(g)


# -- Codex astra C5 (stricter than sol), DeepSeek r2 F4: a timed floor still asks for its commit all day --------

class TimedFloorTests(Base):
    def test_a_timed_floor_requires_its_commit_for_the_whole_day(self):
        for floor, found in (
            ("2025.09.17-10-00-00-25b418f", "2025.09.17-10-00-01-aaaaaa"),
            ("2025.09.17-10-00-00-25b418f", "2025.09.17-23-59-59-aaaaaa"),
            ("2025.09.17-00-00-00-25b418f", "2025.09.17-23-59-59"),
            ("2025.09.17-00-00-00-25b418f", "2025.09.17-00-00-00-aaaaaa"),
        ):
            with self.subTest(floor=floor, found=found):
                spec = dataclasses.replace(FLOORS["cursor"], security=floor)
                self.refused("version_refused", check_floors, spec, parse_version("date", found))

    def test_the_floor_commit_and_later_days_pass(self):
        spec = dataclasses.replace(FLOORS["cursor"], security="2025.09.17-10-00-00-25b418f")
        for found in ("2025.09.17-10-00-00-25b418f", "2025.09.17-23-00-00-25b418f", "2025.09.18-00-00-00-aaaaaa"):
            with self.subTest(found=found):
                check_floors(spec, parse_version("date", found))
        self.refused("version_refused", check_floors, spec, parse_version("date", "2025.09.16-23-59-59-aaaaaa"))


# -- Codex astra C6, DeepSeek r2 F2, F3, F5: the documents say what the code does ----------------------------

class DocumentTests(unittest.TestCase):
    def test_decoder_plan_names_the_intent_line(self):
        doc = text("DECODER.md")
        self.assertNotIn('the "exec" line', doc)
        self.assertIn('the "intent" line followed by end of file (outcome unknown)', doc)

    def test_decoder_allows_a_second_line_after_a_failed_exec(self):
        doc = text("DECODER.md")
        self.assertNotIn("writes exactly one line:", doc)
        self.assertIn("a second line only when `execve` returns", doc)

    def test_identity_wording_names_the_required_line(self):
        self.assertNotIn("An engine with no identity rule and no pin", text("README.md"))
        self.assertNotIn("# engine has no identity rule or pinned digest", text("refusals.py"))
        self.assertIn("neither a required identity line nor a pin", text("README.md"))

    def test_sigkill_leftovers_are_disclosed(self):
        readme = text("README.md")
        self.assertIn("SIGKILL", readme)
        self.assertIn("`r2a-probe-*`", readme)

    def test_verify_says_it_authenticates(self):
        self.assertIn("authenticat", verify.__doc__)


# -- DeepSeek r2 F6, F7: the cap is on the package surface; a bad env is typed -------------------------------

class SurfaceTests(Base):
    def test_probe_timeout_cap_is_exported(self):
        self.assertIn("MAX_PROBE_TIMEOUT_S", package.__all__)
        self.assertEqual(3600.0, package.MAX_PROBE_TIMEOUT_S)

    def test_env_that_is_not_a_mapping_is_typed(self):
        g = self.claude_grant()

        class BadIterable:
            def __iter__(self):
                raise TypeError("invalid env iterable")

        for env in (object(), BadIterable(), [("A",)], 3):
            with self.subTest(env=type(env).__name__):
                self.refused("spawn_too_large", launch_plan, g, ["claude"], env)

    def test_env_on_a_forged_grant_is_grant_forged_first(self):
        g = self.claude_grant()
        object.__setattr__(g, "mac", "0" * 64)
        self.refused("grant_forged", launch_plan, g, ["claude"], object())


# -- Devin thread on probe.py:129: the blocking reader branch (Windows) must not hang cleanup ------------------

@unittest.skipUnless(POSIX and shutil.which("setsid"), "needs POSIX setsid")
class BlockingReaderCleanupTests(Base):
    """On Windows the reader blocks in ``read1`` and ``stop`` cannot end it. The branch is forced here on POSIX
    with a descendant that leaves the probe's session and keeps both pipes open."""

    def test_a_held_pipe_does_not_hold_the_check(self):
        pidfile = os.path.join(F.dir(), "holder.pid")
        path = script(f"setsid sleep 40 >&1 2>&2 &\necho $! > {pidfile}\nexit 0\n", "holder")
        self.addCleanup(kill_pid_in, pidfile)
        out: dict[str, object] = {}

        def run() -> None:
            try:
                out["result"] = run_version_probe(FLOORS["claude_code"], path, 1)
            except BaseException as e:  # noqa: BLE001 - recorded and asserted below
                out["error"] = e

        with mock.patch.object(probe_mod, "_SELECT_READS", False, create=True), \
                mock.patch.object(probe_mod.os, "name", "nt"), \
                mock.patch.object(probe_mod, "_kill_group", lambda proc: (proc.kill(), proc.wait())):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            t.join(15)
            done = not t.is_alive()
        kill_pid_in(pidfile)
        t.join(10)
        self.assertTrue(done, "cleanup waited on a reader blocked by a descendant's open pipe")
        self.assertIsInstance(out.get("error"), LaunchRefused, out)
        self.assertEqual("version_probe_failed", out["error"].code)


# -- Devin thread on probe.py:134: a pipe numbered 1024 or above is still read; a read failure is typed ---------

@unittest.skipUnless(POSIX, "POSIX descriptors")
class DescriptorTests(Base):
    def test_a_pipe_above_1023_is_read(self):
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft <= 1101:
            self.skipTest(f"descriptor limit {soft} is too low for this case")
        if _is_open(1100):
            self.skipTest("descriptor 1100 is in use")
        r, w = os.pipe()
        high = os.dup2(r, 1100)
        os.close(r)
        self.addCleanup(lambda: os.close(high) if _is_open(high) else None)
        os.write(w, b"2.1.318 (Claude Code)\n")
        os.close(w)
        stream = os.fdopen(high, "rb", closefd=False)
        reader = probe_mod._CappedReader(stream, threading.Event())
        reader.start()
        reader.join(5)
        self.assertFalse(reader.is_alive())
        self.assertEqual(b"2.1.318 (Claude Code)\n", bytes(reader.data))

    def test_a_reader_that_fails_is_a_typed_refusal(self):
        path = script("echo '2.1.318 (Claude Code)'\n")
        real_read = os.read

        def failing_read(fd, n):
            if isinstance(threading.current_thread(), probe_mod._CappedReader):
                raise OSError(5, "injected read failure")
            return real_read(fd, n)

        with mock.patch.object(os, "read", failing_read):
            self.refused("version_probe_failed", run_version_probe, FLOORS["claude_code"], path, 5)


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


# -- Devin thread on detect.py:228: on Windows a bare file in an earlier directory hides no PATHEXT hit --------

class WindowsAcrossDirectoriesTests(Base):
    def put(self, d: str, name: str) -> str:
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"x")
        os.chmod(p, 0o755)
        return p

    def test_bare_name_ranks_after_every_extension_hit(self):
        d1, d2 = F.dir(), F.dir()
        bare = self.put(d1, "pi")
        cmd = self.put(d2, "pi.cmd")
        exe1, exe2 = self.put(d1, "claude"), self.put(d2, "claude.exe")
        with mock.patch.object(binary_mod, "is_windows", return_value=True):
            self.assertEqual([cmd, bare], path_hits("pi", d1 + os.pathsep + d2, True))
            self.assertEqual([exe2, exe1], path_hits("claude", d1 + os.pathsep + d2))

    def test_directory_order_holds_among_extension_hits(self):
        d1, d2 = F.dir(), F.dir()
        cmd = self.put(d1, "pi.cmd")
        exe = self.put(d2, "pi.exe")
        with mock.patch.object(binary_mod, "is_windows", return_value=True):
            self.assertEqual([cmd, exe], path_hits("pi", d1 + os.pathsep + d2, True))

    def test_posix_order_is_unchanged(self):
        d1, d2 = F.dir(), F.dir()
        a, b = self.put(d1, "pi"), self.put(d2, "pi")
        with mock.patch.object(binary_mod, "is_windows", return_value=False):
            self.assertEqual([a, b], path_hits("pi", d1 + os.pathsep + d2, True))


# -- Devin thread on grant.py (outdated line, still true of the head): Windows command-line length --------------

class WindowsCommandLineTests(Base):
    LIMIT = 32767

    def plan_on_windows(self, g, argv):
        with mock.patch.object(binary_mod, "is_windows", return_value=True):
            return launch_plan(g, argv)

    def test_over_the_createprocess_limit_is_spawn_too_large(self):
        g = self.claude_grant()
        for arg in ("x" * 40000, '"' * 17000, "\U0001F600" * 17000):
            with self.subTest(arg=arg[:1]):
                self.refused("spawn_too_large", self.plan_on_windows, g, ["claude", "-p", arg])

    def test_under_the_limit_passes_and_posix_keeps_its_byte_rules(self):
        g = self.claude_grant()
        self.plan_on_windows(g, ["claude", "-p", "x" * 30000])
        with mock.patch.object(binary_mod, "is_windows", return_value=False):
            launch_plan(g, ["claude", "-p", "x" * 40000])


# -- Devin thread on tests/fixtures.py:99: no /bin/true (native Windows) does not stop module setup ------------

class FixtureFallbackTests(unittest.TestCase):
    def test_factory_builds_without_bin_true(self):
        real_isfile, real_copy = os.path.isfile, shutil.copyfile

        def isfile(p):
            return False if p == "/bin/true" else real_isfile(p)

        def copyfile(src, dst, *a, **kw):
            if src == "/bin/true":
                raise FileNotFoundError(2, "No such file", src)
            return real_copy(src, dst, *a, **kw)

        with mock.patch.object(fixtures_mod.shutil, "which", return_value=None), \
                mock.patch.object(fixtures_mod.os.path, "isfile", isfile), \
                mock.patch.object(fixtures_mod.shutil, "copyfile", copyfile):
            f = Factory()
        self.addCleanup(f.cleanup)
        self.assertFalse(f.compiled)
        with open(sys.executable, "rb") as exe:
            self.assertEqual(exe.read(), f.base)


if __name__ == "__main__":
    unittest.main()
