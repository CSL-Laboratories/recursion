"""What the user daemon does on Windows: a typed refusal, asserted on a real Windows host.

The daemon is Linux-only (flock, uid ownership checks, systemd user units). Its Linux suites skip
on Windows, so this file is what the Windows gate asserts about it. On a POSIX host the same class
runs under the simulation in child_env/tests/test_simulated_windows.py. Nothing is started.
"""
from pathlib import Path
import os
import tempfile
import unittest

import recursion_core.daemon as daemon
from recursion_core.daemon import (Daemon, PlatformUnsupported, Registry, SystemdUserManager,
                                   UserLock, require_posix)
from recursion_core.daemon.engine_user import EngineManagerPresent, check_engine_no_user_manager

WINDOWS_ONLY = ('Windows-only assertions; a POSIX host runs them simulated '
                '(child_env/tests/test_simulated_windows.py)')


@unittest.skipUnless(os.name == 'nt', WINDOWS_ONLY)
class WindowsRefusalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_import_survives_without_fcntl_and_the_refusal_is_typed(self):
        self.assertIsNone(daemon.fcntl)
        self.assertTrue(issubclass(PlatformUnsupported, RuntimeError))
        with self.assertRaisesRegex(PlatformUnsupported, 'platform_unsupported'):
            require_posix()

    def test_user_lock_refuses_and_creates_no_lock_file(self):
        lock = UserLock(self.path / 'runtime')
        with self.assertRaises(PlatformUnsupported):
            lock.__enter__()
        self.assertIsNone(lock.fd)
        self.assertEqual([], list(self.path.iterdir()))

    def test_registry_refuses_and_creates_no_state(self):
        with self.assertRaises(PlatformUnsupported):
            Registry(self.path / 'state')
        self.assertEqual([], list(self.path.iterdir()))

    def test_daemon_and_systemd_manager_refuse_on_construction(self):
        with self.assertRaises(PlatformUnsupported):
            SystemdUserManager(self.path)
        with self.assertRaises(PlatformUnsupported):
            Daemon(object(), object())

    def test_engine_probe_refuses_without_an_operator_uid(self):
        def call(*args, **kwargs):
            raise AssertionError('probe ran without an operator uid')
        with self.assertRaisesRegex(EngineManagerPresent, 'platform_unsupported'):
            check_engine_no_user_manager('csl-engine', 1001, call=call, runtime_root=self.path)


if __name__ == '__main__':
    unittest.main()
