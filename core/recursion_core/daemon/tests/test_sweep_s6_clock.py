"""P3: a plugin wait that already timed out stays a timeout.

``test_F07_timed_out_plugin_wait_must_stay_a_timeout``. The worker finishes before
``_turn`` is taken again. Fixture bytes are not used.
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
        host._guarded = lambda handler, command: handler(command)
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
            answer = host._call_operator('example.plugin', lambda cmd: {'ok': True}, {})
        self.assertIsInstance(answer, tuple)
        self.assertEqual(answer[0], 504)
        self.assertEqual(host.degraded.get('example.plugin'), 1)
        self.assertFalse(host._busy)
        self.assertIsNone(host._stuck, 'a completed worker must not be marked stuck')


if __name__ == '__main__':
    unittest.main()
