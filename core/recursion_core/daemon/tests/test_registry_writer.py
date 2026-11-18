"""The registry refuses a file it cannot read, has one writer, and books no end it did not see.

Defect tests of the sweep triage of daemon (STOP, CSL-238), entries E15, E14, E11, E12 and E32, fail on
master without the change in ``daemon/__init__.py`` and ``control_adapter.py`` and pass with it. Four
controls pass on master as well: a complete registry still opens, the folder is free once its registry
is released, a failed rename leaves no owed directory sync, and an ended unit whose recorded cgroup
walked empty is still terminated. Every fixture is synthetic and lies below a temporary folder; the host
manager is replaced and no process is signalled.
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
        refused = []  # two refusals; a refused open has already released its descriptor
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

    # -- answers: column order and types, the directory lease, an unfinished walk, a reader -----------
    _CANON = (
        'run_id TEXT PRIMARY KEY, unit TEXT UNIQUE NOT NULL, stop INTEGER NOT NULL, '
        'state TEXT NOT NULL, boot_id TEXT, detail TEXT NOT NULL, created REAL NOT NULL, '
        'updated REAL NOT NULL')

    def plant_runs(self, name, columns):
        directory = self.root / name
        directory.mkdir(mode=0o700)
        target = directory / 'runs.sqlite3'
        with closing(sqlite3.connect(target)) as db:
            db.execute(f'CREATE TABLE runs ({columns})')
            db.execute('PRAGMA user_version=1')
            db.commit()
        os.chmod(target, 0o600)
        return directory

    def test_a_reordered_runs_table_is_refused_at_open(self):
        directory = self.plant_runs('reordered', (
            'unit TEXT UNIQUE NOT NULL, run_id TEXT PRIMARY KEY, stop INTEGER NOT NULL, '
            'state TEXT NOT NULL, boot_id TEXT, detail TEXT NOT NULL, created REAL NOT NULL, '
            'updated REAL NOT NULL'))
        with self.assertRaises(core.RegistryIntegrityError):
            self.store(directory)

    def test_an_extra_runs_column_is_refused_at_open(self):
        directory = self.plant_runs('extra', self._CANON + ', extra TEXT')
        with self.assertRaises(core.RegistryIntegrityError):
            self.store(directory)

    def test_a_text_stop_column_is_refused_at_open(self):
        directory = self.plant_runs('stoptext', self._CANON.replace(
            'stop INTEGER NOT NULL', 'stop TEXT NOT NULL', 1))
        with self.assertRaises(core.RegistryIntegrityError):
            self.store(directory)

    def test_removing_the_owner_file_does_not_admit_a_second_writer(self):
        first = self.store()
        first.add('target', 'boot-a')
        first.request_stop('target')
        os.unlink(first.directory / 'owner.lock')
        with self.assertRaises(core.AlreadyRunning):
            self.store(first.directory)
        with closing(sqlite3.connect(first.path)) as db:
            row = db.execute("SELECT stop, state FROM runs WHERE run_id='target'").fetchone()
        self.assertEqual(row, (1, 'stopping'))

    def test_the_writer_refusal_is_the_folder_lease(self):
        first = self.store()
        with self.assertRaises(core.RegistryAlreadyRunning) as caught:
            core.Registry(first.directory)
        self.assertIsInstance(caught.exception, core.AlreadyRunning)
        self.assertIsNot(type(caught.exception), core.AlreadyRunning)

    def test_a_read_only_registry_reads_beside_the_writer(self):
        first = self.store()
        first.add('one', 'boot-a')
        reader = core.Registry(first.directory, read_only=True)
        self.addCleanup(reader.close)
        self.assertEqual([run.run_id for run in reader.all()], ['one'])
        self.assertFalse(reader.read_only is False)
        with self.assertRaises(RuntimeError):
            reader.add('two', 'boot-a')
        self.assertEqual([run.run_id for run in first.all()], ['one'])

    def test_a_held_read_only_registry_sees_a_later_publish(self):
        # A reader opened once must see a later publish. The writer replaces the
        # file, so a connection that keeps the first inode misses the new run and
        # the Stop.
        first = self.store()
        first.add('one', 'boot-a')
        reader = core.Registry(first.directory, read_only=True)
        self.addCleanup(reader.close)
        self.assertEqual([run.run_id for run in reader.all()], ['one'])
        first.add('two', 'boot-b')
        first.request_stop('one')
        held = [(run.run_id, run.stop_requested) for run in reader.all()]
        fresh = core.Registry(first.directory, read_only=True)
        self.addCleanup(fresh.close)
        again = [(run.run_id, run.stop_requested) for run in fresh.all()]
        self.assertEqual(held, [('one', True), ('two', False)])
        self.assertEqual(again, held)

    def test_a_read_only_registry_refuses_a_stop_the_directory_barrier_has_not_paid(self):
        # A reader must not report a Stop the writer itself will not read while
        # the directory fsync is still failing.
        first = self.store()
        first.add('one', 'boot-a')
        real = os.fsync

        def refuse_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, 'injected')
            return real(fd)

        with mock.patch.object(core.os, 'fsync', refuse_directory):
            with self.assertRaises(core.RegistryDurabilityError):
                first.request_stop('one')
            with self.assertRaises(core.RegistryDurabilityError):
                first.all()
            reader = core.Registry(first.directory, read_only=True)
            self.addCleanup(reader.close)
            with self.assertRaises(core.RegistryDurabilityError):
                reader.all()

    def test_a_recorded_stop_on_an_unwalkable_cgroup_stays_unknown(self):
        # A recorded Stop whose walk does not finish stays unknown. Collapsing
        # that answer to "not alive" would book the run terminated and is what
        # this test refuses.
        unit = 'recursion-run-one.service'
        group = '/old-manager/' + unit
        root = self.root / 'cgroups-stop' / 'old-manager' / unit
        root.mkdir(parents=True)
        (root / 'cgroup.procs').write_text('')
        (root / 'cgroup.events').write_text('populated 0\n')
        nested = root
        for index in range(70):
            nested = nested / str(index)
            nested.mkdir()
            (nested / 'cgroup.procs').write_text('')
            (nested / 'cgroup.events').write_text('populated 0\n')
        daemon, manager = self.ended_with_record(
            json.dumps({'cgroup': group}), cgroup_root=self.root / 'cgroups-stop')
        daemon.registry.request_stop('one')
        run = daemon.reconcile('one')
        self.assertTrue(run.stop_requested)
        self.assertEqual(run.state, 'unknown')
        self.assertNotIn(run.state, core.TERMINAL)
        self.assertIn(core.CGROUP_UNREAD, run.detail)
        manager._kill_recorded_cgroup.assert_not_called()

    def test_an_existing_owner_file_is_made_private(self):
        directory = self.root / 'wide'
        directory.mkdir(mode=0o700)
        lock = directory / 'owner.lock'
        lock.write_bytes(b'')
        os.chmod(lock, 0o644)
        self.store(directory)
        self.assertEqual(stat.S_IMODE(lock.lstat().st_mode), 0o600)

    def test_a_symlinked_owner_file_is_refused(self):
        directory = self.root / 'linked'
        directory.mkdir(mode=0o700)
        other = self.root / 'elsewhere'
        other.write_bytes(b'secret')
        (directory / 'owner.lock').symlink_to(other)
        with self.assertRaises(OSError):
            self.store(directory)
        self.assertEqual(other.read_bytes(), b'secret')

    def test_a_fifo_owner_file_is_refused(self):
        directory = self.root / 'fifo'
        directory.mkdir(mode=0o700)
        os.mkfifo(directory / 'owner.lock', 0o600)
        with self.assertRaises(OSError):
            self.store(directory)

    def ended_with_record(self, document, manager=None, *, cgroup_root=None):
        directory = self.root / f'state-{len(list(self.root.glob("state-*")))}'
        store = self.store(directory)
        store.add('one', 'boot-a')
        if document is not None:
            store.control_put('one', document)
        if manager is None:
            manager = core.SystemdUserManager(cgroup_root or (self.root / 'cgroups'))
            manager._call = mock.Mock(return_value=(
                'LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
                'ControlGroup=\nInvocationID=abc\nJob=0\nMainPID=0\n'
                'ExecMainStartTimestampMonotonic=1\n'))
            manager._kill_recorded_cgroup = mock.Mock(return_value=False)
        return core.Daemon(store, manager, lambda: 'boot-a'), manager

    def test_an_unwalkable_recorded_cgroup_stays_unknown(self):
        unit = 'recursion-run-one.service'
        group = '/old-manager/' + unit
        root = self.root / 'cgroups' / 'old-manager' / unit
        root.mkdir(parents=True)
        (root / 'cgroup.procs').write_text('')
        (root / 'cgroup.events').write_text('populated 0\n')
        nested = root
        for index in range(70):
            nested = nested / str(index)
            nested.mkdir()
            (nested / 'cgroup.procs').write_text('')
            (nested / 'cgroup.events').write_text('populated 0\n')
        daemon, manager = self.ended_with_record(json.dumps({'cgroup': group}))
        run = daemon.reconcile('one')
        self.assertEqual(run.state, 'unknown')
        self.assertNotIn(run.state, core.TERMINAL)
        self.assertIn(core.CGROUP_UNREAD, run.detail)
        manager._kill_recorded_cgroup.assert_not_called()

        link_root = self.root / 'cgroups-link'
        real = link_root / 'real'
        real.mkdir(parents=True)
        (real / 'cgroup.procs').write_text(str(os.getpid()) + '\n')
        (real / 'cgroup.events').write_text('populated 1\n')
        parent = link_root / 'old-manager'
        parent.mkdir()
        (parent / unit).symlink_to(real, target_is_directory=True)
        daemon, manager = self.ended_with_record(json.dumps({'cgroup': group}), cgroup_root=link_root)
        run = daemon.reconcile('one')
        self.assertEqual(run.state, 'unknown', 'a symlink is not a walked cgroup')
        manager._kill_recorded_cgroup.assert_not_called()

        file_root = self.root / 'cgroups-file'
        file_parent = file_root / 'old-manager'
        file_parent.mkdir(parents=True)
        (file_parent / unit).write_text('not a directory')
        daemon, _manager = self.ended_with_record(json.dumps({'cgroup': group}), cgroup_root=file_root)
        self.assertEqual(daemon.reconcile('one').state, 'unknown')

        daemon, _manager = self.ended_with_record('not-json')
        run = daemon.reconcile('one')
        self.assertEqual(run.state, 'unknown')
        self.assertIn(core.CGROUP_UNREAD, run.detail)

        class Raiser:
            def stop(self, _unit):
                return None

            def observe(self, _unit):
                return 'empty'

            def processes_gone(self, _unit):
                return True

            def owned_units(self):
                return set()

            def recorded_cgroup_live(self, _unit, _group):
                raise PermissionError('unread')

        daemon, _manager = self.ended_with_record(json.dumps({'cgroup': group}), Raiser())
        self.assertEqual(daemon.reconcile('one').state, 'unknown')

        class NoLook:
            def stop(self, _unit):
                return None

            def observe(self, _unit):
                return 'empty'

            def processes_gone(self, _unit):
                return True

            def owned_units(self):
                return set()

        daemon, _manager = self.ended_with_record(json.dumps({'cgroup': group}), NoLook())
        self.assertEqual(daemon.reconcile('one').state, 'unknown')

    def test_a_truthy_cgroup_read_is_not_treated_as_alive(self):
        class Odd:
            def stop(self, _unit):
                return None

            def observe(self, _unit):
                return 'empty'

            def processes_gone(self, _unit):
                return True

            def owned_units(self):
                return set()

            def recorded_cgroup_live(self, _unit, _group):
                return 1

        unit = 'recursion-run-one.service'
        daemon, manager = self.ended_with_record(json.dumps({'cgroup': '/old/' + unit}), Odd())
        run = daemon.reconcile('one')
        self.assertEqual(run.state, 'unknown')
        self.assertNotEqual(run.state, 'running')
        self.assertIs(manager.recorded_cgroup_live(unit, '/old/' + unit), 1)

    def test_a_recorded_cgroup_for_another_unit_is_not_walked(self):
        unit = 'recursion-run-two.service'
        group = '/old-manager/' + unit
        directory = self.root / 'cgroups' / group.lstrip('/')
        directory.mkdir(parents=True)
        (directory / 'cgroup.procs').write_text('')
        (directory / 'cgroup.events').write_text('populated 0\n')
        daemon, manager = self.ended_with_record(json.dumps({'cgroup': group}))
        run = daemon.reconcile('one')
        self.assertEqual(run.state, 'unknown')
        self.assertIn(core.CGROUP_UNREAD, run.detail)
        manager._kill_recorded_cgroup.assert_not_called()
        self.assertIsNone(manager.recorded_cgroup_live('recursion-run-one.service', group))

    def test_a_recorded_cgroup_path_with_a_parent_dot_is_not_walked(self):
        unit = 'recursion-run-one.service'
        group = '/a/../' + unit
        daemon, manager = self.ended_with_record(json.dumps({'cgroup': group}))
        run = daemon.reconcile('one')
        self.assertEqual(run.state, 'unknown')
        manager._kill_recorded_cgroup.assert_not_called()
        self.assertIsNone(manager.recorded_cgroup_live(unit, group))

    def test_a_non_finite_result_is_kept_and_leaves_no_temp(self):
        folder = self.root / 'runs' / 'run-one'
        folder.mkdir(parents=True, mode=0o700)
        starter = adapter.RouteStarter(None, {}, None, self.root)
        for value in (float('nan'), float('inf')):
            starter.results.clear()
            starter._record_result('run-one', {'status': 'ok', 'value': value})
            self.assertIn('run-one', starter.results)
            self.assertFalse((folder / 'result.json').exists(), value)
            self.assertFalse((folder / 'result.json.tmp').exists(), value)

    def test_an_unserialisable_result_leaves_no_temp(self):
        folder = self.root / 'runs' / 'run-one'
        folder.mkdir(parents=True, mode=0o700)
        starter = adapter.RouteStarter(None, {}, None, self.root)
        starter._record_result('run-one', {'status': 'ok', 'value': {1, 2}})
        self.assertEqual(starter.results['run-one']['value'], {1, 2})
        self.assertFalse((folder / 'result.json').exists())
        self.assertFalse((folder / 'result.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
