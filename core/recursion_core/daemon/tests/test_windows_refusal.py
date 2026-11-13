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
from recursion_core.daemon import __main__ as entry
from recursion_core.daemon import breaker, control_adapter, launcher, plugins
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

    def test_launcher_imports_without_fcntl_or_termios_and_refuses_typed(self):
        self.assertIsNone(launcher.fcntl)
        self.assertIsNone(launcher.termios)
        with self.assertRaisesRegex(PlatformUnsupported, 'platform_unsupported'):
            launcher.require_launcher_platform()
        for build in (lambda: launcher.EngineUnitManager(self.path / 'cgroup'),
                      lambda: launcher.ProductionLauncher('claude_code', object(), object(), self.path / 'state'),
                      lambda: launcher.UnitEngineProcess(object(), 'run-1', 0, self.path / 'stdout', None),
                      lambda: launcher.UnitKillTree(object()),
                      lambda: launcher.private_dir(self.path / 'private')):
            with self.assertRaises(PlatformUnsupported):
                build()
        self.assertEqual([], list(self.path.iterdir()))

    def test_composition_refuses_and_makes_no_socket_token_or_state(self):
        environ = {'XDG_RUNTIME_DIR': str(self.path / 'rt'), 'XDG_CONFIG_HOME': str(self.path / 'config')}
        with self.assertRaisesRegex(PlatformUnsupported, 'platform_unsupported'):
            control_adapter.compose_control(object(), self.path / 'state', environ=environ, routes={})
        with self.assertRaises(PlatformUnsupported):
            control_adapter.DeviceDoor(self.path / 'door')
        with self.assertRaises(PlatformUnsupported):
            control_adapter.load_routes(self.path / 'routes.json')
        self.assertEqual([], list(self.path.iterdir()))

    def test_plugin_host_and_breaker_store_refuse_and_make_no_file(self):
        (self.path / 'config' / 'plugins' / 'one').mkdir(parents=True)
        before = sorted(p.as_posix() for p in self.path.rglob('*'))
        with self.assertRaisesRegex(PlatformUnsupported, 'platform_unsupported'):
            plugins.PluginHost(self.path / 'config', self.path / 'state')
        with self.assertRaisesRegex(PlatformUnsupported, 'platform_unsupported'):
            plugins.PluginHost(self.path / 'config', self.path / 'state', plugin_dirs=(self.path / 'config',),
                               verification_available=lambda: False)
        with self.assertRaisesRegex(PlatformUnsupported, 'platform_unsupported'):
            breaker.open_breaker_store(self.path / 'state', clock=lambda: 0.0)
        self.assertEqual(before, sorted(p.as_posix() for p in self.path.rglob('*')))

    def test_daemon_entry_point_refuses_before_any_lock_state_or_gui(self):
        def never(*args, **kwargs):
            raise AssertionError('the daemon entry point went past the platform refusal')
        saved = {name: getattr(entry, name) for name in
                 ('require_free_threaded', 'check_operator_linger', 'UserLock', 'Registry', 'Daemon',
                  'compose_control', 'LoopbackGUI')}
        try:
            for name in saved:
                setattr(entry, name, never)
            with self.assertRaisesRegex(PlatformUnsupported, 'platform_unsupported'):
                entry.main(port=0)
            with self.assertRaisesRegex(PlatformUnsupported, 'platform_unsupported'):
                entry.main(client=object(), door=bool, port=0)
            with self.assertRaisesRegex(PlatformUnsupported, 'platform_unsupported'):
                entry.main(plugin_dirs=(str(self.path),), port=0)
        finally:
            for name, value in saved.items():
                setattr(entry, name, value)


if __name__ == '__main__':
    unittest.main()
