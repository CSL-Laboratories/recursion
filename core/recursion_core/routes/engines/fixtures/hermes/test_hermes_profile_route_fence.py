"""routes/profile_route.py fences a started child on every way out of ``ProfileRouteAdapter.run``.

The Liaison's ruling of 3:46 PM PST, Nov 15, 2026 (lane ENGINE-E9's start note), part (2): lane DRIVER-CC found,
building draft #420, that ``profile_route.py`` had the gap ``routes/base.py`` had before #420. An exception outside
the three typed exits (``Deadline``, ``_Protocol``, ``OSError``) left ``run`` with the started child unfenced. The
rule taken from #420: once ``guard_then_spawn`` has returned a process, any exception that leaves ``run``,
``BaseException`` included, fences that process first, and the same exception object leaves. A fence that itself
fails is noted on that exception and never replaces it.

These tests live in lane ENGINE-E9's fixtures folder because the map gives engine lanes no test path yet
(``routes/tests/`` is lane DRIVER-CC's). Fake processes only: no engine, no real launcher.
"""

from __future__ import annotations

import builtins
import os
import pathlib
import shutil
import tempfile
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

    def test_an_exception_that_takes_no_note_still_leaves_unchanged(self) -> None:
        err = ValueError("drive")
        err.__notes__ = 42  # add_note then raises TypeError
        h = Harness(_raiser(err), process=Process(fence_raises=[RuntimeError("fence")]))
        with self.assertRaises(ValueError) as caught:
            h.run()
        self.assertIs(caught.exception, err)
        self.assertEqual((h.process.fenced, err.__notes__), ([UNEXPECTED], 42))

    def test_a_typed_fence_that_raises_is_tried_again_and_its_exception_leaves(self) -> None:
        for err, reason in ((Deadline(), "deadline"), (_Protocol("p"), "protocol_error"),
                            (BrokenPipeError(), "io_error")):
            with self.subTest(reason=reason):
                fence_err = RuntimeError("fence")
                h = Harness(_raiser(err), process=Process(fence_raises=[fence_err]))
                with self.assertRaises(RuntimeError) as caught:
                    h.run()
                self.assertIs(caught.exception, fence_err)
                self.assertEqual(h.process.fenced, [reason, UNEXPECTED])

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


if __name__ == "__main__":
    unittest.main()
