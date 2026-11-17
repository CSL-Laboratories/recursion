"""P3: a plugin wait that already timed out stays a timeout.

``test_F07_timed_out_plugin_wait_must_stay_a_timeout``. The worker finishes before
``_turn`` is taken again. ``_guarded`` takes the one callable the worker passes, so
the payload this test discards is that worker's body.
``test_an_event_that_cannot_be_created_does_not_leave_the_plugin_busy`` pins the
step between taking ``_busy`` and creating the event. Fixture bytes are not used.
"""
import threading
import types
import unittest
from unittest.mock import patch

from recursion_core.daemon import plugins


class DeadlineRegressionTests(unittest.TestCase):
    def test_F07_timed_out_plugin_wait_must_stay_a_timeout(self):
        host = plugins.PluginHost.__new__(plugins.PluginHost)
        host._turn = threading.Condition()
        host._stuck = None
        host._busy = False
        host.degraded = {}
        host._operator_deadline_s = 1.0
        host._operator_turn_wait_s = 0.1
        # The worker calls ``_guarded`` with one callable. Two parameters made every
        # run a TypeError, so a late body was never what this test judged.
        host._guarded = lambda call: call()
        worker = []

        class Thread:
            def __init__(self, target, **kw):
                worker.append(target)

            def start(self):
                pass

        class Event:
            flag = False

            def set(self):
                self.flag = True

            def is_set(self):
                return self.flag

            def wait(self, timeout):
                worker[0]()
                return False

        with patch.object(plugins, 'threading', types.SimpleNamespace(Thread=Thread, Event=Event)):
            answer = host._call_operator(
                'example.plugin', lambda cmd: {'ok': True, 'late': 'LATE-ANSWER'}, {})
        self.assertIsInstance(answer, tuple)
        self.assertEqual(answer[0], 504)
        self.assertEqual(host.degraded.get('example.plugin'), 1)
        self.assertFalse(host._busy)
        self.assertIsNone(host._stuck, 'a completed worker must not be marked stuck')

    def test_an_event_that_cannot_be_created_does_not_leave_the_plugin_busy(self):
        host = plugins.PluginHost.__new__(plugins.PluginHost)
        host._turn = threading.Condition()
        host._stuck = None
        host._busy = False
        host.degraded = {}
        host._operator_deadline_s = 1.0
        host._operator_turn_wait_s = 0.1
        host._guarded = lambda call: call()

        class Boom(Exception):
            pass

        with patch.object(plugins.threading, 'Event', side_effect=Boom('event could not be created')):
            with self.assertRaises(Boom):
                host._call_operator('example.plugin', lambda cmd: {'ok': True}, {})
        self.assertFalse(host._busy, 'a failed event left the plugin busy')
        self.assertIsNone(host._stuck)


if __name__ == '__main__':
    unittest.main()
