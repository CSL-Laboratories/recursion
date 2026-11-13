"""What the cgroup v2 leaf launcher does on Windows: a typed refusal, asserted on a real Windows host.

The launcher is Linux-only (cgroup v2, pidfd, uid ownership). Its Linux suites skip on Windows, so
this file is what the Windows gate asserts about it. On a POSIX host the same class runs under the
simulation in child_env/tests/test_simulated_windows.py. Nothing is launched.
"""
from pathlib import Path
import os
import tempfile
import unittest

from recursion_core.cgroup_leaf import (Cgroup2, DelegatedRoot, LaunchError, LeafRun,
                                        PlatformUnsupported, require_linux_calls)

WINDOWS_ONLY = ('Windows-only assertions; a POSIX host runs them simulated '
                '(child_env/tests/test_simulated_windows.py)')


@unittest.skipUnless(os.name == 'nt', WINDOWS_ONLY)
class WindowsRefusalTests(unittest.TestCase):
    def test_the_refusal_is_typed_and_names_the_platform(self):
        self.assertTrue(issubclass(PlatformUnsupported, LaunchError))
        with self.assertRaisesRegex(PlatformUnsupported, 'platform_unsupported'):
            require_linux_calls()

    def test_every_entry_point_refuses_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(PlatformUnsupported):
                Cgroup2(root)
            with self.assertRaises(PlatformUnsupported):
                DelegatedRoot(root / 'delegated')
            with self.assertRaises(PlatformUnsupported):
                DelegatedRoot(root / 'delegated', fs=object())
            with self.assertRaises(PlatformUnsupported):
                LeafRun(object(), root / 'run-x', 1.0)
            self.assertEqual([], list(root.iterdir()))


if __name__ == '__main__':
    unittest.main()
