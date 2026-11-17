"""T18, T24 and T25 from the hooks sweep. Markers here are synthetic.

Run from the repository root with PYTHONPATH=core.
"""
from __future__ import annotations

import io
import logging
import sys
import threading
import traceback
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

    def test_a_page_walk_that_raises_answers_the_empty_set(self):
        """A walk that raises something other than RuntimeError still answers the empty set, and says so."""
        marker = 'SYNTHETIC_WALK_MARKER_71'

        class Raising(set):
            def __iter__(self):
                raise ValueError('walk diagnostic: ' + marker)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        H.log.addHandler(handler)
        try:
            answer = configured('page_methods', lambda: Raising({'session.turn'})).page_methods()
        finally:
            H.log.removeHandler(handler)
        self.assertEqual(answer, frozenset())
        self.assertIn('page_methods failed while reading the names', stream.getvalue())
        self.assertNotIn(marker, stream.getvalue())

    def test_a_credential_failure_omits_the_type_name_and_the_caller_traceback(self):
        """The log does not carry the source's type name, and the caller's traceback does not carry the text."""
        marker = 'SYNTHETIC_DIAGNOSTIC_MARKER_71'
        type_marker = 'SYNTHETIC_TYPE_MARKER_71'
        boom = type(type_marker, (Exception,), {})

        def source(connection, env_name):
            raise boom('decoding diagnostic: ' + marker)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        H.log.addHandler(handler)
        try:
            with self.assertRaises(KeyError) as caught:
                configured('credentials', source).credentials('connection-label', 'ENV_LABEL')
        finally:
            H.log.removeHandler(handler)
        self.assertEqual(caught.exception.args, ('connection-label',))
        logged = stream.getvalue()
        self.assertTrue(logged, 'the failure must still be logged')
        self.assertNotIn(marker, logged)
        self.assertNotIn(type_marker, logged)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)
        formatted = ''.join(traceback.format_exception(caught.exception))
        self.assertNotIn(marker, formatted)
        self.assertNotIn(type_marker, formatted)

    def test_a_page_name_whose_type_is_not_str_is_left_out(self):
        """A name that is a str subclass is not a str. It must not make another method look allowed."""

        class Tricky(str):
            def __eq__(self, other):
                return other == 'run.stop' or other == 'session.turn' or str.__eq__(self, other)

            def __hash__(self):
                return hash('run.stop')

        answer = configured('page_methods', lambda: {Tricky('session.turn')}).page_methods()
        self.assertEqual(answer, frozenset())
        self.assertNotIn('run.stop', answer)

    def test_an_empty_credential_is_refused_even_when_it_claims_to_be_present(self):
        """Empty text is not a credential, including a subclass that says it is present."""

        class Blank(str):
            def __new__(cls):
                return str.__new__(cls, '')

            def __bool__(self):
                return True

        hooks = configured('credentials', lambda connection, env_name: Blank())
        with self.assertRaises(KeyError) as caught:
            hooks.credentials('connection-label', 'ENV_LABEL')
        self.assertEqual(caught.exception.args, ('connection-label',))

    def test_a_drive_label_that_raises_still_declines_the_owner(self):
        """Reading the run label must not replace the decline. The caller still gets no owner."""

        class Ctx:
            @property
            def run_id(self):
                raise RuntimeError('label failed')

        class Owner:
            run = None

        hooks = configured('drive_owner', lambda context, request: Owner())
        self.assertIsNone(hooks.drive_owner(Ctx(), {}))


if __name__ == '__main__':
    unittest.main()
