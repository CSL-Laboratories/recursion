"""T18, T24 and T25 from the hooks sweep. Markers here are synthetic.

Run from the repository root with PYTHONPATH=core.
"""
from __future__ import annotations

import io
import logging
import sys
import threading
import unittest

from recursion_core.control import hooks as H


def configured(name, function):
    hooks = H.Hooks()
    hooks.set(name, function)
    hooks.freeze()
    return hooks


class HooksBoundaryTest(unittest.TestCase):
    def test_f1_credential_source_exception_text_never_reaches_logs(self):
        marker = 'SYNTHETIC_DIAGNOSTIC_MARKER_71'

        def source(connection, env_name):
            raise RuntimeError('decoding diagnostic: ' + marker)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        H.log.addHandler(handler)
        try:
            with self.assertRaises(KeyError) as caught:
                configured('credentials', source).credentials('connection-label', 'ENV_LABEL')
        finally:
            H.log.removeHandler(handler)
        self.assertEqual(caught.exception.args, ('connection-label',))
        self.assertTrue(stream.getvalue(), 'the failure must still be logged')
        self.assertNotIn(marker, stream.getvalue())

    def test_f4_page_methods_handles_a_set_changed_by_its_provider(self):
        shared = {'session.turn', 'session.status'}
        hooks = configured('page_methods', lambda: shared)
        mutate, changed = threading.Event(), threading.Event()
        coordinated = []

        def writer():
            if mutate.wait(2):
                shared.add('session.list')
                changed.set()

        other = threading.Thread(target=writer, daemon=True)
        other.start()
        armed = True

        def trace(frame, event, arg):
            nonlocal armed
            # Pause at iteration of the provider's set, without modifying product code.
            if (armed and event == 'line' and frame.f_code.co_filename == H.__file__
                    and frame.f_code.co_name == '<genexpr>'):
                armed = False
                mutate.set()
                coordinated.append(changed.wait(2))
            return trace

        previous_trace = sys.gettrace()
        try:
            sys.settrace(trace)
            answer = hooks.page_methods()
        finally:
            sys.settrace(previous_trace)
            mutate.set()
            other.join(2)
        self.assertFalse(other.is_alive())
        self.assertNotIn(False, coordinated, 'the helper exceeded its deadline')
        self.assertIsInstance(answer, frozenset)
        self.assertLessEqual(answer, frozenset(shared))

    def test_f5_drive_owner_validation_failure_declines_ownership(self):
        class Owner:
            @property
            def run(self):
                raise OSError('driver not initialized')

        hooks = configured('drive_owner', lambda context, request: Owner())
        self.assertIsNone(hooks.drive_owner(object(), {}))


if __name__ == '__main__':
    unittest.main()
