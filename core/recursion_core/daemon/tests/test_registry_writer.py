"""The registry refuses a file it cannot read, has one writer, and books no end it did not see.

Red tests of the sweep triage of daemon (STOP, CSL-238), entries E15, E14, E11, E12 and E32: each fails on
master without the change in ``daemon/__init__.py`` and ``control_adapter.py`` and passes with it. Every
fixture is synthetic and lies below a temporary folder; the host manager is replaced and no process is signalled.
"""
from contextlib import closing
import errno
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import recursion_core.daemon as core
from recursion_core.daemon import control_adapter as adapter

LINUX = sys.platform.startswith('linux')
CHILD_LIMIT_S = 20
OPEN_STATE = ('import sys\n'
              'from pathlib import Path\n'
              'import recursion_core.daemon as core\n'
              'try:\n'
              '    core.Registry(Path(sys.argv[1]) / "state")\n'
              'except core.AlreadyRunning:\n'
              '    print("refused")\n'
              'else:\n'
              '    print("opened")\n')


class Units:
    def __init__(self):
        self.live = True
        self.stops = []

    def stop(self, unit):
        self.stops.append(unit)
        self.live = False

    def observe(self, unit):
        return 'running' if self.live else 'gone'

    def processes_gone(self, unit):
        return not self.live

    def owned_units(self):
        return set()


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class RegistryWriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='registry-writer-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def store(self, directory=None):
        store = core.Registry(directory or self.root / 'state')
        self.addCleanup(store.close)
        return store

    # -- E15: a runs table that lacks columns is a typed refusal at open ------------------------------
    def test_a_runs_table_without_its_columns_is_refused_at_open(self):
        partial = self.root / 'partial'
        partial.mkdir(mode=0o700)
        target = partial / 'runs.sqlite3'
        with closing(sqlite3.connect(target)) as db:
            db.execute('CREATE TABLE runs (run_id TEXT PRIMARY KEY)')
            db.execute('PRAGMA user_version=1')
            db.commit()
        before = target.read_bytes()
        refused = []  # kept, so the refused registries stay alive: only a close, not a garbage collection, frees the lock
        for _ in range(2):
            try:
                self.store(partial)
            except core.RegistryIntegrityError as exc:
                refused.append(exc)
        self.assertEqual(len(refused), 2)
        self.assertEqual(target.read_bytes(), before)

    def test_a_registry_with_all_its_columns_still_opens_and_reads(self):
        store = self.store()
        store.add('one', 'boot-a')
        self.assertEqual([run.run_id for run in self.store_reopened(store)], ['one'])

    def store_reopened(self, store):
        directory = store.directory
        store.close()
        return self.store(directory).all()

    # -- E14: one writer per state folder, whatever the runtime folder is -----------------------------
    def test_a_second_registry_over_one_state_folder_is_refused(self):
        first = self.store()
        first.add('target', 'boot-a')
        with self.assertRaises(core.AlreadyRunning):
            core.Registry(first.directory)
        first.request_stop('target')
        self.assertTrue(first.get('target').stop_requested)

    def test_the_state_folder_is_free_again_once_its_registry_is_closed(self):
        first = self.store()
        first.add('target', 'boot-a')
        first.close()
        again = self.store(first.directory)
        self.assertEqual([run.run_id for run in again.all()], ['target'])

    def test_a_second_process_over_one_state_folder_is_refused(self):
        first = self.store()
        first.add('target', 'boot-a')
        try:
            done = subprocess.run([sys.executable, '-B', '-c', OPEN_STATE, str(self.root)], capture_output=True,
                                  text=True, timeout=CHILD_LIMIT_S, check=False)
        except subprocess.TimeoutExpired:
            self.fail('the second process did not answer')
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'refused'), done.stderr)
        first.request_stop('target')
        self.assertTrue(first.get('target').stop_requested)

    def test_the_owner_file_is_private(self):
        first = self.store()
        info = (first.directory / 'owner.lock').lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)

    # -- E12: a publish that an interrupt cut short is not durable until its directory is synced ------
    def interrupted_publish_blocks_a_later_stop(self, interrupt_replace):
        store = self.store()
        store.add('one', 'boot-a')
        manager = Units()
        daemon = core.Daemon(store, manager, lambda: 'boot-a')
        if interrupt_replace:
            replace = core.os.replace

            def interrupted(source, target):
                replace(source, target)
                raise KeyboardInterrupt('interrupt immediately after the rename')
            cut = mock.patch.object(core.os, 'replace', side_effect=interrupted)
        else:
            cut = mock.patch.object(core, '_fsync_dir', side_effect=KeyboardInterrupt())
        with cut, self.assertRaises(KeyboardInterrupt):
            daemon.stop('one')
        self.assertEqual(manager.stops, [])
        with mock.patch.object(core, '_fsync_dir', side_effect=OSError(errno.EIO, 'injected')):
            with self.assertRaises(core.RegistryDurabilityError):
                daemon.sweep()
        self.assertEqual(manager.stops, [], 'a Stop was sent before the directory fsync succeeded')
        daemon.sweep()
        self.assertEqual(manager.stops, ['recursion-run-one.service'], 'the Stop goes out once the sync is done')

    def test_an_interrupt_right_after_the_rename_blocks_a_later_stop(self):
        self.interrupted_publish_blocks_a_later_stop(True)

    def test_an_interrupt_in_the_directory_fsync_blocks_a_later_stop(self):
        self.interrupted_publish_blocks_a_later_stop(False)

    def test_a_rename_that_failed_leaves_the_directory_as_durable_as_it_was(self):
        store = self.store()
        with mock.patch.object(core.os, 'replace', side_effect=OSError(errno.ENOSPC, 'injected')):
            with self.assertRaises(OSError):
                store.add('one', 'boot-a')
        with mock.patch.object(core, '_fsync_dir', side_effect=AssertionError('no sync is owed')):
            self.assertEqual(store.all(), [])

    # -- E11: a unit systemd reports ended does not end a run with a live process in its cgroup -------
    def unit_ended_with_a_live_process_in_the_recorded_cgroup(self):
        store = self.store()
        unit = 'recursion-run-one.service'
        group = '/old-manager/' + unit
        directory = self.root / 'cgroups' / group.lstrip('/')
        directory.mkdir(parents=True)
        (directory / 'cgroup.procs').write_text(str(os.getpid()) + '\n')
        (directory / 'cgroup.events').write_text('populated 1\n')
        store.add('one', 'boot-a')
        store.control_put('one', json.dumps({'cgroup': group}))
        manager = core.SystemdUserManager(self.root / 'cgroups')
        manager._call = mock.Mock(return_value=(
            'LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
            'ControlGroup=\nInvocationID=123456\nJob=0\nMainPID=0\n'))
        manager._kill_recorded_cgroup = mock.Mock(return_value=False)
        return core.Daemon(store, manager, lambda: 'boot-a'), manager

    def test_a_stop_walks_the_recorded_cgroup_of_an_ended_unit(self):
        daemon, manager = self.unit_ended_with_a_live_process_in_the_recorded_cgroup()
        self.assertNotIn(daemon.stop('one').state, core.TERMINAL)
        self.assertNotIn(daemon.reconcile('one').state, core.TERMINAL)
        manager._kill_recorded_cgroup.assert_called()

    def test_a_read_looks_at_the_recorded_cgroup_and_signals_nothing(self):
        daemon, manager = self.unit_ended_with_a_live_process_in_the_recorded_cgroup()
        self.assertNotIn(daemon.reconcile('one').state, core.TERMINAL)
        self.assertNotIn(daemon.reconcile('one').state, core.TERMINAL)
        manager._kill_recorded_cgroup.assert_not_called()

    def test_an_ended_unit_with_no_process_in_its_cgroup_is_still_terminated(self):
        daemon, manager = self.unit_ended_with_a_live_process_in_the_recorded_cgroup()
        directory = self.root / 'cgroups' / 'old-manager' / 'recursion-run-one.service'
        (directory / 'cgroup.procs').write_text('')
        (directory / 'cgroup.events').write_text('populated 0\n')
        self.assertIn(daemon.reconcile('one').state, core.TERMINAL)
        self.assertIn(daemon.stop('one').state, core.TERMINAL)
        manager._kill_recorded_cgroup.assert_not_called()

    # -- E32: a result that was renamed into place is synced through its folder -----------------------
    def test_a_result_write_syncs_its_folder(self):
        folder = self.root / 'runs' / 'run-one'
        folder.mkdir(parents=True, mode=0o700)
        starter = adapter.RouteStarter(None, {}, None, self.root)
        synced = []
        real = os.fsync

        def track(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return real(fd)
        with mock.patch.object(adapter.os, 'fsync', track):
            starter._record_result('run-one', {'status': 'ok', 'code': None})
        self.assertEqual(synced, [False, True], 'the file, then its folder, after the rename')
        self.assertEqual(json.loads((folder / 'result.json').read_text()), {'code': None, 'status': 'ok'})
        self.assertNotIn('run-one', starter.results)

    def test_a_result_whose_folder_cannot_be_synced_is_kept_in_memory(self):
        folder = self.root / 'runs' / 'run-one'
        folder.mkdir(parents=True, mode=0o700)
        starter = adapter.RouteStarter(None, {}, None, self.root)
        real = os.fsync

        def refuse_folder(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, 'injected')
            return real(fd)
        with mock.patch.object(adapter.os, 'fsync', refuse_folder):
            starter._record_result('run-one', {'status': 'ok', 'code': None})
        self.assertEqual(starter.results['run-one'], {'status': 'ok', 'code': None})


if __name__ == '__main__':
    unittest.main()
