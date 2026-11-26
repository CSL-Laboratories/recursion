"""The third outside reads of #400 (two Codex reads of ``2fd180e4..9c7c9c92``), each change taken as a test that
fails on ``9c7c9c92``: when a start turns on the offering of the backlog to a surface that subscribes late, an
interrupted launch, a stop during a sweep, and a row whose setting aside fails. Five are Astra's own round-3
checks (``startup_boundaries.py``), taken as she wrote them with names and layout changed. Every thread a test
starts is a daemon with a bounded join.
"""

from __future__ import annotations

import threading
import types
from unittest import mock

from recursion_core.approvals import store as rows
from recursion_core.approvals.tests.test_queue import needs_fakes
from recursion_core.approvals.tests.test_review1 import _Base


def _context(store) -> types.SimpleNamespace:
    return types.SimpleNamespace(store=store)


@needs_fakes
class StartTests(_Base):
    def test_an_interrupted_launch_withdraws_ready(self) -> None:  # Astra N2 (her check)
        with mock.patch.object(threading.Thread, "start", side_effect=KeyboardInterrupt("synthetic launch interruption")):
            with self.assertRaises(KeyboardInterrupt):
                self.queue.start(_context(self.store))
        self.assertFalse(self.queue.ready, "an interrupted start left ready true with no worker launched")
        self.queue.stop()  # the worker never launched: stop lets it go
        self.assertIsNone(self.queue._worker)

    def test_a_failed_launch_offers_nothing_to_a_later_surface(self) -> None:  # Astra N1 (her check)
        approval_id = self.plugin_request()
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("synthetic launch failure")):
            with self.assertRaises(RuntimeError):
                self.queue.start(_context(self.store))
        shown = []
        self.queue.subscribe_requested(lambda record: shown.append(record["approval_id"]))
        self.assertEqual(shown, [], "a failed start turned on the backlog with no deadline worker")
        self.assertIsNone(self.queue._late)  # nor does it keep a list of the surfaces that subscribe after it
        self.queue.start(_context(self.store))  # a start that succeeds offers it, once
        self.addCleanup(self.queue.stop)
        self.assertEqual(shown, [approval_id])

    def _start_paused_in_recovery(self, queue, *, after: bool):
        """Start ``queue`` in a thread that pauses inside recovery (before it, or after it when ``after``)."""
        paused, release, errors = threading.Event(), threading.Event(), []
        original = queue.recover

        def recovery():
            if not after:
                paused.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")
            result = original()
            if after:
                paused.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")
            return result

        def start() -> None:
            try:
                queue.start(_context(self.store))
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                errors.append(type(exc).__name__)

        patch = mock.patch.object(queue, "recover", recovery)
        patch.start()
        self.addCleanup(patch.stop)
        starter = threading.Thread(target=start, daemon=True)
        starter.start()
        self.assertTrue(paused.wait(3))
        return starter, release, errors

    def test_a_surface_that_subscribes_during_start_is_offered_once_start_succeeds(self) -> None:  # Astra N1, hers
        approval_id = self.plugin_request()
        queue = self.restarted()
        starter, release, errors = self._start_paused_in_recovery(queue, after=True)
        shown = []
        try:
            queue.subscribe_requested(lambda record: shown.append(record["approval_id"]))
            before_launch = list(shown)
        finally:
            release.set()
            starter.join(4)
            queue.stop()
        self.assertFalse(starter.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(before_launch, [], "offered before the start had launched its worker")
        self.assertEqual(shown, [approval_id], "a surface that subscribed during the start lost the backlog")

    def test_a_surface_that_subscribes_during_recovery_is_not_offered_an_orphan_engine_request(self) -> None:
        # Astra N1 (her check): an engine request whose driver died is retired by recovery, never offered.
        approval_id = self.engine_request()
        queue = self.restarted()
        starter, release, errors = self._start_paused_in_recovery(queue, after=False)
        shown = []
        try:
            queue.subscribe_requested(lambda record: shown.append((record["approval_id"], record["state"])))
        finally:
            release.set()
            starter.join(4)
            queue.stop()
        self.assertFalse(starter.is_alive())
        self.assertEqual(errors, [])
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, approval_id)["state"], "expired")
        self.assertEqual(shown, [], "an engine request recovery retires was offered before it was retired")

    def test_a_surface_that_unsubscribes_during_start_is_offered_nothing(self) -> None:
        self.plugin_request()
        queue = self.restarted()
        starter, release, errors = self._start_paused_in_recovery(queue, after=True)
        shown = []
        try:
            unsubscribe = queue.subscribe_requested(lambda record: shown.append(record["approval_id"]))
            unsubscribe()
        finally:
            release.set()
            starter.join(4)
            queue.stop()
        self.assertEqual(errors, [])
        self.assertEqual(shown, [])

    def test_a_stop_during_a_sweep_leaves_ready_false(self) -> None:  # Astra's later item (her check)
        queue = self.queue
        entered, release, stopping = threading.Event(), threading.Event(), threading.Event()
        real_sweep = queue.sweep

        def sweep():
            if threading.current_thread().name == "approvals-deadlines":
                entered.set()
                release.wait(3)
            return real_sweep()

        queue.start(_context(self.store))
        with mock.patch.object(queue, "sweep", sweep):
            queue._wake.set()
            self.assertTrue(entered.wait(3))

            def stop() -> None:
                stopping.set()
                queue.stop()

            stopper = threading.Thread(target=stop, daemon=True)
            stopper.start()
            self.assertTrue(stopping.wait(3))
            self.assertTrue(queue._closing.wait(3))
            release.set()
            stopper.join(4)
        self.assertFalse(stopper.is_alive())
        self.assertIsNone(queue._worker)
        self.assertFalse(queue.ready, "a worker that finished its sweep published ready again after stop")


@needs_fakes
class SetAsideTests(_Base):
    def test_a_row_whose_setting_aside_fails_holds_back_no_other_row(self) -> None:  # Sol N2
        ids = iter(("apr-000-bad", "apr-111-good"))
        self.queue._mint = lambda: next(ids)
        bad, good = self.plugin_request(), self.plugin_request()
        with self.store.transaction() as tx:
            tx.execute("UPDATE approvals_records SET record = ? WHERE approval_id = ?", ("{}", bad))
        self.clock.now += 301
        with mock.patch.object(rows, "set_aside", side_effect=OSError("synthetic store failure")), \
                self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
            with self.assertRaises(OSError):  # raised after the others, so the worker withdraws ready and retries
                self.queue.sweep()
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, good)["state"], "expired")
            self.assertEqual(rows.due(tx, 10**15), [bad])  # not set aside yet: the next turn tries again
        with self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
            self.queue.sweep()
        with self.store.transaction() as tx:
            self.assertEqual(rows.due(tx, 10**15), [])


if __name__ == "__main__":
    import unittest

    unittest.main()
