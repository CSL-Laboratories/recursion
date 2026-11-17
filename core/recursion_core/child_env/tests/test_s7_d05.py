import inspect
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.child_env import ChildRoute, EnvRefused, build_child_env, policy


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='s7-child-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.bases = mock.patch.object(policy, '_tmp_bases', return_value=[str(self.root)])
        self.bases.start()
        self.addCleanup(self.bases.stop)
        self.cache = mock.patch.dict(policy._TMP, {}, clear=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def build(self, **kw):
        return build_child_env(kw.pop('engine', 'claude_code'), home=kw.pop('home', str(self.home)),
                               auth_mode=kw.pop('auth_mode', 'subscription'), parent=kw.pop('parent', {}), **kw)

    def interleave(self, function, source_line, mutate, call):
        """Select one ordinary-container interleaving; no scheduling sleeps."""
        lines, first = inspect.getsourcelines(function)
        target = next(first + i for i, s in enumerate(lines) if s.strip() == source_line)
        entered, done = threading.Event(), threading.Event()
        errors = []

        def worker():
            try:
                if not entered.wait(3):
                    raise AssertionError('interleaving point was not reached')
                mutate()
            except BaseException as e:
                errors.append(e)
            finally:
                done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        fired = False

        def trace(frame, event, arg):
            nonlocal fired
            if not fired and event == 'line' and frame.f_code is function.__code__ and frame.f_lineno == target:
                fired = True
                entered.set()
                if not done.wait(3):
                    raise AssertionError('mutator missed bounded deadline')
            return trace

        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            return call()
        finally:
            sys.settrace(previous)
            entered.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(fired)


@unittest.skipIf(os.name == "nt", "POSIX rules")
class Defects(Fixture):
    def test_D05_provider_list_snapshot(self):
        switches = ['CLAUDE_CODE_USE_BEDROCK']
        route = ChildRoute(provider_switches=switches)
        env = self.interleave(policy._route_env, 'for name in switches:', switches.clear,
                              lambda: self.build(auth_mode='cloud_provider', route=route))
        self.assertTrue(any(env.get(k) == '1' for k in policy.ENGINE_PROFILES['claude_code'].provider_switches),
                        'accepted cloud_provider with no selected provider switch')


if __name__ == "__main__":
    unittest.main(verbosity=2)
