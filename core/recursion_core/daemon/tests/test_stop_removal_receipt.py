"""Stop removes an empty cgroup directory under the manager root and records proof removed.

A directory that still holds entries is left in place and stays proof empty. The logical
cgroup path on the record is not a host path; the manager joins it to its own root.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from recursion_core.control.api import ControlAPI
from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import launcher
from recursion_core.daemon.tests.test_adv1_stop_restart import GROUP, LINUX, ScriptedManager, show

BOOT = 'boot-removal'


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class ManagerRemovalReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-stop-rm-', dir=os.environ.get('TMPDIR')))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / 'cg'

    def _api(self, manager, cgroup=GROUP, identity=None):
        daemon = Daemon(Registry(self.tmp / 'state'), manager, boot_id=lambda: BOOT)
        self.addCleanup(daemon.close)
        daemon.registry.add('run-x', BOOT)
        registry = adapter.DurableRunRegistry(daemon.registry, processes=adapter.pid_alive(manager))
        record = {'run_id': 'run-x', 'tenant': 't', 'engine': 'claude_code', 'state': 'running',
                  'cgroup': cgroup, 'started_at': 1.0, 'stop': None, 'start_key': None}
        if identity is not None:
            record['cgroup_identity'] = identity
        registry.put(record)
        api = ControlAPI(registry, launcher.UnitKillTree(daemon, poll_s=0.01), lambda p: {}, time.time,
                         confirm_timeout_s=0.3)
        return api

    def _forgotten(self):
        return ScriptedManager(self.root, show(LoadState='not-found', ActiveState='inactive', SubState='dead',
                                               ControlGroup='', InvocationID='', MainPID='0',
                                               ExecMainStartTimestampMonotonic='0'), stop='not_loaded')

    def test_stop_removes_an_empty_directory_under_the_manager_root(self):
        unit = 'recursion-run-run-x.service'
        directory = self.root / 'app.slice' / unit
        directory.mkdir(parents=True)
        api = self._api(self._forgotten())
        answer = api.call('run.stop', {'run_id': 'run-x'})
        proof = api.call('run.status', {'run_id': 'run-x'})['run']['stop_attestation']
        self.assertEqual(answer['code'], 'stopped', answer)
        self.assertEqual(proof['proof'], 'removed', proof)
        self.assertEqual(proof['cgroup'], GROUP)
        self.assertFalse(directory.exists())
        self.assertNotIn('exit_code', answer)

    def test_a_directory_that_still_holds_entries_stays_proof_empty(self):
        directory = self.root / GROUP.lstrip('/')
        directory.mkdir(parents=True)
        (directory / 'cgroup.procs').write_text('')
        (directory / 'cgroup.events').write_text('populated 0\n')
        api = self._api(self._forgotten())
        answer = api.call('run.stop', {'run_id': 'run-x'})
        proof = api.call('run.status', {'run_id': 'run-x'})['run']['stop_attestation']
        self.assertEqual((answer['code'], proof['proof']), ('stopped', 'empty'), (answer, proof))
        self.assertTrue(directory.is_dir())

    def test_an_empty_directory_outside_the_supervised_slice_is_never_removed(self):
        unit = 'recursion-run-run-x.service'
        directory = self.root / 'system.slice' / unit
        directory.mkdir(parents=True)
        cgroup = '/system.slice/' + unit
        api = self._api(self._forgotten(), cgroup=cgroup)
        answer = api.call('run.stop', {'run_id': 'run-x'})
        proof = api.call('run.status', {'run_id': 'run-x'})['run']['stop_attestation']
        self.assertNotEqual(proof['proof'], 'removed', (answer, proof))
        self.assertTrue(directory.is_dir())

    def test_a_symlink_in_the_slice_path_is_not_removed(self):
        unit = 'recursion-run-run-x.service'
        real = self.tmp / 'real-slice'
        directory = real / unit
        directory.mkdir(parents=True)
        self.root.mkdir(parents=True)
        (self.root / 'app.slice').symlink_to(real, target_is_directory=True)
        api = self._api(self._forgotten())
        answer = api.call('run.stop', {'run_id': 'run-x'})
        proof = api.call('run.status', {'run_id': 'run-x'})['run']['stop_attestation']
        self.assertNotEqual(proof['proof'], 'removed', (answer, proof))
        self.assertTrue(directory.is_dir())
        self.assertTrue((self.root / 'app.slice').is_symlink())

    def test_a_directory_whose_inode_is_not_the_recorded_one_stays(self):
        unit = 'recursion-run-run-x.service'
        directory = self.root / 'app.slice' / unit
        directory.mkdir(parents=True)
        info = directory.lstat()
        identity = {'st_dev': info.st_dev, 'st_ino': info.st_ino + 1}
        api = self._api(self._forgotten(), identity=identity)
        answer = api.call('run.stop', {'run_id': 'run-x'})
        proof = api.call('run.status', {'run_id': 'run-x'})['run']['stop_attestation']
        self.assertNotEqual(proof['proof'], 'removed', (answer, proof))
        self.assertTrue(directory.is_dir())

    def test_rmdir_enoent_is_not_answered_removed(self):
        unit = 'recursion-run-run-x.service'
        directory = self.root / 'app.slice' / unit
        directory.mkdir(parents=True)

        def vanish(path, dir_fd=None):
            raise OSError(errno.ENOENT, 'gone')

        api = self._api(self._forgotten())
        with mock.patch('recursion_core.daemon.os.rmdir', vanish), \
                mock.patch('recursion_core.control.api.os.rmdir', vanish):
            answer = api.call('run.stop', {'run_id': 'run-x'})
        proof = api.call('run.status', {'run_id': 'run-x'})['run']['stop_attestation']
        self.assertNotEqual(proof['proof'], 'removed', (answer, proof))
        self.assertEqual(proof['proof'], 'empty', proof)
        self.assertTrue(directory.is_dir())

    def test_ebusy_after_a_clear_walk_does_not_stay_pending(self):
        unit = 'recursion-run-run-x.service'
        directory = self.root / 'app.slice' / unit
        directory.mkdir(parents=True)
        (directory / 'cgroup.procs').write_text('')
        (directory / 'cgroup.events').write_text('populated 0\n')
        child = directory / 'child'
        child.mkdir()
        (child / 'cgroup.procs').write_text('')
        (child / 'cgroup.events').write_text('populated 0\n')

        def busy(path, dir_fd=None):
            raise OSError(errno.EBUSY, 'Device or resource busy')

        api = self._api(self._forgotten())
        with mock.patch('recursion_core.daemon.os.rmdir', busy), \
                mock.patch('recursion_core.control.api.os.rmdir', busy):
            answer = api.call('run.stop', {'run_id': 'run-x'})
        proof = api.call('run.status', {'run_id': 'run-x'})['run']['stop_attestation']
        self.assertEqual(answer['code'], 'stopped', answer)
        self.assertNotEqual(answer.get('reason'), 'cgroup_populated')
        self.assertEqual(proof['proof'], 'empty', proof)
        self.assertTrue(directory.is_dir())
        self.assertTrue(child.is_dir())

    def test_removal_without_a_recorded_stop_leaves_the_directory(self):
        """The path is proved and the marker is present. No recorded Stop, so it stays."""
        unit = 'recursion-run-run-x.service'
        directory = self.root / 'app.slice' / unit
        directory.mkdir(parents=True)
        daemon = Daemon(Registry(self.tmp / 'state-nostop'), self._forgotten(), boot_id=lambda: BOOT)
        self.addCleanup(daemon.close)
        daemon.registry.add('run-x', BOOT)
        daemon.registry.control_put('run-x', json.dumps({
            'run_id': 'run-x', 'cgroup': GROUP, 'cgroup_marker': 'cg-nostop'}))
        outcome = daemon.remove_cleared_cgroup('run-x', GROUP)
        self.assertNotEqual(outcome, 'removed')
        self.assertTrue(directory.is_dir())

    def test_a_dotdot_path_is_not_removed(self):
        """The climb is refused in the removal itself. The launcher also rejects it earlier."""
        unit = 'recursion-run-run-x.service'
        self.root.mkdir(parents=True)
        (self.root / 'app.slice').mkdir()
        outside = self.tmp / unit
        outside.mkdir()
        cgroup = '/app.slice/../../' + unit
        daemon = Daemon(Registry(self.tmp / 'state-dot'), self._forgotten(), boot_id=lambda: BOOT)
        self.addCleanup(daemon.close)
        daemon.registry.add('run-x', BOOT)
        daemon.registry.request_stop('run-x')
        daemon.registry.control_put('run-x', json.dumps({
            'run_id': 'run-x', 'cgroup': cgroup, 'cgroup_marker': 'cg-dot'}))
        outcome = daemon.remove_cleared_cgroup('run-x', cgroup)
        self.assertNotEqual(outcome, 'removed')
        self.assertTrue(outside.is_dir())

    def test_an_empty_marker_is_not_removed(self):
        unit = 'recursion-run-run-x.service'
        directory = self.root / 'app.slice' / unit
        directory.mkdir(parents=True)
        outcome = self._forgotten().remove_cleared_cgroup(unit, GROUP, marker='')
        self.assertNotEqual(outcome, 'removed')
        self.assertEqual(outcome, 'empty')
        self.assertTrue(directory.is_dir())

    def test_a_parent_swapped_between_the_check_and_rmdir_is_not_followed(self):
        """R2. The slice directory becomes a symlink in the rmdir call. The outside unit stays.

        The swap sits in the rmdir the product calls. The real removal runs with the
        arguments the product passed. The stored proof is not removed.
        """
        unit = 'recursion-run-run-x.service'
        directory = self.root / 'app.slice' / unit
        directory.mkdir(parents=True)
        outside = self.tmp / 'outside-swap'
        victim = outside / unit
        victim.mkdir(parents=True)
        real_rmdir = os.rmdir
        swapped = {'done': False}

        def attack(path, dir_fd=None):
            slice_dir = self.root / 'app.slice'
            if not swapped['done'] and slice_dir.is_dir() and not slice_dir.is_symlink():
                swapped['done'] = True
                slice_dir.rename(self.root / 'app.slice.held')
                slice_dir.symlink_to(outside, target_is_directory=True)
            if dir_fd is None:
                return real_rmdir(path)
            return real_rmdir(path, dir_fd=dir_fd)

        api = self._api(self._forgotten())
        with mock.patch('recursion_core.daemon.os.rmdir', attack), \
                mock.patch('recursion_core.control.api.os.rmdir', attack):
            answer = api.call('run.stop', {'run_id': 'run-x'})
        status = api.call('run.status', {'run_id': 'run-x'})['run']
        proof = status.get('stop_attestation') or {}
        self.assertTrue(victim.is_dir(), 'a directory outside the unit was removed')
        self.assertNotEqual(proof.get('proof'), 'removed', proof)
        self.assertNotEqual(answer['code'], 'stopped', answer)
        self.assertTrue(swapped['done'])

    def test_enotempty_with_an_unfinished_reread_is_not_empty(self):
        """R3. A child symlink makes the re-read unfinished. That result is not empty.

        rmdir of a directory that holds an entry answers ENOTEMPTY on this host. The
        remover used to report empty when that re-read could not be finished.
        """
        unit = 'recursion-run-run-x.service'
        directory = self.root / 'app.slice' / unit
        directory.mkdir(parents=True)
        (directory / 'cgroup.procs').write_text('')
        (directory / 'cgroup.events').write_text('populated 0\n')
        target = self.tmp / 'link-target'
        target.mkdir()
        (directory / 'child').symlink_to(target, target_is_directory=True)
        outcome = self._forgotten().remove_cleared_cgroup(unit, GROUP, marker='cg-unread')
        self.assertEqual(outcome, 'populated', outcome)
        self.assertTrue(directory.is_dir())
        self.assertTrue(target.is_dir())

    def test_evidence_stop_check_agrees_with_a_removed_proof(self):
        source = Path(__file__).resolve().parents[1].joinpath('evidence_e2e.py').read_text()
        self.assertNotIn("get('proof') == 'empty'", source)
        self.assertIn("in ('empty', 'removed')", source)

    def _sleeper(self):
        """A ``sleep`` child this test starts and ends itself. Nothing else is signalled."""
        proc = subprocess.Popen(['sleep', '300'])

        def end():
            if proc.poll() is None:
                proc.kill()
            proc.wait()

        self.addCleanup(end)
        return proc

    def _swap_in_rmdir(self, leafdir, child, move, busy):
        """An rmdir stand-in. On its first call the leaf lists ``child`` and ``move`` changes the path.

        Then the real rmdir runs with the arguments the product passed, or EBUSY is answered as
        a cgroup mount answers for a populated cgroup.
        """
        real_rmdir = os.rmdir
        state = {'done': False}

        def swap(path, dir_fd=None):
            if not state['done']:
                state['done'] = True
                (leafdir / 'cgroup.procs').write_text(f'{child.pid}\n')
                (leafdir / 'cgroup.events').write_text('populated 1\n')
                move()
            if busy:
                raise OSError(errno.EBUSY, 'Device or resource busy')
            if dir_fd is None:
                return real_rmdir(path)
            return real_rmdir(path, dir_fd=dir_fd)

        return swap, state

    def _r1(self, busy):
        unit = 'recursion-run-run-x.service'
        leafdir = self.root / 'app.slice' / unit
        leafdir.mkdir(parents=True)
        decoy = self.tmp / 'decoy'
        (decoy / unit).mkdir(parents=True)
        child = self._sleeper()

        def move():
            (self.root / 'app.slice').rename(self.root / 'app.slice.held')
            (self.root / 'app.slice').symlink_to(decoy, target_is_directory=True)

        swap, state = self._swap_in_rmdir(leafdir, child, move, busy)
        api = self._api(self._forgotten())
        with mock.patch('recursion_core.daemon.os.rmdir', swap), \
                mock.patch('recursion_core.control.api.os.rmdir', swap):
            answer = api.call('run.stop', {'run_id': 'run-x'})
        run = api.call('run.status', {'run_id': 'run-x'})['run']
        held = self.root / 'app.slice.held' / unit
        self.assertTrue(state['done'])
        self.assertEqual((answer['code'], answer.get('reason')), ('pending', 'cgroup_populated'), answer)
        self.assertIsNone(run.get('stop_attestation'), run)
        self.assertEqual((held / 'cgroup.procs').read_text().split(), [str(child.pid)])
        self.assertIsNone(child.poll(), 'the sleep child was signalled')
        self.assertTrue((decoy / unit).is_dir())

    def test_r1_a_live_pid_and_a_swapped_slice_inside_rmdir_stay_pending(self):
        """R1. Inside the rmdir call the leaf gains a live pid and app.slice is renamed aside,
        a link to a clear decoy put in its place. rmdir answers ENOTEMPTY.

        The second look reads the leaf through the parent folder the removal held, so the
        Stop is pending with no proof. The decoy is not read and not removed.
        """
        self._r1(busy=False)

    def test_r1_with_ebusy_as_a_cgroup_mount_answers_stays_pending(self):
        """R1 with rmdir answering EBUSY, as a cgroup mount answers for a populated cgroup."""
        self._r1(busy=True)

    def test_r2_a_live_pid_and_a_swapped_folder_above_the_root_stay_pending(self):
        """R2. Inside the rmdir call the leaf gains a live pid and the folder above the cgroup
        root is renamed aside, a link to a decoy tree put in its place.

        The second look reads the held folder, so the Stop is pending with no proof and the
        decoy tree's leaf of the same name stays.
        """
        unit = 'recursion-run-run-x.service'
        top = self.tmp / 'top'
        self.root = top / 'cg'
        leafdir = self.root / 'app.slice' / unit
        leafdir.mkdir(parents=True)
        decoytop = self.tmp / 'decoytop'
        (decoytop / 'cg' / 'app.slice' / unit).mkdir(parents=True)
        child = self._sleeper()

        def move():
            top.rename(self.tmp / 'top.held')
            top.symlink_to(decoytop, target_is_directory=True)

        swap, state = self._swap_in_rmdir(leafdir, child, move, busy=False)
        api = self._api(self._forgotten())
        with mock.patch('recursion_core.daemon.os.rmdir', swap), \
                mock.patch('recursion_core.control.api.os.rmdir', swap):
            answer = api.call('run.stop', {'run_id': 'run-x'})
        run = api.call('run.status', {'run_id': 'run-x'})['run']
        held = self.tmp / 'top.held' / 'cg' / 'app.slice' / unit
        self.assertTrue(state['done'])
        self.assertEqual((answer['code'], answer.get('reason')), ('pending', 'cgroup_populated'), answer)
        self.assertIsNone(run.get('stop_attestation'), run)
        self.assertEqual((held / 'cgroup.procs').read_text().split(), [str(child.pid)])
        self.assertIsNone(child.poll(), 'the sleep child was signalled')
        self.assertTrue((decoytop / 'cg' / 'app.slice' / unit).is_dir())


if __name__ == '__main__':
    unittest.main()
