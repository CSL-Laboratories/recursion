"""CSL-728 same-uid leaf tests. Fake cgroupfs with real processes; not runtime certification.

The fake models a leaf as the process group of its main process (started with
start_new_session). It exercises the real trampoline, pidfd and evidence logic; it does
not prove kernel cgroup behaviour. Set RECURSION_CGROUP_ROOT to a writable delegated
cgroup v2 directory containing this process to run the live cell.
"""
import errno
import inspect
import os
from pathlib import Path
import stat
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import recursion_core.cgroup_leaf as cgroup_leaf
from recursion_core.cgroup_leaf import (Cgroup2, CleanupIncomplete, DelegatedRoot,
                                        LaunchError, LeafRun)


def alive(pid):
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except OSError:
        return False
    return state != 'Z'


def wait_file(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.01)
    raise AssertionError(f'{path} never written')


class FakeCgroup(Cgroup2):
    def __init__(self, mount):
        super().__init__(mount)
        self.kill_broken = False
        self.events_broken = False
        self.member_override = None
        self.procs_override = None
        self.interrupts = 0  # cgroup.kill raises KeyboardInterrupt this many times
        self.interrupt_after_remove = False

    def make_leaf(self, leaf):
        os.mkdir(leaf, 0o755)
        for name in ('cgroup.procs', 'cgroup.kill', 'cgroup.events'):
            (leaf / name).touch()

    def open_procs(self, leaf):
        if self.procs_override:
            return os.open(self.procs_override, os.O_WRONLY | os.O_CLOEXEC)
        return super().open_procs(leaf)

    def main(self, leaf):
        text = (leaf / 'cgroup.procs').read_text().strip()
        return int(text) if text else None

    def kill(self, leaf, deadline=None):
        if self.interrupts:
            self.interrupts -= 1
            raise KeyboardInterrupt('second interrupt during cleanup')
        if self.kill_broken:
            raise OSError('cgroup.kill refused')
        main = self.main(leaf)
        if main:
            try:
                os.killpg(main, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def populated(self, leaf, deadline=None):
        if self.events_broken:
            return None
        main = self.main(leaf)
        if main is None:
            return False
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                try:
                    fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                except OSError:
                    continue
                if fields[0] != 'Z' and int(fields[2]) == main:
                    return True
        return False

    def member(self, pid):
        if self.member_override:
            return self.member_override
        for leaf in self.mount.rglob('cgroup.procs'):
            if leaf.read_text().strip() == str(pid):
                return leaf.parent
        return None

    def remove(self, leaf, deadline=None, identity=None):
        if identity is not None and cgroup_identity(leaf) != identity:
            raise OSError(errno.EBUSY, 'cgroup identity changed')
        for name in ('cgroup.procs', 'cgroup.kill', 'cgroup.events'):
            (leaf / name).unlink(missing_ok=True)
        os.rmdir(leaf)
        if self.interrupt_after_remove:
            self.interrupt_after_remove = False
            raise KeyboardInterrupt('interrupt right after rmdir')


class LeafTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.temp.name))
        self.mount = base / 'cgroup'
        self.root_path = self.mount / 'user.slice' / 'delegated'
        self.root_path.mkdir(parents=True)
        for name in ('cgroup.procs', 'cgroup.kill', 'cgroup.controllers'):
            (self.root_path / name).touch()
        self.work = base / 'work'
        self.work.mkdir()
        self.fs = FakeCgroup(self.mount)
        self.root = DelegatedRoot(self.root_path, self.fs)
        self.cleanup_pids = []

    def tearDown(self):
        for pid in self.cleanup_pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.temp.cleanup()

    def sh(self, script):
        return ['/bin/sh', '-c', script]

    def leaves(self):
        return sorted(p.name for p in self.root_path.iterdir() if p.is_dir())

    def grandchild(self):
        return int(wait_file(self.work / 'gc'))

    # Exit paths
    def test_normal_return_kills_descendants(self):
        with self.root.launch('run-a', self.sh('sleep 60 & echo $! > gc; exit 3'),
                              cwd=self.work) as run:
            self.assertEqual(run.wait(5), 3)
            gc = self.grandchild()
            self.assertTrue(alive(gc))
        self.assertEqual(run.outcome.state, 'leaf_empty')
        self.assertEqual(run.outcome.returncode, 3)
        self.assertTrue(run.outcome.main_exit_seen)
        self.assertFalse(alive(gc))
        self.assertEqual(self.leaves(), [])

    def test_exception_kills_tree_and_propagates(self):
        with self.assertRaisesRegex(ValueError, 'body'):
            with self.root.launch('run-a', self.sh('sleep 60 & echo $! > gc; wait'),
                                  cwd=self.work) as run:
                gc = self.grandchild()
                raise ValueError('body')
        self.assertEqual(run.outcome.state, 'leaf_empty')
        self.assertFalse(alive(gc))
        self.assertFalse(alive(run.pid))

    def test_cancellation_kills_tree(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.root.launch('run-a', self.sh('sleep 60 & echo $! > gc; wait'),
                                  cwd=self.work) as run:
                gc = self.grandchild()
                raise KeyboardInterrupt
        self.assertEqual(run.outcome.state, 'leaf_empty')
        self.assertFalse(alive(gc))

    def test_explicit_close_idempotent_and_thread_safe(self):
        run = self.root.launch('run-a', ['sleep', '60'])
        results = []
        threads = [threading.Thread(target=lambda: results.append(run.close())) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({id(r) for r in results}), 1)
        self.assertEqual(results[0].state, 'leaf_empty')
        self.assertEqual(results[0].returncode, -signal.SIGKILL)

    # Launch race and failures
    def test_target_runs_already_inside_leaf(self):
        with self.root.launch('run-a', self.sh(f'cat {self.root_path}/run-a/cgroup.procs > seen;'
                                               ' echo $$ > me'), cwd=self.work) as run:
            run.wait(5)
        self.assertEqual(wait_file(self.work / 'seen'), str(run.pid))
        self.assertEqual(wait_file(self.work / 'me'), str(run.pid))

    def test_join_failure_never_execs_target(self):
        self.fs.procs_override = '/dev/full'
        with self.assertRaisesRegex(LaunchError, 'before exec'):
            self.root.launch('run-a', self.sh('touch ran'), cwd=self.work)
        time.sleep(0.1)
        self.assertFalse((self.work / 'ran').exists())
        self.assertEqual(self.leaves(), [])

    def test_exec_failure_is_launch_error_and_cleaned(self):
        with self.assertRaisesRegex(LaunchError, 'FileNotFoundError'):
            self.root.launch('run-a', ['/nonexistent/engine'])
        self.assertEqual(self.leaves(), [])

    def test_wrong_membership_refused_and_killed(self):
        self.fs.member_override = self.mount / 'elsewhere'
        with patch.object(cgroup_leaf.LeafRun, '_cleanup_after',
                          autospec=True, side_effect=cgroup_leaf.LeafRun._cleanup_after) as spy:
            with self.assertRaisesRegex(LaunchError, 'not in the leaf'):
                self.root.launch('run-a', ['sleep', '60'])
        run = spy.call_args.args[0]
        self.assertEqual(run.outcome.state, 'leaf_empty')
        self.assertFalse(alive(run.pid))
        self.assertEqual(self.leaves(), [])

    def test_popen_failure_keeps_original_error(self):
        with self.assertRaises(FileNotFoundError):
            self.root.launch('run-a', ['sleep', '1'], cwd=self.work / 'missing')
        self.assertEqual(self.leaves(), [])

    def test_no_fd_leak_on_failures(self):
        before = len(os.listdir('/proc/self/fd'))
        for n in range(5):
            with self.assertRaises(FileNotFoundError):
                self.root.launch(f'run-{n}', ['sleep', '1'], cwd=self.work / 'missing')
            with self.assertRaises(LaunchError):
                self.root.launch(f'run-x{n}', ['/nonexistent/engine'])
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    # Evidence honesty
    def test_never_terminated_while_populated(self):
        self.fs.kill_broken = True  # pidfd kills the main, the grandchild keeps the leaf populated
        with self.assertRaises(CleanupIncomplete) as caught:
            with self.root.launch('run-a', self.sh('sleep 60 & echo $! > gc; wait'),
                                  cwd=self.work, kill_timeout=0.3) as run:
                gc = self.grandchild()
        self.cleanup_pids.append(run.pid)
        self.assertEqual(caught.exception.outcome.state, 'running')
        self.assertTrue(caught.exception.outcome.populated)
        self.assertTrue(alive(gc))
        self.assertEqual(self.leaves(), ['run-a'])  # populated leaf is never removed

    def test_escapee_unseen_so_never_terminated(self):
        # S5 / C1-27: a same-uid process that leaves the leaf is unseen and the leaf reads
        # empty while it lives. The fake models leaving as setsid (a new process group).
        script = ('setsid sh -c \'echo $$ > gc; exec sleep 60\' & '
                  'while [ ! -s gc ]; do sleep 0.01; done; exit 0')
        with self.root.launch('run-a', self.sh(script), cwd=self.work) as run:
            self.assertEqual(run.wait(5), 0)
            escapee = self.grandchild()
        self.cleanup_pids.append(escapee)
        self.assertTrue(alive(escapee))
        self.assertEqual(run.outcome.state, 'leaf_empty')
        self.assertFalse(run.outcome.escape_observable)
        self.assertEqual(self.leaves(), [])

    def test_launcher_never_books_terminated(self):
        source = Path(cgroup_leaf.__file__).read_text()
        self.assertNotIn("state = 'terminated'", source)
        self.assertNotIn('escape_observable=True', source)

    def test_unreadable_events_is_unknown(self):
        self.fs.events_broken = True
        with self.assertRaises(CleanupIncomplete) as caught:
            with self.root.launch('run-a', ['sleep', '60'], kill_timeout=0.2):
                pass
        self.assertEqual(caught.exception.outcome.state, 'unknown')
        self.assertEqual(self.leaves(), ['run-a'])

    def test_cleanup_failure_does_not_hide_original(self):
        self.fs.kill_broken = True
        self.fs.events_broken = True
        with self.assertRaisesRegex(ValueError, 'original') as caught:
            with self.root.launch('run-a', ['sleep', '60'], kill_timeout=0.2) as run:
                raise ValueError('original')
        self.cleanup_pids.append(run.pid)
        self.assertTrue(any('cleanup incomplete' in n for n in caught.exception.__notes__))

    def test_cleanup_raising_does_not_hide_original(self):
        with self.assertLogs(level='ERROR'), \
             self.assertRaisesRegex(ValueError, 'original') as caught:
            with self.root.launch('run-a', ['sleep', '60']) as run:
                self.fs.populated = lambda leaf, deadline=None: 1 / 0
                raise ValueError('original')
        run.proc.wait(5)  # killed by cgroup.kill and pidfd before the observer raised
        self.assertTrue(any('ZeroDivisionError' in n for n in caught.exception.__notes__))

    def orphan_leaf(self, name):
        leaf = self.root_path / name
        self.fs.make_leaf(leaf)
        orphan = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.cleanup_pids.append(orphan.pid)
        (leaf / 'cgroup.procs').write_text(str(orphan.pid))
        return orphan

    def test_sweep_of_unseen_leaf_is_empty_not_terminated(self):
        orphan = self.orphan_leaf('run-left')
        outcomes = self.root.sweep(keep=())
        orphan.wait(5)
        self.assertEqual(outcomes['run-left'].state, 'empty')
        self.assertFalse(outcomes['run-left'].main_exit_seen)
        self.assertEqual(self.leaves(), [])

    # Restart (finding 1): the registry's live runs survive the startup sweep
    def test_sweep_spares_registered_runs_and_adopt_kills_later(self):
        live = self.orphan_leaf('run-live')
        other = self.orphan_leaf('run-other-launcher')
        stale = self.orphan_leaf('run-stale')
        outcomes = self.root.sweep(keep={'run-live', 'run-other-launcher'})
        stale.wait(5)
        self.assertEqual(set(outcomes), {'run-stale'})
        self.assertTrue(alive(live.pid) and alive(other.pid))
        self.assertEqual(self.leaves(), ['run-live', 'run-other-launcher'])
        outcome = self.root.adopt('run-live').close()
        live.wait(5)
        self.assertEqual(outcome.state, 'empty')  # exit not seen by this process
        self.assertFalse(outcome.main_exit_seen)
        self.assertTrue(alive(other.pid))
        self.assertEqual(self.leaves(), ['run-other-launcher'])

    def test_sweep_has_no_kill_everything_default(self):
        self.assertIs(inspect.signature(DelegatedRoot.sweep).parameters['keep'].default,
                      inspect.Parameter.empty)
        for bad in ('run-live', b'run-live', ['../run-x']):
            with self.assertRaises((TypeError, ValueError), msg=repr(bad)):
                self.root.sweep(keep=bad)

    def test_adopt_refuses_missing_or_bad_leaf(self):
        with self.assertRaises(PermissionError):
            self.root.adopt('run-missing')
        with self.assertRaises(ValueError):
            self.root.adopt('../run-x')

    # Unseen exit (finding 4): an empty leaf without a seen exit is kept
    def test_empty_leaf_with_unseen_exit_is_kept_with_its_pidfd(self):
        run = self.root.launch('run-a', ['sleep', '60'], kill_timeout=0.2)
        self.cleanup_pids.append(run.pid)
        self.fs.kill_broken = True
        with patch.object(self.fs, 'populated', return_value=False), \
             patch.object(cgroup_leaf.signal, 'pidfd_send_signal', side_effect=PermissionError):
            outcome = run.close()
        self.assertEqual(outcome.state, 'unknown')
        self.assertFalse(outcome.main_exit_seen)
        self.assertTrue(alive(run.pid))
        self.assertEqual(self.leaves(), ['run-a'])  # evidence kept
        self.assertIsNotNone(run.pidfd)  # kill handle kept
        self.fs.kill_broken = False
        retried = run.close()  # not final, so close() retries the kill
        self.assertEqual(retried.state, 'leaf_empty')
        self.assertEqual(retried.returncode, -signal.SIGKILL)
        self.assertIsNone(run.pidfd)
        self.assertEqual(self.leaves(), [])
        self.assertIs(run.close(), retried)  # final outcome is cached

    # Interrupted cleanup (finding 5): retried on every exit path
    def test_interrupt_during_cleanup_after_exception_is_retried(self):
        with self.assertLogs(level='ERROR'), \
             self.assertRaisesRegex(ValueError, 'body') as caught:
            with self.root.launch('run-a', self.sh('sleep 60 & echo $! > gc; wait'),
                                  cwd=self.work) as run:
                gc = self.grandchild()
                self.fs.interrupts = 2
                raise ValueError('body')
        self.assertEqual(run.outcome.state, 'leaf_empty')
        self.assertFalse(alive(gc))
        self.assertEqual(self.leaves(), [])
        notes = [n for n in caught.exception.__notes__ if 'KeyboardInterrupt' in n]
        self.assertEqual(len(notes), 2)

    def test_interrupt_during_cleanup_on_normal_exit_is_retried_and_propagates(self):
        with self.assertLogs(level='ERROR'), self.assertRaises(KeyboardInterrupt):
            with self.root.launch('run-a', self.sh('sleep 60 & echo $! > gc; wait'),
                                  cwd=self.work) as run:
                gc = self.grandchild()
                self.fs.interrupts = 1
        self.assertEqual(run.outcome.state, 'leaf_empty')
        self.assertFalse(alive(gc))
        self.assertEqual(self.leaves(), [])

    def test_interrupt_during_launch_cleanup_is_retried(self):
        self.fs.member_override = self.mount / 'elsewhere'
        self.fs.interrupts = 1
        with patch.object(cgroup_leaf.LeafRun, '_cleanup_after',
                          autospec=True, side_effect=cgroup_leaf.LeafRun._cleanup_after) as spy, \
             self.assertLogs(level='ERROR'), self.assertRaises(LaunchError):
            self.root.launch('run-a', ['sleep', '60'])
        run = spy.call_args.args[0]
        self.assertEqual(run.outcome.state, 'leaf_empty')
        self.assertFalse(alive(run.pid))

    def test_interrupt_after_rmdir_rebuilds_outcome(self):
        with self.assertLogs(level='ERROR'), self.assertRaises(KeyboardInterrupt):
            with self.root.launch('run-a', ['sleep', '60']) as run:
                self.fs.interrupt_after_remove = True
        self.assertEqual(run.outcome.state, 'leaf_empty')
        self.assertEqual(run.outcome.errors, ())
        self.assertEqual(self.leaves(), [])

    def test_interrupt_storm_keeps_leaf_for_the_sweep(self):
        with self.assertLogs(level='ERROR'), \
             self.assertRaisesRegex(ValueError, 'body') as caught:
            with self.root.launch('run-a', self.sh('sleep 60 & echo $! > gc; wait'),
                                  cwd=self.work) as run:
                gc = self.grandchild()
                self.fs.interrupts = cgroup_leaf.CLEANUP_ATTEMPTS
                raise ValueError('body')
        self.assertTrue(any('abandoned' in n for n in caught.exception.__notes__))
        self.assertIsNone(run.outcome)
        self.assertEqual(self.leaves(), ['run-a'])
        outcomes = self.root.sweep(keep=())  # the durable backstop after a crash
        self.assertEqual(outcomes['run-a'].state, 'empty')
        run.proc.wait(5)
        self.assertFalse(alive(gc))

    # Path constraints
    def test_root_constraints(self):
        (self.mount.parent / 'outside').mkdir()
        (self.mount.parent / 'outside' / 'cgroup.procs').touch()
        (self.mount.parent / 'outside' / 'cgroup.kill').touch()
        (self.mount.parent / 'link').symlink_to(self.root_path)
        for bad in (self.mount.parent / 'outside', self.mount, self.mount.parent / 'link',
                    Path('user.slice/delegated')):
            with self.assertRaises(PermissionError, msg=str(bad)):
                DelegatedRoot(bad, self.fs)
        (self.root_path / 'cgroup.kill').unlink()
        with self.assertRaisesRegex(PermissionError, 'cgroup.kill'):
            DelegatedRoot(self.root_path, self.fs)

    def test_leaf_names_constrained(self):
        for bad in ('../run-x', 'x', 'run-a/b', 'run-', 'run-' + 'a' * 65, 'run-a.b'):
            with self.assertRaises(ValueError, msg=bad):
                self.root.launch(bad, ['true'])
        self.assertEqual(self.leaves(), [])

    def test_existing_leaf_never_reused(self):
        self.fs.make_leaf(self.root_path / 'run-a')
        with self.assertRaises(FileExistsError):
            self.root.launch('run-a', ['true'])

    def test_empty_argv_refused(self):
        with self.assertRaises(ValueError):
            self.root.launch('run-a', [])

    def test_same_uid_only_no_cross_uid_switch(self):
        params = set(inspect.signature(DelegatedRoot.launch).parameters)
        self.assertFalse(params & {'user', 'group', 'uid', 'gid', 'extra_groups'})
        source = Path(cgroup_leaf.__file__).read_text()
        for forbidden in ('setuid', 'setresuid', '--uid', 'sudo'):
            self.assertNotIn(forbidden, source)

    def test_source_avoids_forbidden_primitives(self):
        source = Path(cgroup_leaf.__file__).read_text()
        for forbidden in ('preexec_fn', 'PDEATHSIG', 'prctl'):
            self.assertNotIn(forbidden, source)

    def test_unknown_retry_does_not_delete_foreign_cgroup(self):
        # Empty replacement: an emptiness check alone would allow rmdir.
        run = self.root.launch('run-a', ['sleep', '60'], kill_timeout=0.2)
        self.cleanup_pids.append(run.pid)
        self.fs.kill_broken = True
        with patch.object(self.fs, 'populated', return_value=False), \
             patch.object(cgroup_leaf.signal, 'pidfd_send_signal', side_effect=PermissionError):
            outcome = run.close()
        self.assertEqual(outcome.state, 'unknown')
        leaf = self.root_path / 'run-a'
        leaf.rename(self.root_path / 'run-a-old')
        self.fs.make_leaf(leaf)
        kills = []
        real_kill = self.fs.kill

        def spy(target, deadline=None):
            kills.append(target)
            return real_kill(target)

        self.fs.kill = spy
        self.fs.kill_broken = False
        retried = run.close()
        self.assertEqual(kills, [])
        self.assertEqual(retried.state, 'unknown')
        self.assertTrue((leaf / 'cgroup.procs').is_file())
        self.assertTrue((self.root_path / 'run-a-old').is_dir())
        try:
            os.killpg(run.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        run.proc.wait(2)

    def test_unknown_identity_does_not_delete_foreign_cgroup(self):
        leaf = self.root_path / 'run-foreign'
        run = cgroup_leaf.LeafRun(self.fs, leaf, 0.2)
        self.fs.make_leaf(leaf)
        (leaf / 'marker').write_text('foreign')
        outcome = run.close()
        self.assertEqual(outcome.state, 'unknown')
        self.assertEqual((leaf / 'marker').read_text(), 'foreign')


def cgroup_identity(path):
    st = os.lstat(path)
    if stat_is_symlink(st) or not stat.S_ISDIR(st.st_mode):
        return None
    return (st.st_dev, st.st_ino)


def stat_is_symlink(st):
    return stat.S_ISLNK(st.st_mode)


def write_cgroup(path, procs=''):
    """A tmpdir stand-in for one cgroup directory. Never a host cgroupfs path."""
    path.mkdir(parents=True, exist_ok=True)
    (path / 'cgroup.procs').write_text(procs)
    populated = '1' if procs.strip() else '0'
    (path / 'cgroup.events').write_text(f'populated {populated}\n')
    (path / 'cgroup.kill').write_text('')
    (path / 'cgroup.controllers').touch()


class TmpCgroup(Cgroup2):
    """Cgroup2 on a tmpdir. ``_rmdir`` models cgroupfs: interface files do not block removal."""
    def __init__(self, mount):
        super().__init__(mount)
        self.rmdir_calls = []

    def _rmdir(self, directory):
        directory = Path(directory)
        self.rmdir_calls.append(directory)
        procs = directory / 'cgroup.procs'
        if procs.is_file() and procs.read_text().strip():
            raise OSError(errno.EBUSY, 'procs not empty')
        if any(entry.is_dir() and not entry.is_symlink() for entry in directory.iterdir()):
            raise OSError(errno.EBUSY, 'child cgroup remains')
        for entry in list(directory.iterdir()):
            if entry.is_dir() or entry.is_symlink():
                raise OSError(errno.EBUSY, 'not an interface file')
            entry.unlink()
        os.rmdir(directory)


class RemovalContractTests(unittest.TestCase):
    """Cleanup contracts on a fake cgroup tree in a tmpdir. No host cgroup writes."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.temp.name))
        self.mount = base / 'cgroup'
        self.root_path = self.mount / 'user.slice' / 'delegated'
        self.root_path.mkdir(parents=True)
        for name in ('cgroup.procs', 'cgroup.kill', 'cgroup.controllers'):
            (self.root_path / name).touch()
        self.fs = TmpCgroup(self.mount)

    def tearDown(self):
        self.temp.cleanup()

    def test_rmdir_only_after_procs_and_children_are_empty(self):
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        write_cgroup(leaf / 'sub', procs='9\n')
        with patch.object(cgroup_leaf.os, 'rmdir', side_effect=AssertionError('rmdir')):
            with self.assertRaises(OSError) as caught:
                self.fs.remove(leaf, identity=cgroup_identity(leaf),
                               deadline=time.monotonic() + 0.5)
        self.assertEqual(caught.exception.errno, errno.EBUSY)
        self.assertTrue((leaf / 'sub' / 'cgroup.procs').is_file())
        self.assertEqual(self.fs.rmdir_calls, [])

    def test_ebusy_while_still_populated_is_not_spun(self):
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='9\n')
        start = time.monotonic()
        with self.assertRaises(OSError) as caught:
            self.fs.remove(leaf, identity=cgroup_identity(leaf),
                           deadline=time.monotonic() + 2)
        self.assertEqual(caught.exception.errno, errno.EBUSY)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(self.fs.rmdir_calls, [])
        self.assertTrue(leaf.is_dir())

    def test_ebusy_while_empty_retries_until_deadline(self):
        # Dying-task race: procs and children are empty, rmdir still returns EBUSY.
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        real = TmpCgroup._rmdir

        def always_busy(fs, directory):
            fs.rmdir_calls.append(Path(directory))
            raise OSError(errno.EBUSY, 'dying task')

        self.fs._rmdir = always_busy.__get__(self.fs, TmpCgroup)
        start = time.monotonic()
        with self.assertRaises(OSError) as caught:
            self.fs.remove(leaf, identity=cgroup_identity(leaf),
                           deadline=time.monotonic() + 0.25)
        elapsed = time.monotonic() - start
        self.assertEqual(caught.exception.errno, errno.EBUSY)
        self.assertGreater(len(self.fs.rmdir_calls), 1)
        self.assertLessEqual(len(self.fs.rmdir_calls), 40)
        self.assertLess(elapsed, 1.2)
        self.assertTrue(leaf.is_dir())
        self.assertIs(real, TmpCgroup._rmdir)

    def test_ebusy_while_empty_then_succeeds(self):
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        write_cgroup(leaf / 'sub', procs='')
        remaining = {'n': 2}
        real = TmpCgroup._rmdir.__get__(self.fs, TmpCgroup)

        def flaky(directory):
            if remaining['n']:
                remaining['n'] -= 1
                self.fs.rmdir_calls.append(Path(directory))
                raise OSError(errno.EBUSY, 'dying task')
            real(directory)

        self.fs._rmdir = flaky
        self.fs.remove(leaf, identity=cgroup_identity(leaf),
                       deadline=time.monotonic() + 1)
        self.assertFalse(leaf.exists())

    def test_unknown_identity_does_not_remove_directory(self):
        leaf = self.root_path / 'run-foreign'
        leaf.mkdir()
        with self.assertRaises(OSError) as caught:
            self.fs.remove(leaf)
        self.assertEqual(caught.exception.errno, errno.EBUSY)
        self.assertTrue(leaf.is_dir())

    def test_changed_identity_does_not_remove_replacement(self):
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        old = cgroup_identity(leaf)
        for entry in list(leaf.iterdir()):
            entry.unlink()
        leaf.rmdir()
        leaf.mkdir()
        (leaf / 'marker').write_text('foreign')
        with self.assertRaises(OSError) as caught:
            self.fs.remove(leaf, identity=old, deadline=time.monotonic() + 0.5)
        self.assertEqual(caught.exception.errno, errno.EBUSY)
        self.assertEqual((leaf / 'marker').read_text(), 'foreign')
        self.assertEqual(self.fs.rmdir_calls, [])

    def test_populated_and_kill_files_do_not_hang_cleanup(self):
        leaf = self.root_path / 'run-a'
        leaf.mkdir()
        os.mkfifo(leaf / 'cgroup.events')
        os.mkfifo(leaf / 'cgroup.kill')
        (leaf / 'cgroup.procs').write_text('')
        box = {}

        def read_events():
            try:
                box['populated'] = self.fs.populated(leaf)
            except BaseException as exc:
                box['populated_error'] = exc

        def write_kill():
            try:
                self.fs.kill(leaf)
            except OSError as exc:
                box['kill_error'] = exc

        readers = [threading.Thread(target=read_events), threading.Thread(target=write_kill)]
        for thread in readers:
            thread.start()
        for thread in readers:
            thread.join(1.5)
        hung = [thread for thread in readers if thread.is_alive()]
        if hung:
            # Unblock a blocking open/read so the suite can finish, then fail.
            try:
                writer = os.open(leaf / 'cgroup.events', os.O_WRONLY | os.O_NONBLOCK)
                os.write(writer, b'populated 0\n')
                os.close(writer)
            except OSError:
                pass
            try:
                reader = os.open(leaf / 'cgroup.kill', os.O_RDONLY | os.O_NONBLOCK)
                os.close(reader)
            except OSError:
                pass
            for thread in hung:
                thread.join(2)
            self.fail('cgroup file operation blocked past the cleanup window')
        self.assertNotIn('populated_error', box)
        self.assertIsNone(box.get('populated'))
        self.assertIn('kill_error', box)

    def test_sweep_removes_unregistered_owned_names_only(self):
        free = self.root_path / 'run-not.valid'
        busy = self.root_path / 'run-busy.name'
        foreign = self.root_path / 'app.slice'
        write_cgroup(free, procs='')
        write_cgroup(busy, procs='')
        (busy / 'cgroup.procs').write_text('5\n')
        (busy / 'cgroup.events').write_text('populated 0\n')
        foreign.mkdir()
        (foreign / 'marker').write_text('foreign')
        link = self.root_path / 'run-link'
        link.symlink_to(foreign, target_is_directory=True)
        kept = self.root_path / 'run-kept'
        write_cgroup(kept, procs='')
        root = DelegatedRoot(self.root_path, self.fs)
        outcomes = root.sweep(keep={'run-kept'})
        self.assertFalse(free.exists(), outcomes)
        self.assertTrue(busy.is_dir())
        self.assertEqual((busy / 'cgroup.procs').read_text(), '5\n')
        self.assertEqual((foreign / 'marker').read_text(), 'foreign')
        self.assertTrue(link.is_symlink())
        self.assertTrue(kept.is_dir())
        self.assertNotIn(busy, self.fs.rmdir_calls)
        self.assertIn('run-not.valid', outcomes)
        self.assertNotIn('app.slice', outcomes)
        self.assertNotIn('run-link', outcomes)
        self.assertNotIn('run-kept', outcomes)

    def test_close_does_not_hang_on_slow_events_or_exit(self):
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        os.unlink(leaf / 'cgroup.events')
        os.mkfifo(leaf / 'cgroup.events')
        # A slow exit: wait() holds the child unreaped. Cleanup must still return.
        proc = subprocess.Popen(['sleep', '30'], start_new_session=True)
        waiter = threading.Thread(target=proc.wait)
        waiter.start()

        def stop_slow_exit():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            waiter.join(2)

        self.addCleanup(stop_slow_exit)
        run = LeafRun(self.fs, leaf, 0.3)
        run.proc = proc

        def populated(leaf, *args, **kwargs):
            return Cgroup2.populated(self.fs, leaf, *args, **kwargs)

        self.fs.populated = populated
        box = {}

        def cleanup():
            try:
                box['outcome'] = run.close()
            except BaseException as exc:
                box['error'] = exc

        thread = threading.Thread(target=cleanup)
        start = time.monotonic()
        try:
            thread.start()
            thread.join(1.5)
            if thread.is_alive():
                try:
                    writer = os.open(leaf / 'cgroup.events', os.O_WRONLY | os.O_NONBLOCK)
                    os.write(writer, b'populated 1\n')
                    os.close(writer)
                except OSError:
                    pass
                thread.join(2)
                self.fail(f'close() blocked for {time.monotonic() - start:.2f}s')
            self.assertNotIn('error', box)
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertNotEqual(box['outcome'].state, 'leaf_empty')
            self.assertTrue(leaf.is_dir())
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            waiter.join(2)


class RealCgroupTests(unittest.TestCase):
    """Live cell: runs only when a founder points RECURSION_CGROUP_ROOT at a delegated root."""
    @unittest.skipUnless(os.environ.get('RECURSION_CGROUP_ROOT'), 'no delegated cgroup root')
    def test_real_leaf_kill_tree(self):
        root = DelegatedRoot(os.environ['RECURSION_CGROUP_ROOT'])
        with tempfile.TemporaryDirectory() as work:
            with root.launch(f'run-test{os.getpid()}', ['/bin/sh', '-c',
                             'sleep 60 & echo $! > gc; setsid sleep 60 & wait'], cwd=work) as run:
                gc = int(wait_file(Path(work) / 'gc'))
            self.assertEqual(run.outcome.state, 'leaf_empty')
            self.assertFalse(alive(gc))


if __name__ == '__main__':
    unittest.main()
