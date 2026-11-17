# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The registrant registers nothing live; ``install`` registers exactly this slice's rows and the service."""
from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from recursion_core.control import hooks as H
from recursion_core.control.methods import METHODS
from recursion_core.sessions import hooks as session_hooks
from recursion_core.sessions.service import SessionsService
from recursion_core.sessions.tests.support import Lane


class RegistrantTest(unittest.TestCase):

    def test_the_registrant_is_the_name_reserved_on_the_fixed_list(self):
        self.assertIn('recursion_core.sessions.hooks', H.REGISTRANTS)
        self.assertEqual(session_hooks.__name__, 'recursion_core.sessions.hooks')

    def test_register_registers_nothing(self):
        hooks = H.Hooks()
        self.assertIsNone(session_hooks.register(hooks))
        hooks.freeze()
        self.assertEqual(hooks.methods.registered(), ())
        self.assertEqual(hooks.commands.names(), ())
        self.assertEqual(hooks._services, [])
        self.assertFalse(hooks.start_on)
        self.assertFalse(hooks.gate.enabled)

    def test_a_composition_with_this_module_installed_has_nothing_of_this_lane(self):
        composed = H.compose()
        self.assertEqual(composed.methods.registered(), ())
        self.assertEqual(composed.commands.names(), ())
        self.assertFalse(composed.gate.enabled)

    def test_install_registers_this_slice_s_rows_with_their_published_service_and_the_lane_s_service(self):
        lane = Lane()
        mine = {name for name in METHODS if name.startswith(('session.', 'queue.'))}
        registered = set(lane.hooks.methods.registered())
        self.assertEqual(registered, {'session.start', 'session.turn', 'session.steer', 'session.queue',
                                      'session.status', 'session.list', 'queue.update', 'queue.cancel'})
        self.assertEqual(mine - registered, {'session.interrupt', 'session.resume', 'session.fork', 'session.export',
                                             'session.stop'})
        self.assertEqual(lane.hooks.methods.services_needed(), {})
        self.assertEqual([service.name for service in lane.hooks._services], ['sessions'])


class ShutdownRetryTest(unittest.TestCase):
    """The integration triage of Nov 15 (int-398): a stop whose join was interrupted must still wait for its worker
    when it is repeated, so that the store is never closed under a live worker."""

    class Worker:
        """A thread that raises one KeyboardInterrupt in its first join, then ends."""
        alive = True
        joins = 0

        def is_alive(self):
            return self.alive

        def join(self):
            self.joins += 1
            if self.joins == 1:
                raise KeyboardInterrupt('one interruption before the join completed')
            self.alive = False

    def test_a_stop_repeated_after_an_interrupted_join_joins_the_worker_again(self):
        worker = self.Worker()
        service = SessionsService(SimpleNamespace(wake=threading.Event()))
        service._thread = worker
        with self.assertRaises(KeyboardInterrupt):
            service.stop(None)
        self.assertTrue(worker.alive)
        service.stop(None)
        self.assertEqual((worker.joins, worker.alive), (2, False))
        service.stop(None)
        self.assertEqual(worker.joins, 2, 'a joined thread is not joined again')

    def test_the_store_is_not_closed_under_a_worker_whose_join_was_interrupted(self):
        worker = self.Worker()
        closed_while_alive = []
        db = SimpleNamespace(close=lambda: closed_while_alive.append(worker.alive))
        service = SessionsService(SimpleNamespace(wake=threading.Event()))
        service._thread = worker
        hooks = H.Hooks()
        hooks.service(service)
        hooks.set('event_store', lambda state_dir: db)
        hooks.freeze()
        hooks.open_store(None)
        context = H.ServiceContext(store=db, closing=hooks.closing)
        hooks.start_services(context)
        with self.assertRaises(KeyboardInterrupt):      # SPINE's close keeps the interruption and finishes its teardown
            hooks.finish_close(context)
        self.assertEqual(closed_while_alive, [False], 'the store closed before the interrupted worker was joined')
        self.assertEqual(worker.joins, 2)

    def test_a_start_whose_thread_never_begins_lets_stop_return(self):
        """``start`` stores its thread only once ``Thread.start`` has returned. A start that raises there used to
        leave an unstarted thread assigned, and ``stop`` then raised ``RuntimeError`` on every call, so SPINE's
        teardown (which tries a raising stop again until it returns) never finished. DeepSeek round 7 of #398, F2."""
        sessions = SimpleNamespace(wake=threading.Event(), recover=lambda: None, dispatch_queued=lambda: 0,
                                   run_pending=lambda: 0)
        service = SessionsService(sessions, interval_s=30)
        real_start = threading.Thread.start

        def refuse(thread):
            raise RuntimeError("can't start new thread")

        threading.Thread.start = refuse
        try:
            with self.assertRaises(RuntimeError):
                service.start(None)
        finally:
            threading.Thread.start = real_start
        service.stop(None)
        self.assertIsNone(service._thread)
        service.stop(None)
        service.start(None)
        self.assertIsNotNone(service._thread)
        self.assertTrue(service._thread.is_alive())
        service.stop(None)
        self.assertIsNone(service._thread)
        self.assertFalse(service.ready)


if __name__ == '__main__':
    unittest.main()
