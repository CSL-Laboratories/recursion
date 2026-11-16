"""routes/profile_route.py fences a started child on every way out of ``ProfileRouteAdapter.run``.

The Liaison's ruling of 3:46 PM PST, Nov 15, 2026 (lane ENGINE-E9's start note), part (2): lane DRIVER-CC found,
building draft #420, that ``profile_route.py`` had the gap ``routes/base.py`` had before #420. An exception outside
the three typed exits (``Deadline``, ``_Protocol``, ``OSError``) left ``run`` with the started child unfenced. The
rule taken from #420 (at its head 5beaa221): once ``guard_then_spawn`` has returned a process, any exception that
leaves ``run``, ``BaseException`` included, fences that process first, and the same exception object leaves. When a
typed exit's fence raises, the original typed exception leaves and the fence is tried once more as ``unexpected``. A
fence failure never replaces the exception that leaves: it is noted on it, and when no fence attempt succeeded the
exception carries the failures in ``cleanup_failures``. Not covered (stated in the code): an asynchronous exception
delivered between the guard's return and the store into ``process``.

These tests live in lane ENGINE-E9's fixtures folder because the map gives engine lanes no test path yet
(``routes/tests/`` is lane DRIVER-CC's). Fake processes only: no engine, no real launcher.
"""

from __future__ import annotations

import builtins
import os
import pathlib
import shutil
import tempfile
import traceback
import unittest
from unittest import mock

from recursion_core.routes import contract as c
from recursion_core.routes import profile_route
from recursion_core.routes.base import Deadline, _Protocol
from recursion_core.routes.launch import ReadTimeout
from recursion_core.routes.profile_route import ProfileRouteAdapter, profile_adapters
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, request
from recursion_core.subscription_routes import SubscriptionRouteRefused, WrapperInstall, measure_package_pin

UNEXPECTED = "unexpected"  # the reason the boundary gives (profile_route.UNEXPECTED_FENCE; #420's word)
FAILURES = "cleanup_failures"  # profile_route.CLEANUP_FAILURES: present only when no fence of the child succeeded
RUNNING = "no fence of the started child succeeded: it may still be running"  # profile_route.RUNNING_NOTE
DEADLINE = 2_000_000.0
PROFILE = {"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY",
           "auth.adopt_external_logins": False}
NOT_POSIX = os.name == "nt" or not hasattr(os, "geteuid")
POSIX_ONLY = "POSIX-only: the real guard's route home owner check needs os.geteuid (as routes/tests/test_launch_guards.py)"


class _Custom(BaseException):
    pass


class _CustomError(Exception):
    pass


class _CustomDeadline(Deadline):
    pass


def _kinds():
    """One fresh instance of every kind the typed exits do not catch (the list #420's tests use)."""

    return [
        KeyboardInterrupt(), SystemExit(3), GeneratorExit(), _Custom("custom"),
        MemoryError(), RecursionError(), RuntimeError("defect"), ValueError("bad"), TypeError("a line is text"),
        KeyError("k"), AttributeError("a"), AssertionError("x"), StopIteration(), ArithmeticError(),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), _CustomError("e"), ReadTimeout(),
        c.RouteError(c.ENGINE_ERROR, "raised after the launch"),
        SubscriptionRouteRefused("hermes", "raised_after_the_launch"),
        builtins.BaseExceptionGroup("g", [KeyboardInterrupt()]), builtins.ExceptionGroup("g", [ValueError("v")]),
    ]


class Process:
    """An EngineProcess double that records every fence asked of it."""

    def __init__(self, *, fence_raises=()) -> None:
        self.fenced: list[str] = []
        self._fence_raises = list(fence_raises)

    def send(self, line: str) -> None:
        pass

    def close_stdin(self) -> None:
        pass

    def read_line(self, timeout_s: float):
        return None

    def fence(self, reason: str) -> None:
        self.fenced.append(reason)
        if self._fence_raises:
            raise self._fence_raises.pop(0)


def _raiser(err):
    def drive(process, req, deadline_at, events):
        raise err
    return drive


def _answer(process, req, deadline_at, events):
    return c.result(req["request_id"], None, text="driven", events=events)


def _req():
    return request(engine="hermes", model="m", kind="subscription", conn_id="login-A", profile=dict(PROFILE),
                   home="/var/tmp/not-read-by-the-stand-in")


class Harness:
    """A ProfileRouteAdapter whose guard is a stand-in: it records each call and returns ``process``.

    Everything else in ``run`` is the real code. The real guard is exercised by ``RealGuardTest`` below.
    """

    def __init__(self, drive=_answer, *, process=None, guard_raises=None) -> None:
        self.process = process if process is not None else Process()
        self.guard_calls = 0
        self.adapter = ProfileRouteAdapter("hermes", FakeLauncher(), FakeClock(), lambda *_: "", {"PATH": "/usr/bin"},
                                           argv=lambda req: ["hermes"], driver=drive, operator_home="/home/operator")
        self._guard_raises = guard_raises

    def guard(self, *args, **kwargs):
        self.guard_calls += 1
        if self._guard_raises is not None:
            raise self._guard_raises
        return object(), self.process

    def run(self):
        with mock.patch.object(profile_route, "guard_then_spawn", self.guard):
            return self.adapter.run(_req(), DEADLINE)


class EveryExceptionIsFencedTest(unittest.TestCase):
    def test_an_exception_out_of_the_driver_fences_the_child_and_the_same_object_leaves(self) -> None:
        for err in _kinds():
            with self.subTest(kind=type(err).__name__):
                h = Harness(_raiser(err))
                with self.assertRaises(BaseException) as caught:
                    h.run()
                self.assertIs(caught.exception, err)
                self.assertEqual(h.process.fenced, [UNEXPECTED])

    def test_a_failing_fence_does_not_replace_the_exception(self) -> None:
        for fence_err in (RuntimeError("fence"), KeyboardInterrupt(), OSError(5, "io"), _Custom("f")):
            with self.subTest(fence=type(fence_err).__name__):
                err = ValueError("drive")
                h = Harness(_raiser(err), process=Process(fence_raises=[fence_err]))
                with self.assertRaises(ValueError) as caught:
                    h.run()
                self.assertIs(caught.exception, err)
                self.assertEqual(h.process.fenced, [UNEXPECTED])
                self.assertIn(f"fencing the child also failed: {type(fence_err).__name__}",
                              getattr(err, "__notes__", []))
                self.assertEqual(getattr(err, FAILURES, None), (fence_err,))
                self.assertIn(RUNNING, err.__notes__)

    def test_the_exception_leaves_by_a_bare_re_raise(self) -> None:
        # As #420's test of the same name (F5 of this draft's DeepSeek read): ``raise err`` would add the
        # adapter's run frame to the traceback a second time.
        err = ValueError("drive")
        try:
            Harness(_raiser(err)).run()
        except ValueError as caught:
            frames = [f.name for f in traceback.extract_tb(caught.__traceback__)]
        else:
            self.fail("run returned")
        self.assertEqual(frames.count("run"), 2, frames)  # Harness.run, then ProfileRouteAdapter.run once

    def test_an_exception_that_takes_no_note_still_leaves_unchanged(self) -> None:
        err = ValueError("drive")
        err.__notes__ = 42  # add_note then raises TypeError
        h = Harness(_raiser(err), process=Process(fence_raises=[RuntimeError("fence")]))
        with self.assertRaises(ValueError) as caught:
            h.run()
        self.assertIs(caught.exception, err)
        self.assertEqual((h.process.fenced, err.__notes__), ([UNEXPECTED], 42))

    def test_a_typed_fence_that_raises_is_tried_again_and_the_typed_exception_leaves(self) -> None:
        # CHANGED ASSERTION (round 1 of #425; #420's rule at 5beaa221, by the Liaison's word of Nov 15): the
        # original typed exception leaves, not the fence's; the retry fenced the child, so the failure is a note only.
        for err, reason in ((Deadline(), "deadline"), (_Protocol("p"), "protocol_error"),
                            (BrokenPipeError(), "io_error")):
            with self.subTest(reason=reason):
                fence_err = RuntimeError("fence")
                h = Harness(_raiser(err), process=Process(fence_raises=[fence_err]))
                with self.assertRaises(type(err)) as caught:
                    h.run()
                self.assertIs(caught.exception, err)
                self.assertEqual(h.process.fenced, [reason, UNEXPECTED])
                self.assertIn("fencing the child also failed: RuntimeError", getattr(err, "__notes__", []))
                self.assertFalse(hasattr(err, FAILURES))
                self.assertNotIn(RUNNING, getattr(err, "__notes__", []))

    def test_a_typed_fence_whose_retry_fails_too_leaves_the_typed_exception_marked_not_clean(self) -> None:
        for make, reason in ((Deadline, "deadline"), (lambda: _Protocol("p"), "protocol_error"),
                             (BrokenPipeError, "io_error")):
            for second in (RuntimeError("again"), KeyboardInterrupt(), _Custom("c")):
                err = make()
                with self.subTest(reason=reason, second=type(second).__name__):
                    first = OSError(5, "fence")
                    h = Harness(_raiser(err), process=Process(fence_raises=[first, second]))
                    with self.assertRaises(type(err)) as caught:
                        h.run()
                    self.assertIs(caught.exception, err)
                    self.assertEqual(h.process.fenced, [reason, UNEXPECTED])
                    self.assertEqual(getattr(err, FAILURES, None), (first, second))
                    self.assertIn(RUNNING, getattr(err, "__notes__", []))

    def test_an_exception_after_a_typed_fence_does_not_fence_twice(self) -> None:
        for err, reason in ((Deadline(), "deadline"), (_Protocol("p"), "protocol_error"),
                            (BrokenPipeError(), "io_error")):
            for later in (MemoryError(), KeyboardInterrupt()):
                with self.subTest(reason=reason, later=type(later).__name__):
                    h = Harness(_raiser(err))
                    with mock.patch.object(c, "result", side_effect=later):
                        with self.assertRaises(BaseException) as caught:
                            h.run()
                    self.assertIs(caught.exception, later)
                    self.assertEqual(h.process.fenced, [reason])


class TypedExitsAreUnchangedTest(unittest.TestCase):
    def test_each_typed_exit_is_fenced_once_with_its_own_reason_and_answered(self) -> None:
        cases = ((Deadline(), "deadline", c.DEADLINE_EXCEEDED), (_CustomDeadline(), "deadline", c.DEADLINE_EXCEEDED),
                 (_Protocol("bad line"), "protocol_error", c.PROTOCOL_ERROR),
                 (BrokenPipeError(), "io_error", c.ENGINE_ERROR), (TimeoutError(), "io_error", c.ENGINE_ERROR),
                 (OSError(5, "io"), "io_error", c.ENGINE_ERROR))
        for err, reason, code in cases:
            with self.subTest(kind=type(err).__name__):
                h = Harness(_raiser(err))
                result = h.run()
                self.assertEqual((result["code"], h.process.fenced), (code, [reason]))

    def test_the_reason_is_the_published_one(self) -> None:
        self.assertEqual(getattr(profile_route, "UNEXPECTED_FENCE", None), UNEXPECTED)
        self.assertEqual((getattr(profile_route, "CLEANUP_FAILURES", None), getattr(profile_route, "RUNNING_NOTE", None)),
                         (FAILURES, RUNNING))

    def test_a_driver_that_answers_is_not_fenced(self) -> None:
        h = Harness()
        result = h.run()
        self.assertEqual((result["code"], result["text"], h.process.fenced), (None, "driven", []))


class NothingStartedIsNothingFencedTest(unittest.TestCase):
    def test_a_guard_refusal_or_os_error_is_answered_and_nothing_is_fenced(self) -> None:
        cases = ((SubscriptionRouteRefused("hermes", "no"), c.SUBSCRIPTION_ROUTE_REFUSED),
                 (OSError(7, "big"), c.ENGINE_ERROR))
        for err, code in cases:
            with self.subTest(kind=type(err).__name__):
                h = Harness(guard_raises=err)
                result = h.run()
                self.assertEqual((result["code"], h.guard_calls, h.process.fenced), (code, 1, []))

    def test_any_other_guard_exception_leaves_as_itself_and_nothing_is_fenced(self) -> None:
        for err in (KeyboardInterrupt(), ValueError("v"), MemoryError(), _Custom("c")):
            with self.subTest(kind=type(err).__name__):
                h = Harness(guard_raises=err)
                with self.assertRaises(BaseException) as caught:
                    h.run()
                self.assertIs(caught.exception, err)
                self.assertEqual((h.process.fenced, getattr(err, "__notes__", None)), ([], None))

    def test_a_refusal_before_the_guard_starts_nothing(self) -> None:
        h = Harness()
        req = request(engine="hermes", model="m", kind="api_key", conn_id="k1", credential_env="SECRET_THING",
                      profile=dict(PROFILE), home="/var/tmp/x")
        with mock.patch.object(profile_route, "guard_then_spawn", h.guard):
            result = h.adapter.run(req, DEADLINE)
        self.assertEqual((result["code"], h.guard_calls, h.process.fenced), (c.LAUNCH_REFUSED, 0, []))


class RealGuardTest(unittest.TestCase):
    """The same rule through the real ``guard_then_spawn`` and a fake launcher (Pi, with a measured pin)."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="route-home-")
        os.chmod(self.home, 0o700)
        self.addCleanup(shutil.rmtree, self.home)

    def pin(self) -> WrapperInstall:
        root = pathlib.Path(tempfile.mkdtemp(prefix="route-pin-"))
        self.addCleanup(shutil.rmtree, root)
        root.joinpath("launcher.js").write_text("launcher\n", encoding="ascii")
        root.joinpath("lib.js").write_text("lib\n", encoding="ascii")
        launcher = str(root / "launcher.js")
        return WrapperInstall(launcher, str(root), measure_package_pin(launcher, str(root)))

    @unittest.skipIf(NOT_POSIX, POSIX_ONLY)
    def test_a_started_child_is_fenced_when_the_driver_raises(self) -> None:
        for err in (RuntimeError("defect"), KeyboardInterrupt()):
            with self.subTest(kind=type(err).__name__):
                clock = FakeClock()
                launcher = FakeLauncher(lambda argv, env: [], clock)
                adapters = profile_adapters(launcher, clock, lambda cid, name: "sk-test",
                                            {"PATH": "/usr/bin", "HOME": "/home/operator"},
                                            argv=lambda req: [req["route"]["wrapper"].launcher_path],
                                            driver=_raiser(err), operator_home="/home/operator")
                profile = {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"}
                req = request(engine="pi", model="m", kind="api_key", conn_id="k1",
                              credential_env="ANTHROPIC_API_KEY", profile=profile, home=self.home, wrapper=self.pin())
                with self.assertRaises(BaseException) as caught:
                    adapters["pi"].run(req, DEADLINE)
                self.assertIs(caught.exception, err)
                self.assertEqual(len(launcher.processes), 1)
                self.assertEqual(launcher.processes[0].fenced, [UNEXPECTED])



class RoundOneProofGapsTest(unittest.TestCase):
    """The tests the first reads of #425 asked for (Codex GPT-6 Astra R1, GPT-6.1 Sol R1 to R3), taken from the diffs
    they left in their evidence folders (``r1_additions.patch``, ``required-tests.diff``), merged where they overlap."""

    def test_every_profile_engine_fences_unexpected_exits(self) -> None:
        # Astra R1.1: Pi, Hermes and OpenCode, adapter and request on the same engine.
        for engine in c.PROFILE_ENGINES:
            for err in (ValueError("drive"), KeyboardInterrupt(), SystemExit(3)):
                with self.subTest(engine=engine, error=type(err).__name__):
                    process = Process()
                    adapters = profile_adapters(FakeLauncher(), FakeClock(), lambda *_: "", {},
                                                argv=lambda req: [req["route"]["engine"]], driver=_raiser(err))
                    req = _req()
                    req["route"]["engine"] = engine
                    with mock.patch.object(profile_route, "guard_then_spawn", return_value=(object(), process)):
                        with self.assertRaises(BaseException) as caught:
                            adapters[engine].run(req, DEADLINE)
                    self.assertIs(caught.exception, err)
                    self.assertEqual(process.fenced, [UNEXPECTED])

    @unittest.skipIf(NOT_POSIX, POSIX_ONLY)
    def test_opencode_unexpected_exit_through_real_guard_is_fenced(self) -> None:
        # Sol R1: OpenCode through the real guard (it needs no wrapper pin).
        with tempfile.TemporaryDirectory(prefix="route-home-") as home:
            os.chmod(home, 0o700)
            for err in (RuntimeError("drive"), KeyboardInterrupt(), SystemExit(7)):
                with self.subTest(kind=type(err).__name__):
                    clock = FakeClock()
                    launcher = FakeLauncher(lambda argv, env: [], clock)
                    adapters = profile_adapters(launcher, clock, lambda *_: "synthetic-key",
                                                {"PATH": "/usr/bin"}, argv=lambda _: ["opencode"],
                                                driver=_raiser(err), operator_home="/home/operator")
                    profile = {"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY"}
                    req = request(engine="opencode", kind="api_key", credential_env="OPENAI_API_KEY",
                                  profile=profile, home=home)
                    with self.assertRaises(BaseException) as caught:
                        adapters["opencode"].run(req, DEADLINE)
                    self.assertIs(caught.exception, err)
                    self.assertEqual(len(launcher.processes), 1)
                    self.assertEqual(launcher.processes[0].fenced, [UNEXPECTED])

    def test_process_truthiness_is_not_part_of_ownership(self) -> None:
        # Astra R1.2 and Sol R3: a started process that tests false, and one whose truth conversion raises.
        class FalseyProcess(Process):
            def __bool__(self):
                return False

        class TruthRaisesProcess(Process):
            def __bool__(self):
                raise KeyboardInterrupt("process truth conversion")

        for kind in (FalseyProcess, TruthRaisesProcess):
            with self.subTest(process=kind.__name__):
                err = ValueError("drive")
                process = kind()
                h = Harness(_raiser(err), process=process)
                with self.assertRaises(BaseException) as caught:
                    h.run()
                self.assertIs(caught.exception, err)
                self.assertEqual(process.fenced, [UNEXPECTED])

    def test_a_baseexception_from_add_note_never_replaces_the_driver_exception(self) -> None:
        # Astra R1.3 and Sol R2: add_note raises KeyboardInterrupt, SystemExit or a custom BaseException while the
        # fence fails.
        for note_err in (KeyboardInterrupt(), SystemExit(9), _Custom("hook")):
            with self.subTest(kind=type(note_err).__name__):
                class RefusesNote(ValueError):
                    def add_note(self, note, _raised=note_err):
                        raise _raised
                err = RefusesNote("drive")
                h = Harness(_raiser(err), process=Process(fence_raises=[RuntimeError("fence")]))
                with self.assertRaises(BaseException) as caught:
                    h.run()
                self.assertIs(caught.exception, err)
                self.assertEqual(h.process.fenced, [UNEXPECTED])

    def test_a_failing_fence_is_noted_on_an_original_baseexception(self) -> None:
        # Astra R1.4: the original is a BaseException; the note and the attribute are on that very object.
        for err in (KeyboardInterrupt(), SystemExit(3), _Custom("drive")):
            with self.subTest(error=type(err).__name__):
                fence_err = RuntimeError("fence")
                h = Harness(_raiser(err), process=Process(fence_raises=[fence_err]))
                with self.assertRaises(BaseException) as caught:
                    h.run()
                self.assertIs(caught.exception, err)
                self.assertEqual(h.process.fenced, [UNEXPECTED])
                self.assertIn("fencing the child also failed: RuntimeError", getattr(err, "__notes__", []))
                self.assertEqual(getattr(err, FAILURES, None), (fence_err,))


    def test_an_exception_that_refuses_the_attribute_still_leaves_noted(self) -> None:
        # The lane's own mutation pass (N1): setting cleanup_failures may raise a BaseException; the original
        # exception still leaves, fenced once, with its note.
        class RefusesAttribute(ValueError):
            def __setattr__(self, name, value):
                if name == FAILURES:
                    raise KeyboardInterrupt("attribute")
                super().__setattr__(name, value)
        err = RefusesAttribute("drive")
        h = Harness(_raiser(err), process=Process(fence_raises=[RuntimeError("fence")]))
        with self.assertRaises(RefusesAttribute) as caught:
            h.run()
        self.assertIs(caught.exception, err)
        self.assertEqual(h.process.fenced, [UNEXPECTED])
        self.assertFalse(hasattr(err, FAILURES))
        self.assertIn("fencing the child also failed: RuntimeError", err.__notes__)

    def test_an_interruption_at_the_typed_fence_s_entry_still_fences_the_child(self) -> None:
        # The lane's own mutation pass (O7): ``fenced`` is set only after the typed fence returns. An interruption
        # that lands at the helper's entry, before it fences, leaves ``fenced`` false, so the outer handler fences.
        for err in (Deadline(), _Protocol("p"), BrokenPipeError()):
            with self.subTest(kind=type(err).__name__):
                interruption = KeyboardInterrupt("at the helper's entry")
                h = Harness(_raiser(err))
                with mock.patch.object(profile_route, "_typed_fence", side_effect=interruption):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        h.run()
                self.assertIs(caught.exception, interruption)
                self.assertEqual(h.process.fenced, [UNEXPECTED])


if __name__ == "__main__":
    unittest.main()
