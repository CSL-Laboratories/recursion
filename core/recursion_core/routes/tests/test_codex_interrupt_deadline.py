"""An interrupt the app-server never finishes (finding B3-8.5, PRODUCT.md S6).

A stand-in app-server that never answers ``turn/interrupt`` and never sends ``turn/completed``.
After ``interrupt()`` the worker has to be gone within ``INTERRUPT_DEADLINE_S``: its input ended, and
the run handed to Stop's fence. A repeat interrupt of that same turn writes nothing and does not move
the deadline. No real codex process, credential or network is used.
"""

from __future__ import annotations

import json
import threading
import time
import unittest

from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests.test_codex_driver import (
    AppServer, Base, check_params, message,
)


class _MuteInterrupt(AppServer):
    """Records ``turn/interrupt`` and then stays silent: no answer, no ``turn/completed``."""

    def send(self, line: str) -> None:
        msg = json.loads(line)
        if msg.get("method") == "turn/interrupt":
            assert not self.stdin_closed, "send after close_stdin"
            check_params("client_requests", "turn/interrupt", msg.get("params"))
            self.sent.append(msg)
            self.requests[msg["id"]] = "turn/interrupt"
            return
        super().send(line)


def _bound() -> float:
    """The named deadline, or a stand-in number on a head that has not named one yet."""

    return getattr(cx, "INTERRUPT_DEADLINE_S", 1.0)


def _soon(predicate, seconds: float = 1.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class InterruptDeadlineTest(Base):
    """B3-8.5. The worker is still alive past any bound on a head that does not enforce the deadline."""

    def tearDown(self) -> None:
        session = getattr(getattr(self, "handle", None), "_codex", None)
        if session is None:
            return
        with session.lock:
            for stop in list(getattr(session, "interrupt_stops", {}).values()):
                stop.set()

    def _mute(self, server=None):
        self.open(server=server or _MuteInterrupt())
        self.driver.send(self.handle, message())
        self.drain()  # the turn is in progress and the engine has named it
        return self.driver._clock

    def test_a_silent_interrupt_ends_the_worker_at_the_deadline(self) -> None:
        clock = self._mute()
        self.driver.interrupt(self.handle, "turn-1")
        self.assertEqual(self.sent_methods().count("turn/interrupt"), 1)
        self.assertFalse(self.server.fenced)
        self.assertFalse(self.server.stdin_closed)
        clock.advance(_bound())
        self.assertTrue(_soon(lambda: bool(self.server.fenced)),
                        "the worker is still alive past the interrupt bound")
        self.assertEqual(self.server.fenced, ["interrupt_deadline"])
        self.assertTrue(self.server.stdin_closed)

    def test_a_repeat_interrupt_of_the_same_turn_is_a_no_op(self) -> None:
        clock = self._mute()
        bound = _bound()
        self.driver.interrupt(self.handle, "turn-1")
        clock.advance(bound / 2)
        self.driver.interrupt(self.handle, "turn-1")
        self.assertEqual(self.sent_methods().count("turn/interrupt"), 1)
        self.assertFalse(self.server.fenced)  # halfway is not the first interrupt's deadline
        clock.advance(bound / 2)
        self.assertTrue(_soon(lambda: bool(self.server.fenced)),
                        "a repeat moved the deadline, or the worker is still alive")
        self.assertEqual(self.server.fenced, ["interrupt_deadline"])
        self.assertEqual(self.sent_methods().count("turn/interrupt"), 1)

    def test_a_repeat_before_the_engine_names_the_turn_writes_one_interrupt(self) -> None:
        self.open(server=_MuteInterrupt())
        self.driver.send(self.handle, message())
        self.driver.interrupt(self.handle, "turn-1")
        self.driver.interrupt(self.handle, "turn-1")
        self.drain()
        self.assertEqual(self.sent_methods().count("turn/interrupt"), 1)
        self.driver._clock.advance(_bound())
        self.assertTrue(_soon(lambda: bool(self.server.fenced)))
        self.assertEqual(self.server.fenced, ["interrupt_deadline"])

    def test_two_callers_interrupt_the_same_turn_once(self) -> None:
        self._mute()
        errors: list[BaseException] = []
        gate = threading.Barrier(2)

        def call() -> None:
            gate.wait(5)
            try:
                self.driver.interrupt(self.handle, "turn-1")
            except BaseException as exc:  # noqa: BLE001 - the test records whatever left the call
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(self.sent_methods().count("turn/interrupt"), 1)

    def test_a_fence_that_raises_still_ends_the_input(self) -> None:
        class FenceRaises(_MuteInterrupt):
            def fence(self, reason: str) -> None:
                super().fence(reason)
                raise RuntimeError("stop fence failed")

        clock = self._mute(FenceRaises())
        self.driver.interrupt(self.handle, "turn-1")
        clock.advance(_bound())
        self.assertTrue(_soon(lambda: bool(self.server.fenced)))
        self.assertEqual(self.server.fenced, ["interrupt_deadline"])
        self.assertTrue(self.server.stdin_closed)

    def test_turn_completed_within_the_deadline_leaves_the_worker(self) -> None:
        self.open()
        self.driver.send(self.handle, message())
        self.drain()
        self.driver.interrupt(self.handle, "turn-1")
        self.assertEqual(self.server.sent[-1]["method"], "turn/interrupt")
        drafts = self.drain()
        self.assertEqual(drafts[-1]["payload"]["code"], "interrupted")
        self.assertIsNone(self.handle.live_turn)
        self.driver._clock.advance(_bound())
        time.sleep(0.1)
        self.assertFalse(self.server.fenced)
        self.assertFalse(self.server.stdin_closed)

    def test_an_interruption_while_ending_the_input_still_asks_for_the_fence(self) -> None:
        class CloseInterrupts(_MuteInterrupt):
            def close_stdin(self) -> None:
                raise KeyboardInterrupt()

        clock = self._mute(CloseInterrupts())
        self.driver.interrupt(self.handle, "turn-1")
        clock.advance(_bound())
        self.assertTrue(_soon(lambda: bool(self.server.fenced)),
                        "an interruption while ending the input skipped Stop's fence")
        self.assertEqual(self.server.fenced, ["interrupt_deadline"])


if __name__ == "__main__":
    unittest.main()
