"""Fake-manager tests for the CSL-738 daemon. Not runtime or live-host certification."""
import errno
import multiprocessing
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from recursion_core.daemon import (AlreadyRunning, CGROUP_WALK_MAX_NODES, Daemon, Registry,
                                   RegistryDurabilityError, RegistryIntegrityError,
                                   UserLock, SystemdUserManager, UnitNotLoaded,
                                   check_operator_linger, refuse_engine_user,
                                   require_free_threaded)

NOT_POSIX = os.name == 'nt' or not hasattr(os, 'getuid')
LINUX_ONLY = ('Linux-only: the user daemon needs flock, uid ownership checks and systemd; it refuses '
              'PlatformUnsupported (platform_unsupported) on Windows; covered by WindowsRefusalTests')

UNIT_FILE = Path(__file__).resolve().parents[3] / 'systemd' / 'recursion-daemon.service'

# A legacy registry left by kill -9: the WAL still holds committed frames because
# SQLite only deletes it when the last connection closes.
_LEAVE_UNCHECKPOINTED_WAL = r'''
import os, sqlite3, sys
from pathlib import Path
directory = Path(sys.argv[1])
conn = sqlite3.connect(directory / "runs.sqlite3")
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("PRAGMA synchronous=FULL")
conn.execute(
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, unit TEXT UNIQUE NOT NULL, "
    "stop INTEGER NOT NULL, state TEXT NOT NULL, boot_id TEXT, detail TEXT NOT NULL, "
    "created REAL NOT NULL, updated REAL NOT NULL)")
conn.execute("PRAGMA user_version=1")
conn.execute(
    "INSERT INTO runs VALUES ('one','recursion-run-one.service',0,'starting','boot-a','',1,1)")
conn.commit()
conn.execute(
    "INSERT INTO runs VALUES ('tail','recursion-run-tail.service',0,'starting','boot-a','',2,2)")
conn.commit()
os._exit(0)
'''

# Real os.replace, then the process dies the way kill -9 does: no finally, no close.
_CRASH_AFTER_REAL_RENAME = r'''
import os, sys
from pathlib import Path
import recursion_core.daemon as daemon
from recursion_core.daemon import Registry
directory = Path(sys.argv[1])
real_replace = daemon.os.replace

def crash_after_real_replace(src, dst, *args, **kwargs):
    real_replace(src, dst, *args, **kwargs)
    os._exit(73)

daemon.os.replace = crash_after_real_replace
Registry(directory).add("two", "boot-a")
os._exit(0)
'''


class FakeManager:
    """Stands in for the systemd user manager. Units persist across daemon objects.

    ``listeners`` are processes this fake still attests alive after the unit record
    has been collected. ``processes_gone`` is that attestation; a scripted
    ``observe`` return value is not.
    """
    def __init__(self):
        self.units = {}
        self.listeners = set()
        self.stops = []
        self.starts = []
        self.fail_stop = False

    def start(self, unit, argv):
        self.starts.append(unit)
        self.units[unit] = 'running'

    def stop(self, unit):
        """Like systemctl stop: waits for inactive; the cgroup and transient unit go away."""
        self.stops.append(unit)
        if self.fail_stop:
            raise OSError('interrupted')
        if unit not in self.units:
            raise UnitNotLoaded(unit)  # systemctl stop: exit 5, "Unit <unit> not loaded."
        del self.units[unit]

    def observe(self, unit):
        return self.units.get(unit, 'gone')  # not-found, as systemd reports it

    def processes_gone(self, unit):
        if unit in self.listeners:
            return False
        return self.units.get(unit) != 'running'

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


@unittest.skipIf(NOT_POSIX, LINUX_ONLY)
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
        # A directory we own at 0755 is tightened to 0700. It is not refused.
        loose = self.path / 'loose'
        loose.mkdir(mode=0o755)
        self.assertEqual(loose.stat().st_mode & 0o777, 0o755)
        opened = Registry(loose)
        self.addCleanup(opened.close)
        self.assertEqual(loose.stat().st_mode & 0o777, 0o700)
        self.assertEqual((loose / 'runs.sqlite3').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.path / 'state' / 'runs.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_umask_000_state_and_registry_files_are_private(self):
        # umask only clears bits. mkdir(parents=True) creates missing parents at
        # 0777, and sqlite's own files are created at 0644, so umask 000 leaves
        # world-writable directories and world-readable registry sidecars.
        parent = self.path / 'new-state'
        directory = parent / 'recursion'
        previous = os.umask(0)
        try:
            registry = Registry(directory)
            registry.add('one', 'boot-a')
            paths = [parent, directory, *directory.iterdir()]
            modes = {path: path.stat().st_mode & 0o777 for path in paths}
            registry.close()
        finally:
            os.umask(previous)
        self.assertTrue(modes)
        for path, mode in modes.items():
            self.assertFalse(mode & 0o002, f'{path} is world-writable ({oct(mode)})')
            if path.is_dir():
                self.assertEqual(mode, 0o700, path)
            else:
                self.assertEqual(mode, 0o600, path)

    def test_scripted_end_or_live_listener_is_not_terminated(self):
        self.daemon.start('one', ['fake'])
        unit = 'recursion-run-one.service'
        # Scripted observation: the fake process is still running, but observe()
        # claims the unit has ended.
        self.manager.observe = lambda _unit: 'empty'
        self.assertFalse(self.manager.processes_gone(unit))
        self.assertNotEqual(self.daemon.sweep()[0].state, 'terminated')
        # The unit record is gone and observe agrees, but a listener is still alive.
        self.manager.listeners.add(unit)
        self.manager.units.clear()
        self.manager.observe = lambda _unit: 'gone'
        self.manager.stop = Mock(side_effect=UnitNotLoaded(unit))
        stopped = self.daemon.stop('one')
        self.assertNotEqual(stopped.state, 'terminated')
        self.assertFalse(self.manager.processes_gone(unit))
        # Once the listener is actually gone, the same observation may terminate.
        self.manager.listeners.clear()
        self.manager.observe = lambda _unit: 'empty'
        self.assertTrue(self.manager.processes_gone(unit))
        self.assertEqual(self.daemon.sweep()[0].state, 'terminated')

    def test_torn_registry_and_enospc_do_not_drop_or_trust_partial(self):
        directory = self.path / 'durable'
        registry = Registry(directory)
        registry.add('one', 'boot-a')
        registry.close()
        target = directory / 'runs.sqlite3'
        good = target.read_bytes()
        # kill -9 while a replacement file is half written: the partial temp is
        # not the registry, and the previous file is still the registry.
        (directory / '.runs.sqlite3.tmp-killed').write_bytes(good[:32])
        reopened = Registry(directory)
        self.assertEqual([run.run_id for run in reopened.all()], ['one'])
        before = target.read_bytes()
        with patch('recursion_core.daemon.os.replace',
                   side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            with self.assertRaises(OSError) as caught:
                reopened.add('two', 'boot-a')
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(target.read_bytes(), before)
        reopened.close()
        survived = Registry(directory)
        self.assertEqual([run.run_id for run in survived.all()], ['one'])
        survived.close()
        target.write_bytes(b'')
        with self.assertRaises(RegistryIntegrityError):
            Registry(directory)
        self.assertEqual(target.read_bytes(), b'')
        target.write_bytes(good[:64])
        with self.assertRaises(RegistryIntegrityError):
            Registry(directory)

    def _python_env(self):
        env = os.environ.copy()
        core = str(Path(__file__).resolve().parents[3])
        env['PYTHONPATH'] = core + os.pathsep + env.get('PYTHONPATH', '')
        env['PYTHONUNBUFFERED'] = '1'
        return env

    def test_crash_after_rename_does_not_replay_a_stale_wal(self):
        # Uncheckpointed WAL from a killed writer, then a crash in the window
        # after the new file is renamed into place and before the old -wal/-shm
        # is removed. SQLite would replay that WAL onto the new file.
        directory = self.path / 'legacy-wal'
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        env = self._python_env()
        left = subprocess.run([sys.executable, '-c', _LEAVE_UNCHECKPOINTED_WAL, str(directory)],
                              capture_output=True, text=True, env=env)
        self.assertEqual(left.returncode, 0, left.stderr)
        self.assertGreater((directory / 'runs.sqlite3-wal').stat().st_size, 0)
        crashed = subprocess.run([sys.executable, '-c', _CRASH_AFTER_REAL_RENAME, str(directory)],
                                 capture_output=True, text=True, env=env)
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        # The old journal must already be gone. Reopening must not be what
        # deletes it: a WAL left beside the new file is replayed onto it.
        self.assertFalse((directory / 'runs.sqlite3-wal').exists())
        self.assertFalse((directory / 'runs.sqlite3-shm').exists())
        reopened = Registry(directory)
        self.addCleanup(reopened.close)
        self.assertEqual([run.run_id for run in reopened.all()], ['one', 'tail', 'two'])

    def test_reopen_failure_after_publish_is_typed(self):
        self.registry.add('one', 'boot-a')

        def fail_reopen(this, drop_sidecars=False):
            if this.db is not None:
                this.db.close()
                this.db = None
            raise sqlite3.OperationalError('unable to open database file')

        with patch.object(Registry, '_reopen', fail_reopen):
            with self.assertRaises(RegistryIntegrityError):
                self.registry.add('two', 'boot-a')
            with self.assertRaises(RegistryIntegrityError):
                self.registry.all()
            with self.assertRaises(RegistryIntegrityError):
                self.registry.get('one')
            with self.assertRaises(RegistryIntegrityError):
                self.registry.add('three', 'boot-a')
        # The rename had already published "two". A later open can use it.
        self.assertEqual([run.run_id for run in self.registry.all()], ['one', 'two'])
        reopened = Registry(self.path / 'state')
        self.addCleanup(reopened.close)
        self.assertEqual([run.run_id for run in reopened.all()], ['one', 'two'])

    def test_directory_fsync_failure_blocks_start_and_stop(self):
        # The rename is kept, but it is not an acknowledged commit. Start and
        # stop are external effects and do not run until a directory fsync works.
        with patch('recursion_core.daemon._fsync_dir',
                   side_effect=OSError(errno.EIO, 'Input/output error')):
            with self.assertRaises(RegistryDurabilityError) as caught:
                self.daemon.start('one', ['fake'])
            self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertEqual(self.manager.starts, [])
        self.assertEqual([run.run_id for run in self.registry.all()], ['one'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.daemon.start('one', ['fake'])
        self.assertEqual(self.manager.starts, [])
        self.daemon.start('two', ['fake'])
        self.assertEqual(self.manager.starts, ['recursion-run-two.service'])
        with patch('recursion_core.daemon._fsync_dir',
                   side_effect=OSError(errno.EDQUOT, 'Disk quota exceeded')):
            with self.assertRaises(RegistryDurabilityError) as caught:
                self.daemon.stop('two')
            self.assertEqual(caught.exception.errno, errno.EDQUOT)
        self.assertEqual(self.manager.stops, [])
        self.assertTrue(self.registry.get('two').stop_requested)
        # The failed stop left the directory unsynced. The same stop reaches
        # the unit only after a later directory fsync succeeds.
        self.assertEqual(self.daemon.stop('two').state, 'terminated')
        self.assertEqual(self.manager.stops, ['recursion-run-two.service'])
        with patch('recursion_core.daemon._fsync_dir',
                   side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            with self.assertRaises(RegistryDurabilityError):
                self.registry.add('three', 'boot-a')
        self.assertEqual([run.run_id for run in self.registry.all()], ['one', 'three', 'two'])

    def test_sweep_does_not_stop_while_directory_fsync_is_pending(self):
        # A Stop whose directory fsync failed must not be acted on by a later
        # read. all() and sweep() retry the fsync and raise before any signal.
        self.daemon.start('one', ['fake'])
        self.assertEqual(self.manager.starts, ['recursion-run-one.service'])
        with patch('recursion_core.daemon._fsync_dir',
                   side_effect=OSError(errno.EIO, 'Input/output error')):
            with self.assertRaises(RegistryDurabilityError):
                self.daemon.stop('one')
            self.assertFalse(self.registry._dir_durable)
            with self.assertRaises(RegistryDurabilityError):
                self.registry.all()
            fresh = Daemon(self.registry, self.manager, self.boot)
            with self.assertRaises(RegistryDurabilityError):
                fresh.sweep()
            self.assertEqual(self.manager.stops, [])
            self.assertEqual(self.manager.starts, ['recursion-run-one.service'])

    def test_open_removes_temp_files_left_by_dead_pids(self):
        dead = 1 << 22
        while dead < (1 << 22) + 1000:
            try:
                os.kill(dead, 0)
            except ProcessLookupError:
                break
            dead += 1
        else:
            self.fail('no unused pid')
        directory = self.path / 'state'
        stale = directory / f'.runs.sqlite3.tmp-{dead}-1'
        stale_wal = directory / f'.runs.sqlite3.tmp-{dead}-1-wal'
        stale.write_bytes(b'x' * 64)
        stale_wal.write_bytes(b'y' * 64)
        live = directory / f'.runs.sqlite3.tmp-{os.getpid()}-99'
        live.write_bytes(b'keep')
        reopened = Registry(directory)
        self.addCleanup(reopened.close)
        self.assertFalse(stale.exists())
        self.assertFalse(stale_wal.exists())
        self.assertTrue(live.exists())
        self.assertEqual([run.run_id for run in reopened.all()], [])

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
        self.daemon.start('one', ['fake'])
        self.assertEqual(self.daemon.stop('one').state, 'terminated')
        self.assertNotIn('recursion-run-one.service', self.manager.units)  # collected
        self.assertEqual(self.daemon.sweep()[0].state, 'terminated')

    def test_stop_of_already_collected_unit_is_ended(self):
        # X1a (t1430u): a recorded Stop that systemd answers "not loaded", with the unit
        # gone, is ended. Nothing runs under the unit; a later same-name unit is an orphan.
        self.daemon.start('one', ['fake'])
        self.manager.units.clear()
        run = self.daemon.stop('one')
        self.assertEqual(run.state, 'terminated')
        self.assertEqual(run.detail, 'stop: unit not loaded, unit gone')
        self.manager.units['recursion-run-one.service'] = 'running'  # late same-name unit
        self.assertEqual(self.daemon.sweep()[0].state, 'terminated')
        self.assertEqual(self.manager.stops[-1], 'recursion-run-one.service')

    def test_stop_that_ended_the_unit_before_a_crash_replays_to_terminated(self):
        # The earlier systemctl stop ended and collected the unit, then the daemon died
        # before recording it. The replayed Stop gets "not loaded"; it must not stick at unknown.
        self.daemon.start('one', ['fake'])
        self.registry.request_stop('one')
        del self.manager.units['recursion-run-one.service']  # stop took effect; record did not
        run = self.restart().sweep()[0]
        self.assertEqual((run.state, run.stop_requested), ('terminated', True))

    def test_not_loaded_stop_without_a_gone_unit_is_not_terminated(self):
        self.daemon.start('one', ['fake'])
        for seen, expected in (('unknown', 'unknown'), ('running', 'stopping')):
            with self.subTest(observe=seen):
                self.manager.stop = Mock(side_effect=UnitNotLoaded('recursion-run-one.service'))
                self.manager.observe = Mock(return_value=seen)
                self.assertEqual(self.daemon.stop('one').state, expected)

    def test_other_stop_errors_with_a_gone_unit_stay_unknown(self):
        self.daemon.start('one', ['fake'])
        self.manager.units.clear()
        for exc in (subprocess.CalledProcessError(5, ['systemctl', 'stop']),
                    subprocess.CalledProcessError(1, ['systemctl', 'stop'], stderr='Unit x not loaded'),
                    OSError('interrupted')):
            with self.subTest(exc=exc):
                self.manager.stop = Mock(side_effect=exc)
                self.assertEqual(self.daemon.stop('one').state, 'unknown')

    def test_absent_unit_without_a_stop_stays_unknown(self):
        self.daemon.start('one', ['fake'])
        self.manager.units.clear()
        self.assertEqual(self.daemon.sweep()[0].state, 'unknown')
        self.assertEqual(self.manager.stops, [])

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

    def test_operator_linger_read_only(self):
        call = Mock(return_value=Mock(stdout='yes\n'))
        check_operator_linger('operator', call)
        argv = call.call_args.args[0]
        self.assertEqual(argv[:3], ['loginctl', 'show-user', 'operator'])
        self.assertNotIn('enable-linger', argv)
        self.assertNotIn('disable-linger', argv)

    def test_operator_linger_no_refused(self):
        with self.assertRaises(RuntimeError):
            check_operator_linger('operator', Mock(return_value=Mock(stdout='no')))

    def test_operator_linger_probe_failure_is_named_refusal(self):
        for exc in (subprocess.CalledProcessError(1, ['loginctl']), FileNotFoundError('loginctl'),
                    subprocess.TimeoutExpired(['loginctl'], 15)):
            with self.assertRaisesRegex(RuntimeError, 'Linger=yes required for operator op'):
                check_operator_linger('op', Mock(side_effect=exc))

    def test_engine_user_refused_not_linger_checked(self):
        # Engine Linger=yes conflicts with leaf containment (B6/B9 conflict); Nov 11 is same-uid.
        refuse_engine_user({})
        refuse_engine_user({'RECURSION_ENGINE_USER': '  '})
        with self.assertRaisesRegex(RuntimeError, 'no user manager'):
            refuse_engine_user({'RECURSION_ENGINE_USER': 'csl-engine'})

    @unittest.skipIf(NOT_POSIX, LINUX_ONLY)
    def test_main_checks_only_operator_linger(self):
        import recursion_core.daemon.__main__ as entry
        with patch.object(entry, 'require_free_threaded'), \
             patch.object(entry.getpass, 'getuser', return_value='op'), \
             patch.object(entry, 'check_operator_linger',
                          side_effect=RuntimeError('stop here')) as linger, \
             patch.dict(entry.os.environ, {'RECURSION_ENGINE_USER': ''}):
            with self.assertRaisesRegex(RuntimeError, 'stop here'):
                entry.main()
        linger.assert_called_once_with('op')
        with patch.object(entry, 'require_free_threaded'), \
             patch.object(entry, 'check_operator_linger') as linger, \
             patch.dict(entry.os.environ, {'RECURSION_ENGINE_USER': 'csl-engine'}):
            with self.assertRaisesRegex(RuntimeError, 'not supported'):
                entry.main()
        linger.assert_not_called()

    def test_unit_file(self):
        text = UNIT_FILE.read_text()
        self.assertIn('-X gil=0', text)
        self.assertIn('python3.14t', text)
        self.assertIn('Restart=on-failure', text)
        for forbidden in ('Delegate=', 'PartOf', 'BindsTo', 'enable-linger', 'PDEATHSIG'):
            self.assertNotIn(forbidden, text)


@unittest.skipIf(NOT_POSIX, LINUX_ONLY)
class SystemdManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = SystemdUserManager(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def show(self, group, active='active', sub='running', load='loaded',
             invocation='0b5a2f3c9d8e4f60a1b2c3d4e5f60718', started='81234567', job=None):
        extra = '' if job is None else f'Job={job}\n'
        self.manager._call = Mock(return_value=f'LoadState={load}\nActiveState={active}\n'
                                               f'SubState={sub}\nControlGroup={group}\n'
                                               f'{extra}InvocationID={invocation}\n'
                                               f'ExecMainStartTimestampMonotonic={started}\n')

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
        self.show('', active='active')  # running but no cgroup: not positively ended
        self.assertEqual(self.manager.observe('a'), 'unknown')
        self.show('', active='deactivating', sub='stop-sigterm')
        self.assertEqual(self.manager.observe('a'), 'unknown')

    def test_stop_maps_only_systemds_not_loaded_error(self):
        unit = 'recursion-run-one.service'
        self.manager._call = Mock(side_effect=subprocess.CalledProcessError(
            5, ['systemctl'], stderr=f'Failed to stop {unit}: Unit {unit} not loaded.\n'))
        with self.assertRaises(UnitNotLoaded):
            self.manager.stop(unit)
        for exc in (subprocess.CalledProcessError(5, ['systemctl'], stderr='Access denied\n'),
                    subprocess.CalledProcessError(1, ['systemctl'], stderr=f'Unit {unit} not loaded.'),
                    subprocess.CalledProcessError(5, ['systemctl'], stderr='Unit recursion-run-two.service not loaded.'),
                    subprocess.TimeoutExpired(['systemctl'], 15)):
            with self.subTest(exc=exc):
                self.manager._call = Mock(side_effect=exc)
                with self.assertRaises(subprocess.SubprocessError) as caught:
                    self.manager.stop(unit)
                self.assertNotIsInstance(caught.exception, UnitNotLoaded)

    def test_real_post_stop_show_output(self):
        # systemctl --user show after systemctl --user stop of a transient unit: the
        # cgroup is pruned (ControlGroup= blank) and the unit is collected (not-found).
        self.show('', active='inactive', sub='dead', load='not-found')
        self.assertEqual(self.manager.observe('a'), 'gone')
        # Not yet collected (or failed): loaded, ended, cgroup pruned.
        for active, sub in (('inactive', 'dead'), ('failed', 'failed')):
            self.show('', active=active, sub=sub)
            self.assertEqual(self.manager.observe('a'), 'empty')

    def test_real_natural_exit_show_output(self):
        # RemainAfterExit=yes: active (exited); systemd prunes the empty cgroup.
        self.show('', active='active', sub='exited')
        self.assertEqual(self.manager.observe('a'), 'empty')

    def test_never_started_unit_is_not_empty(self):
        # A transient unit whose start job is still queued: no cgroup yet, no invocation.
        self.show('', active='inactive', sub='dead', invocation='', started='0', job='4211')
        self.assertEqual(self.manager.observe('a'), 'unknown')
        self.show('', active='inactive', sub='dead', invocation='', started='0')
        self.assertEqual(self.manager.observe('a'), 'unknown')
        # Ran before, but a new start job is queued: not positively ended.
        self.show('', active='inactive', sub='dead', job='4212')
        self.assertEqual(self.manager.observe('a'), 'unknown')
        # Either piece of evidence that it ran is enough once no job is queued.
        self.show('', active='inactive', sub='dead', invocation='', started='81234567')
        self.assertEqual(self.manager.observe('a'), 'empty')
        self.show('', active='inactive', sub='dead', started='0')
        self.assertEqual(self.manager.observe('a'), 'empty')

    def test_launch_timeout_with_queued_job_is_unknown_and_not_swept(self):
        # Round-2 red team: systemd-run hits the _call timeout while the start job is
        # queued. The run must read unknown (never terminated) and the sweep must not
        # stop its unit.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        registry = Registry(Path(temp.name) / 'state')
        self.addCleanup(registry.close)
        unit = 'recursion-run-one.service'
        calls = []
        def call(argv):
            calls.append(argv)
            if argv[0] == 'systemd-run':
                raise subprocess.TimeoutExpired(argv, 15)
            if argv[:3] == ['systemctl', '--user', 'show']:
                return ('LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
                        'ControlGroup=\nJob=4211\nInvocationID=\n'
                        'ExecMainStartTimestampMonotonic=0\n')
            if argv[:3] == ['systemctl', '--user', 'list-units']:
                return f'{unit} loaded inactive dead /fake\n'
            return ''
        self.manager._call = call
        daemon = Daemon(registry, self.manager, lambda: 'boot-a')
        with self.assertRaises(subprocess.TimeoutExpired):
            daemon.start('one', ['/fake'])
        self.assertEqual(registry.get('one').state, 'unknown')
        self.assertEqual(daemon.sweep()[0].state, 'unknown')
        self.assertFalse([a for a in calls if a[:3] == ['systemctl', '--user', 'stop']])

    def test_ended_unit_with_live_cgroup_is_not_empty(self):
        # failed with processes left behind: the cgroup still exists and is populated.
        self.events('/user.slice/a.service', 1)
        self.show('/user.slice/a.service', active='failed', sub='failed')
        self.assertEqual(self.manager.observe('a'), 'unknown')

    def test_daemon_stop_reaches_terminated_on_real_show_output(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        registry = Registry(Path(temp.name) / 'state')
        self.addCleanup(registry.close)
        self.events('/user.slice/r.service', 1)
        calls = []
        def call(argv):
            calls.append(argv[:3])
            if argv[:3] == ['systemctl', '--user', 'show']:
                if ['systemctl', '--user', 'stop'] in calls:
                    return 'LoadState=not-found\nActiveState=inactive\nSubState=dead\nControlGroup=\n'
                return ('LoadState=loaded\nActiveState=active\nSubState=running\n'
                        'ControlGroup=/user.slice/r.service\n')
            return ''
        self.manager._call = call
        daemon = Daemon(registry, self.manager, lambda: 'boot-a')
        self.assertEqual(daemon.start('one', ['/fake']).state, 'running')
        self.assertEqual(daemon.stop('one').state, 'terminated')
        self.assertEqual(daemon.sweep()[0].state, 'terminated')

    def test_populated_zero_is_empty(self):
        self.events('/user.slice/a.service', 0)
        self.show('/user.slice/a.service')
        self.assertEqual(self.manager.observe('a'), 'empty')

    def test_scripted_empty_with_live_listener_is_not_empty(self):
        # populated 0 is scripted, but cgroup.procs still names a live listener.
        group = '/user.slice/a.service'
        self.events(group, 0)
        (self.root / group.lstrip('/') / 'cgroup.procs').write_text('4242\n')
        self.show(group, active='inactive', sub='dead')
        self.manager.pid_alive = lambda pid: pid == 4242
        self.assertNotEqual(self.manager.observe('a'), 'empty')
        self.assertFalse(self.manager.processes_gone('a'))
        # A show line that says the unit exited while MainPID is still alive.
        self.manager._call = Mock(return_value=(
            'LoadState=loaded\nActiveState=inactive\nSubState=dead\nControlGroup=\n'
            'InvocationID=0b5a2f3c9d8e4f60a1b2c3d4e5f60718\n'
            'ExecMainStartTimestampMonotonic=81234567\nMainPID=4242\n'))
        self.assertNotEqual(self.manager.observe('a'), 'empty')
        self.assertFalse(self.manager.processes_gone('a'))
        # not-found is the same: a live MainPID is not a collected unit.
        self.manager._call = Mock(return_value=(
            'LoadState=not-found\nActiveState=inactive\nSubState=dead\n'
            'ControlGroup=\nMainPID=4242\n'))
        self.assertNotEqual(self.manager.observe('a'), 'gone')
        self.assertFalse(self.manager.processes_gone('a'))
        # The same scripts with the listener gone are an attested end.
        self.manager.pid_alive = lambda pid: False
        self.assertEqual(self.manager.observe('a'), 'gone')
        self.assertTrue(self.manager.processes_gone('a'))
        self.manager._call = Mock(return_value=(
            'LoadState=loaded\nActiveState=inactive\nSubState=dead\nControlGroup=\n'
            'InvocationID=0b5a2f3c9d8e4f60a1b2c3d4e5f60718\n'
            'ExecMainStartTimestampMonotonic=81234567\nMainPID=4242\n'))
        self.assertEqual(self.manager.observe('a'), 'empty')
        self.assertTrue(self.manager.processes_gone('a'))

    def test_unreadable_cgroup_or_proc_is_not_terminated(self):
        # Only ENOENT/ESRCH mean gone. EACCES, EIO, or any other read error is
        # unknown, and the daemon must not record terminated.
        group = '/user.slice/a.service'
        self.events(group, 0)
        procs = self.root / group.lstrip('/') / 'cgroup.procs'
        procs.mkdir()  # read_text raises EISDIR, a real OSError that is not ENOENT
        self.show(group, active='inactive', sub='dead', invocation='', started='0')
        self.show(group, active='inactive', sub='dead')
        self.assertNotEqual(self.manager.observe('a'), 'empty')
        self.assertFalse(self.manager.processes_gone('a'))
        procs.rmdir()
        procs.write_text('4242\n')
        real_read = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name == 'cgroup.procs':
                error = OSError(errno.EACCES, 'Permission denied')
                raise error
            if path.name == 'stat':
                raise OSError(errno.EIO, 'Input/output error')
            return real_read(path, *args, **kwargs)

        self.show(group, active='inactive', sub='dead')
        with patch.object(Path, 'read_text', read_text):
            self.assertIsNone(self.manager.pid_alive(4242))
            self.assertNotEqual(self.manager.observe('a'), 'empty')
            self.assertFalse(self.manager.processes_gone('a'))
        # The same unit with the process actually gone is an attested end.
        def read_gone(path, *args, **kwargs):
            if path.name == 'stat':
                raise OSError(errno.ENOENT, 'No such file or directory')
            return real_read(path, *args, **kwargs)

        with patch.object(Path, 'read_text', read_gone):
            self.assertIs(self.manager.pid_alive(4242), False)
            self.assertEqual(self.manager.observe('a'), 'empty')
            self.assertTrue(self.manager.processes_gone('a'))
        def read_esrch(path, *args, **kwargs):
            if path.name == 'stat':
                raise OSError(errno.ESRCH, 'No such process')
            return real_read(path, *args, **kwargs)

        with patch.object(Path, 'read_text', read_esrch):
            self.assertIs(self.manager.pid_alive(4242), False)

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        registry = Registry(Path(temp.name) / 'state')
        self.addCleanup(registry.close)
        run_group = '/user.slice/r.service'
        self.events(run_group, 0)
        (self.root / run_group.lstrip('/') / 'cgroup.procs').mkdir()
        def call(argv):
            if argv[:3] == ['systemctl', '--user', 'show']:
                return ('LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
                        f'ControlGroup={run_group}\nMainPID=4242\n'
                        'InvocationID=0b5a2f3c9d8e4f60a1b2c3d4e5f60718\n'
                        'ExecMainStartTimestampMonotonic=81234567\n')
            return ''
        self.manager._call = call
        daemon = Daemon(registry, self.manager, lambda: 'boot-a')
        self.assertNotEqual(daemon.start('one', ['/fake']).state, 'terminated')
        self.assertNotEqual(daemon.stop('one').state, 'terminated')
        self.assertNotEqual(daemon.sweep()[0].state, 'terminated')

    def test_delegated_descendant_pid_is_not_terminated(self):
        # Delegate= child cgroups are inside the unit. A stale populated 0 and
        # an empty cgroup.procs on the unit itself do not make a live descendant gone.
        group = '/user.slice/a.service'
        self.events(group, 0)
        root = self.root / group.lstrip('/')
        (root / 'cgroup.procs').write_text('')
        nested = root / 'delegated' / 'nested'
        nested.mkdir(parents=True)
        (root / 'delegated' / 'cgroup.procs').write_text('')
        (root / 'delegated' / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        (nested / 'cgroup.procs').write_text('4242\n')
        (nested / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        self.manager.pid_alive = lambda pid: pid == 4242
        self.manager._call = Mock(return_value=(
            'LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
            f'ControlGroup={group}\nMainPID=0\n'
            'InvocationID=0b5a2f3c9d8e4f60a1b2c3d4e5f60718\n'
            'ExecMainStartTimestampMonotonic=81234567\n'))
        self.assertNotEqual(self.manager.observe('a'), 'empty')
        self.assertFalse(self.manager.processes_gone('a'))
        # Hierarchical populated, with no pid listed, is still not an empty tree.
        (nested / 'cgroup.procs').write_text('')
        (nested / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
        self.manager.pid_alive = lambda pid: False
        self.assertNotEqual(self.manager.observe('a'), 'empty')
        self.assertFalse(self.manager.processes_gone('a'))
        # A descendant that cannot be read fails closed.
        (nested / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        (nested / 'cgroup.procs').unlink()
        (nested / 'cgroup.procs').mkdir()
        self.assertNotEqual(self.manager.observe('a'), 'empty')
        self.assertFalse(self.manager.processes_gone('a'))
        # A symlink child is not followed and is not treated as empty.
        (nested / 'cgroup.procs').rmdir()
        (nested / 'cgroup.procs').write_text('')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'cgroup.procs').write_text('4242\n')
        link = root / 'linked'
        link.symlink_to(outside, target_is_directory=True)
        self.manager.pid_alive = lambda pid: pid == 4242
        self.assertNotEqual(self.manager.observe('a'), 'empty')
        self.assertFalse(self.manager.processes_gone('a'))
        link.unlink()
        (nested / 'cgroup.procs').unlink()
        (nested / 'cgroup.events').unlink()
        nested.rmdir()
        (root / 'delegated' / 'cgroup.procs').unlink()
        (root / 'delegated' / 'cgroup.events').unlink()
        (root / 'delegated').rmdir()
        (root / 'cgroup.procs').write_text('')
        self.manager.pid_alive = lambda pid: False
        self.assertEqual(self.manager.observe('a'), 'empty')
        self.assertTrue(self.manager.processes_gone('a'))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        registry = Registry(Path(temp.name) / 'state')
        self.addCleanup(registry.close)
        (root / 'delegated').mkdir()
        (root / 'delegated' / 'cgroup.procs').write_text('4242\n')
        (root / 'delegated' / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        self.manager.pid_alive = lambda pid: pid == 4242
        registry.add('one', 'boot-a')
        registry.state('one', 'running')
        daemon = Daemon(registry, self.manager, lambda: 'boot-a')
        self.assertNotEqual(daemon.sweep()[0].state, 'terminated')

    def test_deep_cgroup_tree_is_unknown(self):
        # The chain and the cycle live in memory. Nothing is written under the
        # host cgroup filesystem; the temp events file only lets observe() enter
        # the populated-0 branch.
        class Node:
            def __init__(self, ident):
                self.ident = ident
                self.child = None

        group = '/user.slice/a.service'
        self.events(group, 0)
        root = self.root / group.lstrip('/')
        (root / 'cgroup.procs').write_text('')
        head = Node(0)
        cursor = head
        for ident in range(1, 1100):
            cursor.child = Node(ident)
            cursor = cursor.child
        loop = Node('loop')
        loop.child = loop
        wide = [Node(ident) for ident in range(CGROUP_WALK_MAX_NODES)]
        real_identity = self.manager._cgroup_identity
        real_children = self.manager._cgroup_children
        real_read = self.manager._read_level
        mode = {'kind': 'deep'}

        def identity(directory):
            if isinstance(directory, Node):
                return ('mem', directory.ident)
            return real_identity(directory)

        def children(directory, remaining):
            if isinstance(directory, Node):
                kids = [] if directory.child is None else [directory.child]
            elif directory != root:
                return real_children(directory, remaining)
            elif mode['kind'] == 'deep':
                kids = [head]
            elif mode['kind'] == 'cycle':
                kids = [loop]
            else:
                kids = wide
            if len(kids) > remaining[0]:
                return None
            remaining[0] -= len(kids)
            return kids

        def read_level(directory):
            if isinstance(directory, Node):
                return [], False
            return real_read(directory)

        self.manager._cgroup_identity = identity
        self.manager._cgroup_children = children
        self.manager._read_level = read_level
        self.manager._call = Mock(return_value=(
            'LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
            f'ControlGroup={group}\nMainPID=0\n'
            'InvocationID=0b5a2f3c9d8e4f60a1b2c3d4e5f60718\n'
            'ExecMainStartTimestampMonotonic=81234567\n'))
        for kind in ('deep', 'cycle', 'wide'):
            mode['kind'] = kind
            self.assertIsNone(self.manager._subtree(root))
            self.assertNotEqual(self.manager.observe('a'), 'empty')
            self.assertFalse(self.manager.processes_gone('a'))

    def test_wide_cgroup_listing_stops_before_read_and_stat(self):
        # Real _cgroup_children, against a fake directory. No host cgroup files.
        group = '/user.slice/a.service'
        self.events(group, 0)
        (self.root / group.lstrip('/') / 'cgroup.procs').write_text('')
        root = self.root / group.lstrip('/')
        real_lstat = Path.lstat

        def listing(count, produced, stated):
            def entries(path):
                # An empty listing for every path except the root. Do not call
                # Path.iterdir here: 3.12's is lazy, so returning it just ends
                # this generator, but 3.13 lists immediately and raises
                # FileNotFoundError for a fake child that was never created.
                if path != root:
                    return
                for i in range(count):
                    produced.append(path / str(i))
                    yield path / str(i)

            def info(path):
                if path != root and not (path.parent == root and path.name.isdigit()):
                    return real_lstat(path)
                stated.append(path)
                inode = 1 if path == root else int(path.name) + 2
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_dev=1, st_ino=inode)

            return entries, info

        show = (
            'LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
            f'ControlGroup={group}\nMainPID=0\n'
            'InvocationID=0b5a2f3c9d8e4f60a1b2c3d4e5f60718\n'
            'ExecMainStartTimestampMonotonic=81234567\n')

        def bounded(before, after):
            self.assertLessEqual(after - before, CGROUP_WALK_MAX_NODES + 1)

        # The root takes one slot. Exactly the remaining slots, each an empty
        # child directory, is a finished walk.
        produced, stated = [], []
        entries, info = listing(CGROUP_WALK_MAX_NODES - 1, produced, stated)
        self.manager._call = Mock(return_value=show)
        with patch.object(Path, 'iterdir', entries), \
             patch.object(Path, 'lstat', info), \
             patch.object(self.manager, '_read_level', return_value=([], False)):
            self.assertEqual(self.manager._subtree(root), ([], False))
            self.assertEqual(len(produced), CGROUP_WALK_MAX_NODES - 1)
            self.assertEqual(self.manager.observe('a'), 'empty')
        # One past the budget is unknown, and that extra name is not stat'd.
        produced, stated = [], []
        entries, info = listing(3 * CGROUP_WALK_MAX_NODES, produced, stated)
        self.manager._call = Mock(return_value=show)
        with patch.object(Path, 'iterdir', entries), \
             patch.object(Path, 'lstat', info), \
             patch.object(self.manager, '_read_level', return_value=([], False)):
            before = len(produced)
            self.assertIsNone(self.manager._subtree(root))
            bounded(before, len(produced))
            before = len(produced)
            self.assertNotEqual(self.manager.observe('a'), 'empty')
            bounded(before, len(produced))
            before = len(produced)
            self.assertFalse(self.manager.processes_gone('a'))
            bounded(before, len(produced))
        child_stats = [path for path in stated if path != root]
        self.assertLess(len(child_stats), len(produced))

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
