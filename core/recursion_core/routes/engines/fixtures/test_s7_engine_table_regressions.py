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

if __name__ == '__main__':
    print('interpreter:', sys.version, 'gil_enabled:', getattr(sys, '_is_gil_enabled', lambda: True)())
    unittest.main(verbosity=2)
