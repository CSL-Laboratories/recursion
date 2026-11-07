"""Fake-manager tests for the CSL-738 daemon. Not runtime or live-host certification."""
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from recursion_core.daemon import (AlreadyRunning, Daemon, Registry, UserLock,
                                   SystemdUserManager, check_linger, require_free_threaded)
from recursion_core.daemon.__main__ import linger_users

UNIT_FILE = Path(__file__).resolve().parents[3] / 'systemd' / 'recursion-daemon.service'


class FakeManager:
    """Stands in for the systemd user manager. Units persist across daemon objects."""
    def __init__(self):
        self.units = {}
        self.stops = []
        self.starts = []
        self.fail_stop = False
        self.unload_on_stop = False

    def start(self, unit, argv):
        self.starts.append(unit)
        self.units[unit] = 'running'

    def stop(self, unit):
        self.stops.append(unit)
        if self.fail_stop:
            raise OSError('interrupted')
        if self.unload_on_stop:
            self.units.pop(unit, None)  # a stopped transient unit is garbage-collected
        else:
            self.units[unit] = 'empty'

    def observe(self, unit):
        return self.units.get(unit, 'unknown')

    def owned_units(self):
        return set(self.units)


class Boot:
    def __init__(self, value='boot-a'):
        self.value = value

    def __call__(self):
        return self.value


def lock_child(path, ready):
    with UserLock(Path(path)):
        ready.send(True)
        signal.pause()


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.registry = Registry(self.path / 'state')
        self.manager = FakeManager()
        self.boot = Boot()
        self.daemon = Daemon(self.registry, self.manager, self.boot)

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    def restart(self):
        """Simulate a daemon restart: drop in-memory state, reopen the durable registry."""
        self.registry.close()
        self.registry = Registry(self.path / 'state')
        self.daemon = Daemon(self.registry, self.manager, self.boot)
        return self.daemon

    # Launch and registry
    def test_start_registered_before_launch(self):
        def start(unit, argv):
            self.assertEqual(self.registry.get('one').state, 'starting')
            self.manager.units[unit] = 'running'
        self.manager.start = start
        self.assertEqual(self.daemon.start('one', ['fake']).state, 'running')

    def test_launch_error_preserved_and_recorded_unknown(self):
        self.manager.start = Mock(side_effect=OSError('failed'))
        with self.assertRaisesRegex(OSError, 'failed'):
            self.daemon.start('one', ['fake'])
        run = self.registry.get('one')
        self.assertEqual(run.state, 'unknown')
        self.assertIn('launch error', run.detail)

    def test_launch_error_survives_failing_reconcile(self):
        self.manager.start = Mock(side_effect=ValueError('original'))
        self.manager.observe = Mock(side_effect=RuntimeError('observer broke'))
        with self.assertRaisesRegex(ValueError, 'original'), self.assertLogs(level='ERROR'):
            self.daemon.start('one', ['fake'])

    def test_unreadable_boot_id_refuses_start(self):
        self.boot.value = None
        with self.assertRaises(RuntimeError):
            self.daemon.start('one', ['fake'])
        self.assertEqual(self.registry.all(), [])
        self.assertEqual(self.manager.starts, [])

    def test_duplicate_run_refused(self):
        self.registry.add('one', 'b')
        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.add('one', 'b')

    def test_invalid_run_id_refused(self):
        for bad in ('../escape', '', 'a' * 65, 'a.service', 'a b'):
            with self.assertRaises(ValueError):
                self.registry.add(bad, 'b')

    def test_missing_run_is_key_error(self):
        with self.assertRaises(KeyError):
            self.registry.get('none')
        with self.assertRaises(KeyError):
            self.daemon.stop('none')

    def test_registry_private_directory_and_file(self):
        bad = self.path / 'bad'
        bad.mkdir(mode=0o755)
        with self.assertRaises(PermissionError):
            Registry(bad)
        self.assertEqual((self.path / 'state' / 'runs.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_registry_symlink_refused(self):
        (self.path / 'link').symlink_to(self.path / 'state')
        with self.assertRaises(PermissionError):
            Registry(self.path / 'link')

    def test_unknown_schema_refused(self):
        self.registry.close()
        db = sqlite3.connect(self.path / 'state' / 'runs.sqlite3')
        db.execute('PRAGMA user_version=99')
        db.close()
        with self.assertRaises(RuntimeError):
            Registry(self.path / 'state')
        self.registry = Registry(self.path / 'other')

    # Survival: clean restart, kill -9, supervisor death (fake manager)
    def test_clean_restart_re_adopts(self):
        self.daemon.start('one', ['fake'])
        self.daemon.close()
        self.assertEqual(self.manager.stops, [])
        self.assertEqual(self.restart().sweep()[0].state, 'running')

    def test_daemon_loss_does_not_stop_scope(self):
        self.daemon.start('one', ['fake'])
        del self.daemon  # no close: models kill -9 or supervisor death
        self.assertEqual(self.restart().sweep()[0].state, 'running')
        self.assertEqual(self.manager.stops, [])
        self.assertEqual(self.manager.starts, ['recursion-run-one.service'])

    def test_crash_between_registry_and_launch_is_unknown(self):
        self.registry.add('one', 'boot-a')  # committed, then the daemon died before systemd-run
        run = self.restart().sweep()[0]
        self.assertEqual(run.state, 'unknown')
        self.assertEqual(self.manager.starts, [])

    # Stop intent (B14 shape, fake)
    def test_stop_intent_precedes_signal(self):
        self.daemon.start('one', ['fake'])
        original = self.manager.stop
        def stop(unit):
            self.assertTrue(self.registry.get('one').stop_requested)
            original(unit)
        self.manager.stop = stop
        self.assertEqual(self.daemon.stop('one').state, 'terminated')

    def test_crash_after_stop_intent_never_re_adopted_running(self):
        self.daemon.start('one', ['fake'])
        self.registry.request_stop('one')  # committed; daemon dies before signalling
        self.manager.stop = Mock()  # replayed stop that has not yet taken effect
        run = self.restart().sweep()[0]
        self.assertTrue(run.stop_requested)
        self.assertEqual(run.state, 'stopping')
        self.manager.stop.assert_called_with('recursion-run-one.service')

    def test_interrupted_stop_replays_after_restart(self):
        self.daemon.start('one', ['fake'])
        self.manager.fail_stop = True
        self.assertEqual(self.daemon.stop('one').state, 'unknown')
        self.manager.fail_stop = False
        result = self.restart().sweep()[0]
        self.assertTrue(result.stop_requested)
        self.assertEqual(result.state, 'terminated')

    def test_populated_stop_never_terminated(self):
        self.daemon.start('one', ['fake'])
        self.manager.stop = lambda unit: None
        self.assertEqual(self.daemon.stop('one').state, 'stopping')

    def test_terminated_is_sticky_after_unit_unloads(self):
        self.manager.unload_on_stop = False
        self.daemon.start('one', ['fake'])
        self.assertEqual(self.daemon.stop('one').state, 'terminated')
        self.manager.units.clear()  # systemd garbage-collected the transient unit
        self.assertEqual(self.daemon.sweep()[0].state, 'terminated')

    def test_natural_exit_terminated_and_unit_released(self):
        self.daemon.start('one', ['fake'])
        self.manager.units['recursion-run-one.service'] = 'empty'
        self.assertEqual(self.daemon.sweep()[0].state, 'terminated')
        self.assertEqual(self.manager.stops, ['recursion-run-one.service'])

    # Reboot: registry persists; processes do not
    def test_reboot_is_lost_not_terminated_and_not_relaunched(self):
        self.daemon.start('one', ['fake'])
        self.manager.units.clear()
        self.boot.value = 'boot-b'
        run = self.restart().sweep()[0]
        self.assertEqual(run.state, 'lost_reboot')
        self.assertIn('boot-a', run.detail)
        self.assertEqual(self.manager.starts, ['recursion-run-one.service'])

    def test_reboot_with_pending_stop_never_running(self):
        self.daemon.start('one', ['fake'])
        self.registry.request_stop('one')
        self.boot.value = 'boot-b'
        run = self.restart().sweep()[0]
        self.assertEqual((run.state, run.stop_requested), ('lost_reboot', True))
        self.manager.units['recursion-run-one.service'] = 'running'  # foreign same-name unit
        self.assertEqual(self.daemon.sweep()[0].state, 'lost_reboot')

    def test_same_boot_missing_unit_is_unknown(self):
        self.daemon.start('one', ['fake'])
        self.manager.units.clear()
        self.assertEqual(self.daemon.sweep()[0].state, 'unknown')

    def test_unreadable_boot_id_is_unknown(self):
        self.daemon.start('one', ['fake'])
        self.boot.value = None
        self.assertEqual(self.daemon.sweep()[0].state, 'unknown')

    # Orphan sweep
    def test_orphan_stopped_only_reserved_namespace(self):
        self.manager.units.update({'recursion-run-orphan.service': 'running',
                                   'other.service': 'running',
                                   'recursion-run-../x.service': 'running'})
        self.daemon.sweep()
        self.assertEqual(self.manager.stops, ['recursion-run-orphan.service'])

    def test_unknown_run_unit_left_alone(self):
        self.daemon.start('one', ['fake'])
        self.manager.units['recursion-run-one.service'] = 'weird'
        self.assertEqual(self.daemon.sweep()[0].state, 'unknown')
        self.assertEqual(self.manager.stops, [])

    def test_sweep_survives_manager_failures(self):
        self.daemon.start('one', ['fake'])
        self.manager.owned_units = Mock(side_effect=subprocess.CalledProcessError(1, 'x'))
        with self.assertLogs(level='ERROR'):
            self.assertEqual(self.daemon.sweep()[0].state, 'running')
        self.manager.owned_units = lambda: {'recursion-run-orphan.service'}
        self.manager.fail_stop = True
        with self.assertLogs(level='ERROR'):
            self.daemon.sweep()

    # Shutdown and concurrency
    def test_closed_daemon_refuses_calls(self):
        self.daemon.close()
        for call in (lambda: self.daemon.start('one', ['x']), self.daemon.sweep,
                     lambda: self.daemon.stop('one')):
            with self.assertRaises(RuntimeError):
                call()

    def test_concurrent_starts_and_sweeps(self):
        errors = []
        def work(n):
            try:
                self.daemon.start(f'r{n}', ['fake'])
                self.daemon.sweep()
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=work, args=(n,)) for n in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual({r.state for r in self.registry.all()}, {'running'})
        self.assertEqual(len(self.registry.all()), 16)
        self.assertEqual(self.manager.stops, [])

    # Singleton lock
    def test_singleton_and_stale_file(self):
        with UserLock(self.path):
            with self.assertRaises(AlreadyRunning):
                with UserLock(self.path):
                    pass
        with UserLock(self.path):
            pass

    def test_kernel_releases_lock_on_sigkill(self):
        receiver, sender = multiprocessing.Pipe(False)
        process = multiprocessing.Process(target=lock_child, args=(str(self.path), sender))
        process.start()
        try:
            self.assertTrue(receiver.poll(5))
            self.assertTrue(receiver.recv())
            with self.assertRaises(AlreadyRunning):
                with UserLock(self.path):
                    pass
            os.kill(process.pid, signal.SIGKILL)
            process.join(5)
            with UserLock(self.path):
                pass
        finally:
            if process.is_alive():
                process.kill()
            process.join()
            receiver.close()
            sender.close()

    def test_different_user_runtime_namespaces(self):
        with UserLock(self.path / 'user-a'), UserLock(self.path / 'user-b'):
            pass

    def test_symlink_lock_refused(self):
        (self.path / 'recursion-daemon.lock').symlink_to(self.path / 'target')
        with self.assertRaises(OSError):
            with UserLock(self.path):
                pass


class RuntimeTests(unittest.TestCase):
    def test_gil_build_refused(self):
        with patch('recursion_core.daemon.sysconfig.get_config_var', return_value=0):
            with self.assertRaises(RuntimeError):
                require_free_threaded()

    def test_gil_enabled_after_import_refused(self):
        with patch('recursion_core.daemon.sys.version_info', (3, 14)), \
             patch('recursion_core.daemon.sysconfig.get_config_var', return_value=1), \
             patch('recursion_core.daemon.sys._is_gil_enabled', return_value=True, create=True):
            with self.assertRaises(RuntimeError):
                require_free_threaded()

    def test_free_threaded_runtime_accepted(self):
        with patch('recursion_core.daemon.sys.version_info', (3, 14)), \
             patch('recursion_core.daemon.sysconfig.get_config_var', return_value=1), \
             patch('recursion_core.daemon.sys._is_gil_enabled', return_value=False, create=True):
            require_free_threaded()

    def test_linger_checks_both_users(self):
        call = Mock(return_value=Mock(stdout='yes\n'))
        check_linger(['operator', 'engine'], call)
        self.assertEqual(call.call_count, 2)
        self.assertTrue(all('enable-linger' not in c.args[0] for c in call.call_args_list))

    def test_linger_no_refused(self):
        with self.assertRaises(RuntimeError):
            check_linger(['operator'], Mock(return_value=Mock(stdout='no')))

    def test_linger_users_engine_when_present(self):
        with patch('recursion_core.daemon.__main__.getpass.getuser', return_value='op'):
            self.assertEqual(linger_users({'RECURSION_ENGINE_USER': 'eng'}, Mock()), ['op', 'eng'])
            self.assertEqual(linger_users({}, Mock()), ['op'])
            with self.assertLogs(level='WARNING'):
                self.assertEqual(linger_users({'RECURSION_ENGINE_USER': 'eng'},
                                              Mock(side_effect=KeyError('eng'))), ['op'])

    def test_unit_file(self):
        text = UNIT_FILE.read_text()
        self.assertIn('-X gil=0', text)
        self.assertIn('python3.14t', text)
        self.assertIn('Restart=on-failure', text)
        for forbidden in ('Delegate=', 'PartOf', 'BindsTo', 'enable-linger', 'PDEATHSIG'):
            self.assertNotIn(forbidden, text)


class SystemdManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = SystemdUserManager(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def show(self, group, active='active'):
        self.manager._call = Mock(return_value=f'LoadState=loaded\nActiveState={active}\n'
                                               f'ControlGroup={group}\n')

    def events(self, group, populated):
        path = self.root / group.lstrip('/')
        path.mkdir(parents=True)
        (path / 'cgroup.events').write_text(f'populated {populated}\nfrozen 0\n')

    def test_launch_is_independent_unit(self):
        self.manager._call = Mock(return_value='')
        self.manager.start('recursion-run-one.service', ['/fake'])
        args = self.manager._call.call_args.args[0]
        self.assertIn('--user', args)
        self.assertIn('--property=KillMode=control-group', args)
        self.assertFalse(any('PartOf' in a or 'BindsTo' in a for a in args))
        self.assertEqual(args[args.index('--') + 1:], ['/fake'])

    def test_empty_launch_refused(self):
        with self.assertRaises(ValueError):
            self.manager.start('recursion-run-one.service', [])

    def test_missing_control_group_unknown(self):
        self.manager._call = Mock(return_value='LoadState=not-found\nControlGroup=\n')
        self.assertEqual(self.manager.observe('missing'), 'unknown')

    def test_populated_zero_is_empty(self):
        self.events('/user.slice/a.service', 0)
        self.show('/user.slice/a.service')
        self.assertEqual(self.manager.observe('a'), 'empty')

    def test_populated_one_active_is_running(self):
        self.events('/user.slice/a.service', 1)
        self.show('/user.slice/a.service')
        self.assertEqual(self.manager.observe('a'), 'running')

    def test_populated_one_deactivating_is_unknown(self):
        self.events('/user.slice/a.service', 1)
        self.show('/user.slice/a.service', 'deactivating')
        self.assertEqual(self.manager.observe('a'), 'unknown')

    def test_traversal_and_unreadable_events_unknown(self):
        self.show('/user.slice/../../etc')
        self.assertEqual(self.manager.observe('a'), 'unknown')
        self.show('/user.slice/absent.service')
        self.assertEqual(self.manager.observe('a'), 'unknown')


if __name__ == '__main__':
    unittest.main()
