"""P3 checks for the cgroup leaf.

No live cgroup or systemd operation. The record is a synthetic fixture. The launch
cells use a fake filesystem; one cell also starts a real short-lived child.
"""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from recursion_core.cgroup_leaf import Cgroup2, DelegatedRoot
from recursion_core.cgroup_leaf.b6_evidence import ATTACH_CALL, SCOPE_ARGV, evaluate

NOT_LINUX = os.name == 'nt' or not hasattr(os, 'pidfd_open')
LINUX_ONLY = 'Linux-only: the cgroup v2 leaf launcher needs pidfd, pipe2 and uid ownership'

LEAF = '/operator.slice/run-probe'


def _proc_state(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except FileNotFoundError:
        return None


def host_record():
    """Small generated fixture, structural only; never evidence of host behaviour."""
    arms = {}
    for arm, first in (('manager_absent', 4101), ('manager_present', 5101)):
        trials = []
        for i, method in enumerate(('scope', 'direct_write', 'attach_unit')):
            for j in range(10):
                pid = first + i * 20 + j * 2
                item = dict(method=method, uid=1000, pid=pid, initial_cgroup=LEAF,
                            expected_pids=[pid], run_pids={str(pid): LEAF})
                if method == 'scope':
                    item.update(argv=list(SCOPE_ARGV), exit_code=1,
                                sleep_cgroup='never_started', refused=True,
                                error='synthetic manager absence')
                    if arm == 'manager_present':
                        item.update(exit_code=0, sleep_cgroup='/outside/run-control')
                        item.pop('refused', None)
                        item.pop('error', None)
                elif method == 'direct_write':
                    item.update(target='/outside/cgroup.procs', written_pid=pid, refused=True)
                    if arm == 'manager_present':
                        item['refused'] = False
                else:
                    item.update(bus='system', call=ATTACH_CALL, unit='outside.service',
                                subcgroup='', pids=[pid], refused=True)
                    if arm == 'manager_present':
                        item['refused'] = False
                trials.append(item)
        arms[arm] = dict(user_manager='inactive' if arm == 'manager_absent' else 'active',
                         manager_socket='absent', attempts=trials)
    return dict(evidence_class='host', host='synthetic-host', kernel='synthetic',
                systemd='synthetic', captured_at='2026-11-16T00:00:00Z',
                command_log_sha256='a' * 64, leaf=LEAF, engine_uid=1000, operator_uid=1001,
                escapers=dict(escaped=0, total=120), procs_write_refused=dict(ok=10, total=10),
                user_spawn_refused_or_contained=dict(ok=10, total=10),
                terminated_while_populated=0,
                spawn_order=['uid_switch', 'pdeathsig', 'exec'],
                pdeathsig_spawner=dict(tid=4000, long_lived=True, spawn_tids=[4000]), **arms)


class EvidenceRegressions(unittest.TestCase):
    def test_untrusted_fields_cannot_insert_lines_into_refusals(self):
        """A newline in a record field must not become a second refusal line."""
        for index, field, value in (
            (10, 'target', '/synthetic-canary\nFORGED-EVENT/cgroup.procs'),
            (20, 'unit', 'synthetic-canary.service\nFORGED-EVENT'),
        ):
            with self.subTest(field=field):
                record = host_record()
                record['manager_absent']['attempts'][index].update({field: value, 'refused': False})
                result = evaluate(record)
                self.assertEqual(result['verdict'], 'fail')
                for reason in result['reasons']:
                    self.assertNotIn('\n', reason, 'untrusted newline reached a refusal')
                    self.assertNotIn('\r', reason, 'untrusted carriage return reached a refusal')


class ProcessGroupFS(Cgroup2):
    """A leaf has just one real child, in its own process group."""

    def make_leaf(self, leaf):
        leaf.mkdir()
        for name in ('cgroup.procs', 'cgroup.kill', 'cgroup.events'):
            (leaf / name).touch()

    def kill(self, leaf):
        text = (leaf / 'cgroup.procs').read_text().strip()
        if text:
            try:
                os.killpg(int(text), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def populated(self, leaf):
        try:
            text = (leaf / 'cgroup.procs').read_text().strip()
        except FileNotFoundError:
            return False if leaf.exists() else None
        if not text:
            return False
        return _proc_state(int(text)) not in (None, 'Z', 'X')

    def member(self, pid):
        for path in self.mount.rglob('cgroup.procs'):
            if path.read_text().strip() == str(pid):
                return path.parent
        return None

    def remove(self, leaf):
        for name in ('cgroup.procs', 'cgroup.kill', 'cgroup.events'):
            (leaf / name).unlink(missing_ok=True)
        leaf.rmdir()


@unittest.skipIf(NOT_LINUX, LINUX_ONLY)
class MkdirInterruptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p3-cg-')
        self.base = Path(self.temp.name).resolve()
        self.mount = self.base / 'cgroup'
        self.root_path = self.mount / 'delegated'
        self.root_path.mkdir(parents=True)
        for name in ('cgroup.procs', 'cgroup.kill'):
            (self.root_path / name).touch()
        self.fs = ProcessGroupFS(self.mount)
        self.root = DelegatedRoot(self.root_path, self.fs)

    def tearDown(self):
        self.temp.cleanup()

    def test_cancellation_after_mkdir_cleans_the_new_leaf(self):
        """An interrupt after the directory exists must drop that leaf and still propagate."""
        make_leaf = self.fs.make_leaf

        def interrupted(leaf):
            make_leaf(leaf)
            raise KeyboardInterrupt('injected immediately after mkdir')

        with patch.object(self.fs, 'make_leaf', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.root.launch('run-cancel', ['/bin/true'])
        self.assertIn('injected immediately after mkdir', str(caught.exception))
        self.assertFalse((self.root_path / 'run-cancel').exists())

    def test_removal_failure_after_mkdir_keeps_the_original_interrupt(self):
        make_leaf = self.fs.make_leaf

        def interrupted(leaf):
            make_leaf(leaf)
            raise KeyboardInterrupt('injected immediately after mkdir')

        def removal_fails(leaf):
            raise OSError('rmdir refused')

        with patch.object(self.fs, 'make_leaf', side_effect=interrupted), \
             patch.object(self.fs, 'remove', side_effect=removal_fails):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.root.launch('run-cancel', ['/bin/true'])
        self.assertIn('injected immediately after mkdir', str(caught.exception))
        self.assertIsInstance(caught.exception, KeyboardInterrupt)
        self.assertTrue((self.root_path / 'run-cancel').is_dir())

    def test_an_existing_leaf_stays_when_creation_is_refused(self):
        leaf = self.root_path / 'run-kept'
        self.fs.make_leaf(leaf)
        with self.assertRaises(FileExistsError):
            self.root.launch('run-kept', ['/bin/true'])
        self.assertTrue(leaf.is_dir())
        self.assertTrue((leaf / 'cgroup.procs').is_file())


class PipeFilesystem:
    """Fake leaf used only to collect one real child and its PIPE streams."""

    def __init__(self, mount, root):
        self.mount = mount
        self.root = root

    def make_leaf(self, leaf):
        leaf.mkdir()
        (leaf / 'cgroup.procs').touch()

    def open_procs(self, leaf):
        return os.open(leaf / 'cgroup.procs', os.O_WRONLY)

    def member(self, pid):
        return self.root / 'run-pipes'

    def kill(self, leaf):
        text = (leaf / 'cgroup.procs').read_text()
        if text.strip():
            try:
                os.killpg(int(text), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def populated(self, leaf):
        text = (leaf / 'cgroup.procs').read_text()
        return bool(text.strip()) and _proc_state(int(text)) not in (None, 'Z', 'X')

    def remove(self, leaf):
        (leaf / 'cgroup.procs').unlink()
        leaf.rmdir()


@unittest.skipIf(NOT_LINUX, LINUX_ONLY)
class LeafDescriptorTests(unittest.TestCase):
    def test_F08_context_closes_captured_stdio_after_collecting_child(self):
        temp = tempfile.TemporaryDirectory(prefix='p3-pipe-')
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        mount = base / 'cgroup'
        root = mount / 'delegated'
        root.mkdir(parents=True)
        (root / 'cgroup.procs').touch()
        (root / 'cgroup.kill').touch()
        delegated = DelegatedRoot(root, PipeFilesystem(mount, root))
        holder = {}

        def reap():
            run = holder.get('run')
            if run is None or run.proc is None:
                return
            if run.proc.poll() is None:
                run.proc.kill()
                run.proc.wait(timeout=2)
            for stream in (run.proc.stdin, run.proc.stdout, run.proc.stderr):
                if stream is not None:
                    stream.close()

        self.addCleanup(reap)
        with delegated.launch('run-pipes', ['/bin/sleep', '4'], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as run:
            holder['run'] = run
        streams = [run.proc.stdin, run.proc.stdout, run.proc.stderr]
        self.assertIsNotNone(run.proc.returncode)
        self.assertTrue(all(stream.closed for stream in streams))


if __name__ == '__main__':
    unittest.main()
