"""The sixth outside read of #400 (a DeepSeek break-it read of ``2d1a0b91``), each change taken as a test that fails
on ``2d1a0b91``:

* R6-1: a start interrupted once its worker has launched, at the step that lets the surfaces through (as the lock
  is taken, or just after it is let go), leaves the queue not started and never deaf: ``ready`` withdrawn, the
  worker told to return and joined by ``stop``, each request after it offered to every surface at once, and a
  later start works;
* R6-2: the drawer takes every session id the record's grammar takes, the longest ``run:`` id included;
* R6-3: the queue's own ``open_in`` and ``answer_in`` refuse a principal that is not an approver exactly as a
  record keeps one, and ``open_in`` a surface that is not one of ``SURFACES``, so nothing they write leaves the
  record unreadable;
* R6-4: a timeout ``Event.wait`` cannot take (past ``threading.TIMEOUT_MAX``, a huge int, minus infinity) is a
  typed refusal, and ``threading.TIMEOUT_MAX`` itself is a wait.

One more test passes on ``2d1a0b91`` too and holds what the R6-1 change must keep: a second start that fails
before its launch leaves the worker already running alone. R6-5 to R6-7 are answered in the text.

From the round-5 Codex read by astra of ``2d1a0b91`` (its changes 2 to 5 are R6-1 to R6-4 above; its change 1 is
the command id, in ``test_review4.py``): a start interrupted after it has published its state and before its launch
is withdrawn too (astra's red test, taken as written with names and layout changed; it fails on ``2d1a0b91``), and
astra's two lock checks, which pass there too: the start publishes its state and the worker withdraws ``ready``
under the queue's lock. From sol's read of the same head (its M1 to M5 are the same five changes): the driver's
``ask`` takes the longest mixed ``run:`` session id, and ``threading.TIMEOUT_MAX + 1`` is refused (both fail on
``2d1a0b91``); its DEL command id is in ``test_review4.py``. Every thread a test starts is a daemon with a bounded
join.
"""

from __future__ import annotations

import threading
import types
import unittest
from unittest import mock

from recursion_core.approvals import effects
from recursion_core.approvals import record
from recursion_core.approvals import store as rows
from recursion_core.approvals.effects import EffectRefused
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.approvals.tests.test_queue import needs_fakes
from recursion_core.approvals.tests.test_review1 import OWNER, _Base


def _context(store) -> types.SimpleNamespace:
    return types.SimpleNamespace(store=store)


class _InterruptOnce:
    """The queue's lock, raising ``KeyboardInterrupt`` (where a signal would) once, in the starting thread, at the
    first ``with`` it enters (``at="enter"``, before the lock is taken) or leaves (``at="exit"``, once the lock is
    let go) after it is armed. Every other use is the real lock's."""

    def __init__(self, lock, starter, at: str) -> None:
        self._lock, self._starter, self._at = lock, starter, at
        self.armed = self.raised = False

    def _interrupt(self, at: str) -> None:
        if self.armed and not self.raised and self._at == at and threading.current_thread() is self._starter:
            self.raised = True
            raise KeyboardInterrupt("synthetic interruption at the first lock taken after the launch")

    def __enter__(self):
        self._interrupt("enter")
        return self._lock.__enter__()

    def __exit__(self, *exc_info):
        let_go = self._lock.__exit__(*exc_info)
        self._interrupt("exit")
        return let_go


@needs_fakes
class InterruptedFenceTests(_Base):
    def _interrupted_after_the_launch(self, at: str) -> None:  # DS R6-1
        before = self.plugin_request()  # no surface is subscribed: it stays unshown
        queue, shown = self.restarted(), []
        queue.subscribe_requested(lambda held: shown.append(held["approval_id"]))
        lock = _InterruptOnce(queue._lock, threading.current_thread(), at)
        queue._lock = lock
        launched, real_start = [], threading.Thread.start

        def start(thread):
            real_start(thread)
            launched.append(thread)
            lock.armed = True  # the next lock the start takes is the one that lets the surfaces through

        with mock.patch.object(threading.Thread, "start", start):
            with self.assertRaises(KeyboardInterrupt):
                queue.start(_context(self.store))
        self.assertTrue(lock.raised)
        self.assertEqual(len(launched), 1)
        self.assertFalse(queue.ready, "a start interrupted after its launch left the queue ready")
        launched[0].join(5)
        self.assertFalse(launched[0].is_alive(), "the worker of the interrupted start was not told to return")
        self.assertEqual(shown, [], "a start interrupted after its launch offered the backlog")
        after = self.plugin_request(queue=queue)
        self.assertEqual(shown, [after], "a request after the interrupted start reached no surface")
        late: list = []
        queue.subscribe_requested(lambda held: late.append(held["approval_id"]))
        self.assertEqual(late, [], "the queue took itself as started: a new surface was offered the backlog")
        last = self.plugin_request(queue=queue)
        self.assertEqual((shown, late), ([after, last], [last]))
        queue.stop()
        queue.start(_context(self.store))  # a start after that works and offers what no surface took
        self.addCleanup(queue.stop)
        self.assertTrue(queue.ready)
        self.assertTrue(queue._worker.is_alive())
        self.assertEqual((shown, late), ([after, last, before], [last, before]))

    def test_a_start_interrupted_as_it_lets_the_surfaces_through_is_withdrawn_and_not_deaf(self) -> None:
        self._interrupted_after_the_launch("enter")

    def test_a_start_interrupted_just_after_it_let_the_surfaces_through_is_withdrawn_and_not_deaf(self) -> None:
        self._interrupted_after_the_launch("exit")

    def test_a_second_start_that_fails_before_its_launch_leaves_the_running_worker_alone(self) -> None:
        queue = self.restarted()  # passes on 2d1a0b91 too: what the R6-1 change must keep
        queue.start(_context(self.store))
        self.addCleanup(queue.stop)
        first = queue._worker
        with mock.patch.object(queue, "recover", side_effect=RuntimeError("synthetic recovery failure")):
            with self.assertRaises(RuntimeError):
                queue.start(_context(self.store))
        self.assertIs(queue._worker, first)
        self.assertTrue(first.is_alive())
        self.assertTrue(queue.ready)
        shown: list = []
        queue.subscribe_requested(lambda held: shown.append(held["approval_id"]))
        self.assertEqual(shown, [self.plugin_request(queue=queue)])


class _InterruptAfterPublication:
    """The queue's lock, raising ``KeyboardInterrupt`` once as it is let go while the start has published its state
    (``ready``, a worker) and not yet launched that worker (astra's round-5 check)."""

    def __init__(self, queue, lock) -> None:
        self._queue, self._lock = queue, lock
        self.raised = False

    def __enter__(self):
        return self._lock.__enter__()

    def __exit__(self, *exc_info):
        let_go = self._lock.__exit__(*exc_info)
        worker = self._queue._worker
        if not self.raised and worker is not None and worker.ident is None and self._queue.ready:
            self.raised = True
            raise KeyboardInterrupt("synthetic interruption after the start published its state")
        return let_go


@needs_fakes
class PublishedStateTests(_Base):
    def test_a_start_interrupted_after_it_published_its_state_before_the_launch_is_withdrawn(self) -> None:
        # Astra's round-5 change 2 (its test_R6_1_interrupt_after_state_publication_before_launch_withdraws_ready).
        queue, shown = self.restarted(), []
        queue.subscribe_requested(lambda held: shown.append(held["approval_id"]))
        real_lock = queue._lock
        lock = _InterruptAfterPublication(queue, real_lock)
        queue._lock = lock
        try:
            with self.assertRaises(KeyboardInterrupt):
                queue.start(_context(self.store))
        finally:
            queue._lock = real_lock
        self.assertTrue(lock.raised)
        self.assertIsNone(queue._worker.ident, "the interruption came after the launch")
        self.assertFalse(queue.ready, "a start interrupted before its launch advertised ready with no worker")
        later = self.plugin_request(queue=queue)
        self.assertEqual(shown, [later], "a request after the interrupted start reached no surface")
        queue.stop()  # a worker that never launched is let go
        queue.start(_context(self.store))
        self.addCleanup(queue.stop)
        self.assertTrue(queue.ready)
        self.assertTrue(queue._worker.is_alive())

    def test_the_start_publishes_its_state_under_the_queue_lock(self) -> None:  # astra's lock check
        queue, observed = self.queue, []
        clear = queue._closing.clear

        def clearing() -> None:
            observed.append(queue._lock.locked())
            clear()

        with mock.patch.object(queue._closing, "clear", clearing):
            queue.start(_context(self.store))
        queue.stop()
        self.assertEqual(observed, [True])

    def test_the_worker_withdraws_ready_under_the_queue_lock(self) -> None:  # astra's lock check
        observed: list = []

        class Witness(ApprovalQueue):
            def __setattr__(self, name, value):
                if name == "ready" and value is False and threading.current_thread() is getattr(self, "_worker", None):
                    observed.append(self._lock.locked())
                super().__setattr__(name, value)

        queue = Witness(clock=self.clock, retry_seconds=5)
        queue.attach(self.store, declare_tables=False)
        self.addCleanup(queue.stop)
        paused, real_wait, real_sweep = threading.Event(), queue._closing.wait, queue.sweep

        def sweep():
            if threading.current_thread() is queue._worker:
                raise OSError("synthetic failure of the one worker")
            return real_sweep()  # recovery's own sweep, in the start

        def wait(timeout=None):
            paused.set()
            return real_wait(timeout)

        queue._wake.set()
        with mock.patch.object(queue, "sweep", sweep), mock.patch.object(queue._closing, "wait", wait):
            with self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
                queue.start(_context(self.store))
                self.assertTrue(paused.wait(2))
                queue.stop()
        self.assertTrue(observed)
        self.assertTrue(all(observed), observed)


@needs_fakes
class DrawerGrammarTests(_Base):
    def test_the_drawer_takes_every_session_id_the_record_grammar_takes(self) -> None:  # DS R6-2
        for session in ("run:" + "a" * 60, "run:" + "a" * 61, "run:" + "a" * 64, "ses-" + "b" * 60):
            with self.subTest(length=len(session)):
                effect = dict(self.engine, session_id=session)
                self.assertIsNone(record.effect_problem("engine_tool", effect))
                bound = effects.bind("engine_tool", effect)
                self.assertIn({"text": "session: " + session, "trust": "trusted"}, bound.rendered)
        longest = "run:" + "a" * 64
        approval_id = self.queue.request("engine_tool", dict(self.engine, session_id=longest), session_id=longest,
                                         run_id=self.engine["run_id"], turn_id="turn-0001", engine_request_id="toolu_9")
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, approval_id)["session_id"], longest)
        mixed = "run:" + "A_-" * 21 + "Z"  # sol's round-5 case: the longest, through a driver's ask
        asked = self.queue.ask(dict(self.engine, session_id=mixed), {"engine_request_id": "toolu_10"})
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, asked)["effect"]["session_id"], mixed)
        for session in ("run:" + "a" * 65, "ses-" + "b" * 61):
            with self.subTest(refused=len(session)):
                with self.assertRaises(EffectRefused) as refused:
                    effects.bind("engine_tool", dict(self.engine, session_id=session))
                self.assertEqual(refused.exception.code, "effect")


@needs_fakes
class RecordShapeTests(_Base):
    def test_open_in_and_answer_in_refuse_what_the_record_could_not_keep(self) -> None:  # DS R6-3
        approval_id = self.plugin_request()
        with self.store.transaction() as tx:
            before = rows.load(tx, approval_id)
        opening = {"command_id": "o-00000001", "approval_id": approval_id, "surface": "cli"}
        answering = dict(opening, command_id="a-00000001", expected_revision=before["revision"],
                         digest=before["digest"], answer="approve")
        scope = {"code": "scope_refused", "detail": "approve"}
        for principal in ({"kind": "owner", "id": "local", "extra": 1}, {"kind": "device", "id": "a b"},
                          {"kind": "owner", "id": "x" * 129}, {"kind": "owner", "id": "a" + chr(10)}):
            with self.subTest(principal=repr(principal)):
                with self.store.transaction() as tx:
                    self.assertEqual(self.queue.open_in(tx, principal, opening), scope)
                told: list = []
                with self.store.transaction() as tx:
                    self.assertEqual(self.queue.answer_in(tx, principal, answering, told.append), scope)
                self.assertEqual(told, [])
        for surface in (7, "bogus", None, ["cli"]):
            with self.subTest(surface=repr(surface)):
                with self.store.transaction() as tx:
                    opened = self.queue.open_in(tx, OWNER, dict(opening, surface=surface))
                self.assertEqual(opened, {"code": "request_invalid", "detail": "surface"})
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, approval_id), before)  # still whole: nothing was written
        self.assertEqual(self.approve(approval_id)["state"], "approved")


@needs_fakes
class TimeoutBoundTests(_Base):
    def test_a_timeout_event_wait_cannot_take_is_a_typed_refusal(self) -> None:  # DS R6-4
        approval_id = self.plugin_request()
        for timeout in (int(threading.TIMEOUT_MAX) + 1, threading.TIMEOUT_MAX + 1, 1e15, 1e308, 2 ** 63, 10 ** 400,
                        float("-inf")):
            with self.subTest(timeout=repr(timeout)[:24]):
                with self.assertRaises(ApprovalRefused) as refused:
                    self.queue.wait(approval_id, timeout=timeout)
                self.assertEqual(refused.exception.code, "timeout")
        for timeout in (-1, -10 ** 400, 0):
            with self.subTest(timeout=repr(timeout)[:24]):
                self.assertIsNone(self.queue.wait(approval_id, timeout=timeout))  # returns at once
        answered = threading.Timer(0.2, self.approve, args=(approval_id,))
        answered.daemon = True
        answered.start()
        try:
            held = self.queue.wait(approval_id, timeout=threading.TIMEOUT_MAX)
        finally:
            answered.join(5)
        self.assertFalse(answered.is_alive())
        self.assertEqual(held["state"], "approved")


if __name__ == "__main__":
    unittest.main()
