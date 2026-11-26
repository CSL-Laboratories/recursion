"""A bounded registration/freeze witness, using ordinary names and factories."""
import inspect
import sys
import threading
import unittest
from recursion_core.routes import engines

class EngineTableFreeze(unittest.TestCase):
    def test_freeze_returns_with_stable_names(self):
        table = engines.EngineTable()
        at_commit = threading.Event()
        release = threading.Event()
        freeze_started = threading.Event()
        frozen = threading.Event()
        snapshots, errors = [], []
        lines, first = inspect.getsourcelines(engines.EngineTable.register)
        commit_line = first + next(i for i, line in enumerate(lines) if 'self._factories[name] = factory' in line)
        target_code = engines.EngineTable.register.__code__
        def trace(frame, event, arg):
            if frame.f_code is target_code and event == 'line' and frame.f_lineno == commit_line:
                at_commit.set()
                if not release.wait(3):
                    raise AssertionError('registration gate timed out')
            return trace
        def register():
            try:
                sys.settrace(trace)
                table.register('pi', lambda *a, **kw: None)
            except engines.EngineTableError:
                pass  # Refusing a registration overtaken by freeze is also safe.
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                sys.settrace(None)
        def freeze():
            try:
                freeze_started.set()
                table.freeze()
                snapshots.append(table.names())
                frozen.set()
            except BaseException as exc:
                errors.append(repr(exc))
        registering = threading.Thread(target=register, daemon=True)
        freezing = threading.Thread(target=freeze, daemon=True)
        registering.start()
        try:
            self.assertTrue(at_commit.wait(3), 'register never reached commit')
            freezing.start()
            self.assertTrue(freeze_started.wait(3))
            frozen.wait(0.2)  # A locking implementation may wait for registration.
        finally:
            release.set()
            registering.join(3)
            if freezing.ident is not None:
                freezing.join(3)
        self.assertFalse(registering.is_alive())
        self.assertFalse(freezing.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(snapshots), 1)
        print('freeze snapshot:', snapshots[0], 'final:', table.names())
        self.assertEqual(snapshots[0], table.names(), 'frozen engine names changed after freeze returned')

    def test_readers_copy_the_table_under_the_registration_lock(self):
        """names, items and containment look at the dict only while the registration lock is held.

        A reader that does not can raise while a registration changes the dict, or return a torn tuple.
        """
        table = engines.EngineTable()
        inner = table._lock
        depth = [0]

        class Watched:
            def __enter__(self):
                depth[0] += 1
                return inner.__enter__()

            def __exit__(self, *exc):
                depth[0] -= 1
                return inner.__exit__(*exc)

        class Seeing(dict):
            def __iter__(self):
                under.append(('names', depth[0] > 0))
                return dict.__iter__(self)

            def items(self):
                under.append(('items', depth[0] > 0))
                return dict.items(self)

            def __contains__(self, name):
                under.append(('contains', depth[0] > 0))
                return dict.__contains__(self, name)

        under = []
        table._lock = Watched()
        table._factories = Seeing(table._factories)
        self.assertEqual(table.names(), ('claude_code', 'codex'))
        self.assertEqual([name for name, _factory in table.items()], ['claude_code', 'codex'])
        self.assertIn('claude_code', table)
        self.assertEqual(under, [('names', True), ('items', True), ('contains', True)])

    def test_a_name_that_is_not_a_str_is_refused_before_the_lock(self):
        """Equality or a text form that calls back must not run while the lock is held.

        On a table whose register holds the lock across those calls, this thread never returns.
        """
        table = engines.EngineTable()
        calls = []

        class CallsBack(str):
            def __eq__(self, other):
                calls.append('eq')
                table.freeze()
                return str.__eq__(self, other)

            def __hash__(self):
                return hash(str(self))

            def __repr__(self):
                calls.append('repr')
                table.register('pi', lambda *a, **k: None)
                return 'CallsBack'

        done = threading.Event()
        outcome = []

        def run():
            try:
                table.register(CallsBack('pi'), lambda *a, **k: None)
                outcome.append('accepted')
            except engines.EngineTableError:
                outcome.append('refused')
            except BaseException as exc:
                outcome.append(type(exc).__name__)
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(done.wait(2), 'register did not return; a name that is not a str took the lock')
        worker.join(1)
        self.assertEqual(outcome, ['refused'])
        self.assertEqual(calls, [], 'the name was asked for equality or text before the refusal')
        self.assertFalse(table._frozen)
        self.assertNotIn('pi', table.names())

    def test_an_interruption_at_the_store_does_not_burn_the_one_replacement(self):
        """The replacement is recorded only after the factory is stored.

        An interruption on the store line used to leave the name recorded and the old factory in place,
        so the one allowed replacement could never be asked for again.
        """
        table = engines.EngineTable()
        lines, first = inspect.getsourcelines(engines.EngineTable.register)
        store_line = first + next(
            i for i, line in enumerate(lines) if 'self._factories[name] = factory' in line)
        target = engines.EngineTable.register.__code__

        def trace(frame, event, arg):
            if frame.f_code is target and event == 'line' and frame.f_lineno == store_line:
                raise KeyboardInterrupt('synthetic interruption at the store')
            return trace

        sys.settrace(trace)
        try:
            try:
                table.register('codex', lambda *a, **k: 'lost', replace=True)
            except KeyboardInterrupt:
                pass
        finally:
            sys.settrace(None)
        marker = lambda *a, **k: 'kept'  # noqa: E731 - the identity of this one factory is the assertion
        table.register('codex', marker, replace=True)
        self.assertIs(dict(table.items())['codex'], marker)
        with self.assertRaises(engines.EngineTableError):
            table.register('codex', lambda *a, **k: 'again', replace=True)

    def test_the_lock_comment_does_not_say_freeze_snapshots_the_table(self):
        text = inspect.getsource(engines.EngineTable.__init__)
        self.assertNotIn('freeze cannot snapshot', text)
        self.assertIn('does not take a snapshot', text)


if __name__ == '__main__':
    print('interpreter:', sys.version, 'gil_enabled:', getattr(sys, '_is_gil_enabled', lambda: True)())
    unittest.main(verbosity=2)
