"""The shared route boundary fences a started child on every way out (routes/base.py, RouteAdapter.run).

Asked by the Liaison on Nov 15, 2026 from lane ENGINE-E6's draft #412 (its DeepSeek round 1, F2): an exception
outside the three typed exits (``Deadline``, ``_Protocol``, ``OSError``) used to leave ``run`` with the child
unfenced, so every engine driver had to fence for itself. Now the boundary fences a started child on every
exception, ``BaseException`` included, and the same exception object leaves. Fake processes only.
"""

from __future__ import annotations

import builtins
import traceback
import unittest
from unittest import mock

from recursion_core.routes import base
from recursion_core.routes import contract as c
from recursion_core.routes.base import Deadline, RouteAdapter, _Protocol
from recursion_core.routes.launch import ReadTimeout
from recursion_core.routes.tests.fakes import FakeClock, runs_adapter


class _Custom(BaseException):
    pass


class _CustomError(Exception):
    pass


class _CustomDeadline(Deadline):
    pass


def _kinds():
    """One fresh instance of every kind the typed exits do not catch."""

    return [
        KeyboardInterrupt(), SystemExit(3), GeneratorExit(), _Custom("custom"),
        MemoryError(), RecursionError(), RuntimeError("defect"), ValueError("bad"), TypeError("a line is text"),
        KeyError("k"), AttributeError("a"), AssertionError("x"), StopIteration(), ArithmeticError(),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), _CustomError("e"), ReadTimeout(),
        c.RouteError(c.ENGINE_ERROR, "raised after the launch"),
        builtins.BaseExceptionGroup("g", [KeyboardInterrupt()]), builtins.ExceptionGroup("g", [ValueError("v")]),
    ]


class Process:
    """An EngineProcess double that records every fence asked of it."""

    def __init__(self, *, read=None, fence_raises=()) -> None:
        self.fenced: list[str] = []
        self.sent: list[str] = []
        self._read = read
        self._fence_raises = list(fence_raises)

    def send(self, line: str) -> None:
        self.sent.append(line)

    def close_stdin(self) -> None:
        pass

    def read_line(self, timeout_s: float):
        if isinstance(self._read, BaseException):
            raise self._read
        return self._read

    def fence(self, reason: str) -> None:
        self.fenced.append(reason)
        if self._fence_raises:
            raise self._fence_raises.pop(0)


class Adapter(RouteAdapter):
    engine = "claude_code"

    def __init__(self, process=None, *, drive=None, launch_raises=None) -> None:
        self.process = process if process is not None else Process()
        self.launches = 0

        def launcher(argv, *, env, shell=False):
            self.launches += 1
            if launch_raises is not None:
                raise launch_raises
            return self.process

        super().__init__(launcher, FakeClock(), lambda *_: "", {})
        self._drive = drive

    def argv(self, request):
        return ["claude"]

    def env(self, request):
        return {}

    def drive(self, process, request, deadline_at, events):
        if self._drive is not None:
            return self._drive(process)
        self.read_message(process, deadline_at)
        return c.result(request["request_id"], None, text="ok", events=events)


def _raiser(err):
    def drive(process):
        raise err
    return drive


REQUEST = {"request_id": "r1"}
UNEXPECTED = "unexpected"  # the reason the boundary gives (base.UNEXPECTED_FENCE)
FAILURES = "cleanup_failures"  # base.CLEANUP_FAILURES: present only when no fence of the child succeeded
RUNNING = "no fence of the started child succeeded: it may still be running"  # base.RUNNING_NOTE
DEADLINE = 2_000_000.0


@runs_adapter
class EveryExceptionIsFencedTest(unittest.TestCase):
    def test_an_exception_out_of_drive_fences_the_child_and_the_same_object_leaves(self) -> None:
        for err in _kinds():
            with self.subTest(kind=type(err).__name__):
                adapter = Adapter(drive=_raiser(err))
                with self.assertRaises(BaseException) as caught:
                    adapter.run(REQUEST, DEADLINE)
                self.assertIs(caught.exception, err)
                self.assertEqual(adapter.process.fenced, [UNEXPECTED])
                self.assertFalse(hasattr(err, FAILURES))

    def test_an_exception_out_of_the_childs_read_fences_the_child_and_the_same_object_leaves(self) -> None:
        for err in _kinds():
            if isinstance(err, ReadTimeout):
                continue  # read_message turns a read timeout into the typed deadline (below)
            with self.subTest(kind=type(err).__name__):
                adapter = Adapter(Process(read=err))
                with self.assertRaises(BaseException) as caught:
                    adapter.run(REQUEST, DEADLINE)
                self.assertIs(caught.exception, err)
                self.assertEqual(adapter.process.fenced, [UNEXPECTED])

    def test_a_line_that_is_not_text_fences_the_child(self) -> None:
        # After the witness of #412's first DeepSeek read (F2, A4: a read_line that returns bytes, which its
        # parser refuses with TypeError). json.loads takes bytes, so here the line is a number.
        adapter = Adapter(Process(read=5))
        with self.assertRaises(TypeError):
            adapter.run(REQUEST, DEADLINE)
        self.assertEqual(adapter.process.fenced, [UNEXPECTED])

    def test_a_failing_fence_does_not_replace_the_exception(self) -> None:
        for fence_err in (RuntimeError("fence"), KeyboardInterrupt(), OSError(5, "io"), _Custom("f")):
            with self.subTest(fence=type(fence_err).__name__):
                err = ValueError("drive")
                adapter = Adapter(Process(fence_raises=[fence_err]), drive=_raiser(err))
                with self.assertRaises(ValueError) as caught:
                    adapter.run(REQUEST, DEADLINE)
                self.assertIs(caught.exception, err)
                self.assertEqual(adapter.process.fenced, [UNEXPECTED])
                self.assertIn(f"fencing the child also failed: {type(fence_err).__name__}",
                              getattr(err, "__notes__", []))
                self.assertEqual(getattr(err, FAILURES, None), (fence_err,))
                self.assertIn(RUNNING, err.__notes__)

    def test_a_note_hook_that_raises_a_base_exception_still_lets_the_original_leave(self) -> None:
        # Sol's R3 (M06): an exception whose add_note raises a custom BaseException.
        class Hooked(ValueError):
            def add_note(self, note):
                raise _Custom("hook")
        err = Hooked("drive")
        adapter = Adapter(Process(fence_raises=[RuntimeError("fence")]), drive=_raiser(err))
        with self.assertRaises(Hooked) as caught:
            adapter.run(REQUEST, DEADLINE)
        self.assertIs(caught.exception, err)
        self.assertEqual(adapter.process.fenced, [UNEXPECTED])

    def test_a_process_that_is_false_is_still_fenced(self) -> None:
        # Sol's R3 (M14): the launcher's process need not be truthy.
        class Falsy(Process):
            def __bool__(self):
                return False
        err = ValueError("drive")
        adapter = Adapter(Falsy(), drive=_raiser(err))
        with self.assertRaises(ValueError):
            adapter.run(REQUEST, DEADLINE)
        self.assertEqual(adapter.process.fenced, [UNEXPECTED])

    def test_the_exception_leaves_by_a_bare_re_raise(self) -> None:
        # DeepSeek's F4: ``raise err`` would add the run frame to the traceback a second time.
        err = ValueError("drive")
        try:
            Adapter(drive=_raiser(err)).run(REQUEST, DEADLINE)
        except ValueError as caught:
            frames = [f.name for f in traceback.extract_tb(caught.__traceback__)]
        else:
            self.fail("run returned")
        self.assertEqual(frames.count("run"), 1, frames)

    def test_an_exception_that_takes_no_note_still_leaves_unchanged(self) -> None:
        err = ValueError("drive")
        err.__notes__ = 42  # add_note then raises TypeError
        adapter = Adapter(Process(fence_raises=[RuntimeError("fence")]), drive=_raiser(err))
        with self.assertRaises(ValueError) as caught:
            adapter.run(REQUEST, DEADLINE)
        self.assertIs(caught.exception, err)
        self.assertEqual((adapter.process.fenced, err.__notes__), ([UNEXPECTED], 42))

    def test_a_typed_fence_that_raises_is_tried_again_and_the_typed_exception_leaves(self) -> None:
        # CHANGED ASSERTION (round 1 of #420, the Liaison's word of Nov 15, 5:23 PM): the original typed
        # exception leaves, not the fence's; the retry fenced the child, so the failure is a note only.
        for err, reason in ((Deadline(), "deadline"), (_Protocol("p"), "protocol_error"),
                            (BrokenPipeError(), "io_error")):
            with self.subTest(reason=reason):
                fence_err = RuntimeError("fence")
                adapter = Adapter(Process(fence_raises=[fence_err]), drive=_raiser(err))
                with self.assertRaises(type(err)) as caught:
                    adapter.run(REQUEST, DEADLINE)
                self.assertIs(caught.exception, err)
                self.assertEqual(adapter.process.fenced, [reason, UNEXPECTED])
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
                    adapter = Adapter(Process(fence_raises=[first, second]), drive=_raiser(err))
                    with self.assertRaises(type(err)) as caught:
                        adapter.run(REQUEST, DEADLINE)
                    self.assertIs(caught.exception, err)
                    self.assertEqual(adapter.process.fenced, [reason, UNEXPECTED])
                    self.assertEqual(getattr(err, FAILURES, None), (first, second))
                    self.assertIn(RUNNING, getattr(err, "__notes__", []))

    def test_an_exception_after_a_typed_fence_does_not_fence_twice(self) -> None:
        for err, reason in ((Deadline(), "deadline"), (_Protocol("p"), "protocol_error"),
                            (BrokenPipeError(), "io_error")):
            for later in (MemoryError(), KeyboardInterrupt()):
                with self.subTest(reason=reason, later=type(later).__name__):
                    adapter = Adapter(drive=_raiser(err))
                    with mock.patch.object(c, "result", side_effect=later):
                        with self.assertRaises(BaseException) as caught:
                            adapter.run(REQUEST, DEADLINE)
                    self.assertIs(caught.exception, later)
                    self.assertEqual(adapter.process.fenced, [reason])


@runs_adapter
class TypedExitsAreUnchangedTest(unittest.TestCase):
    def test_each_typed_exit_is_fenced_once_with_its_own_reason_and_answered(self) -> None:
        cases = ((Deadline(), "deadline", c.DEADLINE_EXCEEDED), (_CustomDeadline(), "deadline", c.DEADLINE_EXCEEDED),
                 (_Protocol("bad line"), "protocol_error", c.PROTOCOL_ERROR),
                 (BrokenPipeError(), "io_error", c.ENGINE_ERROR), (TimeoutError(), "io_error", c.ENGINE_ERROR),
                 (OSError(5, "io"), "io_error", c.ENGINE_ERROR))
        for err, reason, code in cases:
            with self.subTest(kind=type(err).__name__):
                adapter = Adapter(drive=_raiser(err))
                result = adapter.run(REQUEST, DEADLINE)
                self.assertEqual((result["code"], adapter.process.fenced), (code, [reason]))

    def test_the_reason_is_the_published_one(self) -> None:
        self.assertEqual(getattr(base, "UNEXPECTED_FENCE", None), UNEXPECTED)
        self.assertEqual((getattr(base, "CLEANUP_FAILURES", None), getattr(base, "RUNNING_NOTE", None)),
                         (FAILURES, RUNNING))

    def test_a_drive_that_answers_is_not_fenced(self) -> None:
        adapter = Adapter(Process(read=None))
        result = adapter.run(REQUEST, DEADLINE)
        self.assertEqual((result["code"], adapter.process.fenced), (None, []))


@runs_adapter
class NothingStartedIsNothingFencedTest(unittest.TestCase):
    def test_a_launcher_refusal_or_os_error_is_answered_and_nothing_is_fenced(self) -> None:
        cases = ((c.RouteError(c.LAUNCH_REFUSED, "no"), c.LAUNCH_REFUSED), (OSError(7, "big"), c.ENGINE_ERROR))
        for err, code in cases:
            with self.subTest(kind=type(err).__name__):
                adapter = Adapter(launch_raises=err)
                result = adapter.run(REQUEST, DEADLINE)
                self.assertEqual((result["code"], adapter.launches, adapter.process.fenced), (code, 1, []))

    def test_any_other_launcher_exception_leaves_as_itself_and_nothing_is_fenced(self) -> None:
        for err in (KeyboardInterrupt(), ValueError("v"), MemoryError(), _Custom("c")):
            with self.subTest(kind=type(err).__name__):
                adapter = Adapter(launch_raises=err)
                with self.assertRaises(BaseException) as caught:
                    adapter.run(REQUEST, DEADLINE)
                self.assertIs(caught.exception, err)
                self.assertEqual((adapter.process.fenced, getattr(err, "__notes__", None)), ([], None))

    def test_an_argv_or_env_failure_starts_nothing(self) -> None:
        adapter = Adapter()
        with mock.patch.object(Adapter, "env", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                adapter.run(REQUEST, DEADLINE)
        self.assertEqual((adapter.launches, adapter.process.fenced), (0, []))


if __name__ == "__main__":
    unittest.main()
