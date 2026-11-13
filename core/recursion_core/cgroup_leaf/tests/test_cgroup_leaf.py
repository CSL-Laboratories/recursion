"""CSL-728 same-uid leaf tests. Fake cgroupfs with real processes; not runtime certification.

The fake models a leaf as the process group of its main process (started with
start_new_session). It exercises the real trampoline, pidfd and evidence logic; it does
not prove kernel cgroup behaviour. Set RECURSION_CGROUP_ROOT to a writable delegated
cgroup v2 directory containing this process to run the live cell.
"""
import errno
import gc
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
                                        LaunchError, LeafRun, cgroup_identity)


def alive(pid):
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except OSError:
        return False
    return state != 'Z'


def _is_leaf_directory_open(path, flags, leaf):
    """True when ``os.open`` is opening ``leaf`` itself as a directory.

    Matches a full path and a parent-dirfd open of the leaf's name. Does not
    match ``cgroup.kill`` or other files opened relative to a leaf fd.
    """
    if isinstance(path, int) or not (flags & os.O_DIRECTORY):
        return False
    text = os.fsdecode(path)
    return text == leaf.name or Path(text) == leaf


def _stop_pid(pid):
    if not isinstance(pid, int):
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


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
        try:
            cgroup_leaf.mark_owned(leaf)
        except OSError:
            cgroup_leaf._discard_new_leaf(leaf)
            raise
        return cgroup_identity(leaf)

    def open_procs(self, leaf, dirfd=None):
        if self.procs_override:
            return os.open(self.procs_override, os.O_WRONLY | os.O_CLOEXEC)
        return super().open_procs(leaf, dirfd=dirfd)

    def main(self, leaf):
        text = (leaf / 'cgroup.procs').read_text().strip()
        return int(text) if text else None

    def kill(self, leaf, deadline=None, dirfd=None):
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

    def populated(self, leaf, deadline=None, dirfd=None):
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

    def remove(self, leaf, deadline=None, identity=None, dirfd=None):
        if identity is not None and cgroup_identity(leaf) != identity:
            raise OSError(errno.EBUSY, 'cgroup identity changed')
        if dirfd is not None:
            info = os.fstat(dirfd)
            if identity is not None and (info.st_dev, info.st_ino) != identity:
                raise OSError(errno.EBUSY, 'cgroup identity changed')
        for name in ('cgroup.procs', 'cgroup.kill', 'cgroup.events'):
            (leaf / name).unlink(missing_ok=True)
        os.rmdir(leaf)
        if dirfd is not None and os.fstat(dirfd).st_nlink != 0:
            raise OSError(errno.EBUSY, 'cgroup identity still present after rmdir')
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
        self.cleanup_procs = []

    def tearDown(self):
        for pid in self.cleanup_pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for proc in self.cleanup_procs:
            try:
                proc.wait(2)
            except Exception:
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
        with self.assertRaisesRegex(LaunchError, 'pinned leaf'):
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
                self.fs.populated = lambda leaf, deadline=None, dirfd=None: 1 / 0
                raise ValueError('original')
        run.proc.wait(5)  # killed by cgroup.kill and pidfd before the observer raised
        self.assertTrue(any('ZeroDivisionError' in n for n in caught.exception.__notes__))

    def orphan_leaf(self, name):
        leaf = self.root_path / name
        self.fs.make_leaf(leaf)
        orphan = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.cleanup_pids.append(orphan.pid)
        self.cleanup_procs.append(orphan)
        (leaf / 'cgroup.procs').write_text(str(orphan.pid))
        return orphan

    def test_sweep_of_unseen_leaf_is_empty_not_terminated(self):
        orphan = self.orphan_leaf('run-left')
        result = self.root.sweep(keep=())
        orphan.wait(5)
        self.assertEqual(result.outcomes['run-left'].state, 'empty')
        self.assertFalse(result.outcomes['run-left'].main_exit_seen)
        self.assertEqual(result.reported, ())
        self.assertEqual(self.leaves(), [])

    # Restart (finding 1): the registry's live runs survive the startup sweep
    def test_sweep_spares_registered_runs_and_adopt_kills_later(self):
        live = self.orphan_leaf('run-live')
        other = self.orphan_leaf('run-other-launcher')
        stale = self.orphan_leaf('run-stale')
        result = self.root.sweep(keep={'run-live', 'run-other-launcher'})
        stale.wait(5)
        self.assertEqual(set(result.outcomes), {'run-stale'})
        self.assertEqual(result.reported, ())
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

    def test_adopt_refuses_leaf_without_ownership_marker(self):
        leaf = self.root_path / 'run-foreign'
        self.fs.make_leaf(leaf)
        os.removexattr(leaf, cgroup_leaf.OWNERSHIP_XATTR)
        with self.assertRaisesRegex(PermissionError, 'ownership marker'):
            self.root.adopt('run-foreign')
        self.assertEqual((leaf / 'cgroup.procs').read_text(), '')
        self.assertTrue(leaf.is_dir())

    def test_adopt_context_accepts_empty_outcome(self):
        orphan = self.orphan_leaf('run-live')
        with self.root.adopt('run-live') as run:
            self.assertIsNone(run.outcome)
        orphan.wait(5)
        self.assertEqual(run.outcome.state, 'empty')
        self.assertEqual(run.outcome.errors, ())
        self.assertFalse(run.outcome.main_exit_seen)
        self.assertEqual(self.leaves(), [])

    def test_adopt_context_raises_when_cleanup_does_not_finish(self):
        orphan = self.orphan_leaf('run-stuck')
        self.addCleanup(orphan.wait)
        self.fs.kill_broken = True
        with self.assertLogs(level='ERROR'), self.assertRaises(CleanupIncomplete) as caught:
            with self.root.adopt('run-stuck', kill_timeout=0.2):
                pass
        self.assertEqual(caught.exception.outcome.state, 'running')
        self.assertTrue(caught.exception.outcome.errors)
        self.assertTrue(alive(orphan.pid))
        self.assertEqual(self.leaves(), ['run-stuck'])

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
        result = self.root.sweep(keep=())  # the durable backstop after a crash
        self.assertEqual(result.outcomes['run-a'].state, 'empty')
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

    def test_rename_before_close_still_kills_child(self):
        # The child renames the leaf aside before close(). The pidfd kill must
        # not wait for the path to still name the held inode.
        run = self.root.launch('run-a', ['sleep', '60'])
        self.cleanup_pids.append(run.pid)
        leaf = self.root_path / 'run-a'
        escaped = self.root_path / 'escaped.scope'
        held = run.identity
        leaf.rename(escaped)
        with self.assertLogs(cgroup_leaf.log.name, level='ERROR'):
            outcome = run.close()
        self.assertFalse(alive(run.pid))
        self.assertIsNotNone(run.proc.returncode)
        self.assertNotIn(outcome.state, ('empty', 'leaf_empty'))
        self.assertIsNone(run.pidfd)
        self.assertEqual(run.identity, held)
        self.assertEqual(cgroup_identity(escaped), held)
        self.assertIsNotNone(run.dirfd)
        self.assertEqual(os.fstat(run.dirfd).st_ino, held[1])
        self.assertTrue(escaped.is_dir())

    def test_chmod_zero_then_restore_retries_kill(self):
        # lstat fails while the root is mode 0. close() must still kill the
        # child and keep the inode so a later close, after access returns, can
        # cgroup.kill and remove that same directory.
        run = self.root.launch('run-a', ['sleep', '60'], kill_timeout=0.3)
        self.cleanup_pids.append(run.pid)
        held = run.identity
        self.root_path.chmod(0)
        try:
            with self.assertLogs(cgroup_leaf.log.name, level='ERROR'):
                outcome = run.close()
        finally:
            self.root_path.chmod(0o755)
        self.assertFalse(alive(run.pid))
        self.assertNotIn(outcome.state, ('empty', 'leaf_empty'))
        self.assertEqual(run.identity, held)
        self.assertIsNotNone(run.dirfd)
        self.assertIsNone(run.pidfd)
        retried = run.close()
        self.assertEqual(retried.state, 'leaf_empty')
        self.assertFalse(alive(run.pid))
        self.assertEqual(self.leaves(), [])

    def test_eio_lstat_still_kills_child(self):
        run = self.root.launch('run-a', ['sleep', '60'], kill_timeout=0.3)
        self.cleanup_pids.append(run.pid)
        leaf = self.root_path / 'run-a'
        held = run.identity
        real = os.lstat

        def eio(path, *args, **kwargs):
            if not isinstance(path, int) and Path(os.fsdecode(path)) == leaf:
                raise OSError(errno.EIO, 'eio')
            return real(path, *args, **kwargs)

        with patch.object(cgroup_leaf.os, 'lstat', eio), \
             self.assertLogs(cgroup_leaf.log.name, level='ERROR'):
            outcome = run.close()
        self.assertFalse(alive(run.pid))
        self.assertNotIn(outcome.state, ('empty', 'leaf_empty'))
        self.assertEqual(run.identity, held)
        self.assertEqual(cgroup_identity(leaf), held)
        self.assertIsNotNone(run.dirfd)
        self.assertIsNone(run.pidfd)

    def test_kill_eacces_and_rename_still_kills_child(self):
        # cgroup.kill fails and the name is swapped before the pidfd signal
        # used to run. The child must still die, and the renamed inode stays.
        run = self.root.launch('run-a', ['sleep', '60'], kill_timeout=0.3)
        self.cleanup_pids.append(run.pid)
        leaf = self.root_path / 'run-a'
        escaped = self.root_path / 'escaped.scope'
        held = run.identity

        def kill(target, deadline=None, dirfd=None):
            if leaf.exists():
                leaf.rename(escaped)
            raise OSError(errno.EACCES, 'cgroup.kill')

        self.fs.kill = kill
        with self.assertLogs(cgroup_leaf.log.name, level='ERROR'):
            outcome = run.close()
        self.assertFalse(alive(run.pid))
        self.assertNotIn(outcome.state, ('empty', 'leaf_empty'))
        self.assertEqual(run.identity, held)
        self.assertEqual(cgroup_identity(escaped), held)
        self.assertIsNotNone(run.dirfd)
        self.assertEqual((escaped / 'cgroup.procs').read_text().strip(), str(run.pid))

    def test_abandoned_close_does_not_leak_pidfd(self):
        before = len(os.listdir('/proc/self/fd'))
        pids = []
        for n in range(4):
            run = self.root.launch(f'run-{n}', ['sleep', '60'])
            pids.append(run.pid)
            self.cleanup_pids.append(run.pid)
            (self.root_path / f'run-{n}').rename(self.root_path / f'escaped-{n}.scope')
            with self.assertLogs(cgroup_leaf.log.name, level='ERROR'):
                outcome = run.close()
            self.assertFalse(alive(run.pid))
            self.assertNotIn(outcome.state, ('empty', 'leaf_empty'))
            self.assertIsNone(run.pidfd)
            del run
        gc.collect()
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        for pid in pids:
            self.assertFalse(alive(pid))

    def test_sweep_of_empty_leaf_does_not_spare_launch_child(self):
        # Sweep removes a leaf that reads empty. The launch handle still holds
        # the child's pidfd and must kill that child after the path is gone.
        run = self.root.launch('run-a', ['sleep', '60'])
        self.cleanup_pids.append(run.pid)
        leaf = self.root_path / 'run-a'
        (leaf / 'cgroup.procs').write_text('')
        result = self.root.sweep(keep=())
        self.assertEqual(result.outcomes['run-a'].state, 'empty')
        self.assertFalse(leaf.exists())
        self.assertTrue(alive(run.pid))
        with self.assertLogs(cgroup_leaf.log.name, level='ERROR'):
            outcome = run.close()
        self.assertFalse(alive(run.pid))
        self.assertNotIn(outcome.state, ('empty', 'leaf_empty'))
        self.assertIsNone(run.pidfd)
        self.assertTrue(outcome.main_exit_seen)

    def test_launch_does_not_spawn_into_scope_swapped_after_make_leaf(self):
        scope = self.root_path / 'run-1.scope'
        aside = self.root_path / 'run-a-aside'
        leaf = self.root_path / 'run-a'
        write_cgroup(scope, procs='')
        (scope / 'payload').write_text('systemd')
        swapped = {'done': False}
        real_open = os.open

        def swap_open(path, flags, *args, **kwargs):
            if not swapped['done'] and _is_leaf_directory_open(path, flags, leaf):
                try:
                    marked = os.getxattr(leaf, cgroup_leaf.OWNERSHIP_XATTR,
                                         follow_symlinks=False)
                except OSError:
                    marked = b''
                if marked == cgroup_leaf.OWNERSHIP_VALUE and scope.exists():
                    swapped['done'] = True
                    leaf.rename(aside)
                    scope.rename(leaf)
            return real_open(path, flags, *args, **kwargs)

        caught = None
        try:
            with patch.object(cgroup_leaf.os, 'open', swap_open):
                try:
                    self.root.launch('run-a', ['sleep', '60'])
                except PermissionError as exc:
                    caught = exc
        finally:
            if (leaf / 'cgroup.procs').is_file():
                text = (leaf / 'cgroup.procs').read_text().strip()
                if text.isdigit():
                    _stop_pid(int(text))
        self.assertIsNotNone(caught)
        self.assertTrue(swapped['done'])
        self.assertEqual((leaf / 'payload').read_text(), 'systemd')
        self.assertEqual((leaf / 'cgroup.kill').read_text(), '')
        self.assertEqual((leaf / 'cgroup.procs').read_text().strip(), '')

    def test_spawn_does_not_join_scope_swapped_before_procs_open(self):
        # LeafRun has already pinned the directory. Before cgroup.procs is
        # opened, that directory is renamed aside and a scope takes its name.
        # The child must join the pinned inode, not the scope. close() must
        # not remove the scope.
        gc.collect()
        before = len(os.listdir('/proc/self/fd'))
        scope = self.root_path / 'run-1.scope'
        aside = self.root_path / 'run-a-aside'
        leaf = self.root_path / 'run-a'
        write_cgroup(scope, procs='')
        (scope / 'payload').write_text('systemd')
        swapped = {'done': False}
        real_open = os.open

        def swap_open(path, flags, *args, **kwargs):
            if (not swapped['done'] and not isinstance(path, int)
                    and (flags & os.O_WRONLY) and not (flags & os.O_CREAT)
                    and not (flags & os.O_DIRECTORY)):
                text = os.fsdecode(path)
                if text == 'cgroup.procs' or text.endswith('/cgroup.procs'):
                    swapped['done'] = True
                    if leaf.exists() and scope.exists() and not aside.exists():
                        leaf.rename(aside)
                        scope.rename(leaf)
            return real_open(path, flags, *args, **kwargs)

        run = None
        try:
            with patch.object(cgroup_leaf.os, 'open', swap_open):
                run = self.root.launch('run-a', ['sleep', '30'])
            self.assertTrue(swapped['done'])
            self.assertIsNotNone(run)
            self.assertEqual((leaf / 'payload').read_text(), 'systemd')
            self.assertEqual((leaf / 'cgroup.procs').read_text().strip(), '')
            self.assertEqual((aside / 'cgroup.procs').read_text().strip(), str(run.pid))
            with self.assertLogs(cgroup_leaf.log.name, level='ERROR'):
                outcome = run.close()
            self.assertNotIn(outcome.state, ('empty', 'leaf_empty'))
            self.assertFalse(alive(run.pid))
            self.assertEqual((leaf / 'payload').read_text(), 'systemd')
            self.assertEqual((leaf / 'cgroup.kill').read_text(), '')
            self.assertTrue(leaf.is_dir())
            self.assertTrue(aside.is_dir())
        finally:
            if run is not None:
                if run.pid and alive(run.pid):
                    _stop_pid(run.pid)
                run._release_held_fds()
            gc.collect()
        gc.collect()
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_launch_does_not_accept_recycled_inode(self):
        # make_leaf's directory fd must still be open when launch compares the
        # path. Replacing the directory after that fd is closed reuses the
        # inode number; launch must not start the child there.
        gc.collect()
        before = len(os.listdir('/proc/self/fd'))
        leaf = self.root_path / 'run-a'
        real_make = cgroup_leaf.Cgroup2.make_leaf

        def make(target):
            created = real_make(self.fs, target)
            for name in ('cgroup.procs', 'cgroup.kill', 'cgroup.events'):
                path = Path(target) / name
                if not path.exists():
                    path.touch()
            return created

        self.fs.make_leaf = make
        real_open_identity = cgroup_leaf.LeafRun._open_identity
        state = {'reused': None}

        def recycle(run, expect=None):
            path = Path(run.leaf)
            if path.is_dir():
                original = path.stat().st_ino
                for child in list(path.iterdir()):
                    if child.is_symlink() or child.is_file():
                        child.unlink()
                    else:
                        raise AssertionError(f'unexpected child {child}')
                os.rmdir(path)
                write_cgroup(path, procs='')
                (path / 'recycled').write_text('yes')
                state['reused'] = path.stat().st_ino == original
            return real_open_identity(run, expect)

        run = None
        caught = None
        try:
            with patch.object(cgroup_leaf.LeafRun, '_open_identity', recycle):
                try:
                    run = self.root.launch('run-a', ['sleep', '30'])
                except (PermissionError, LaunchError) as exc:
                    caught = exc
            self.assertIsNotNone(caught, f'reused={state["reused"]} run={run!r}')
            self.assertEqual((leaf / 'cgroup.procs').read_text().strip(), '')
            self.assertEqual((leaf / 'recycled').read_text(), 'yes')
            self.assertTrue(leaf.is_dir())
            caught = None
        finally:
            if run is not None:
                try:
                    run.close()
                except Exception:
                    pass
                if run.pid and alive(run.pid):
                    _stop_pid(run.pid)
                run._release_held_fds()
            gc.collect()
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)


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
        self.killed = []

    def kill(self, leaf, deadline=None, dirfd=None):
        self.killed.append(Path(leaf).name)
        return super().kill(leaf, deadline, dirfd=dirfd)

    def _rmdir(self, directory, identity=None, dirfd=None):
        directory = Path(directory)
        self.rmdir_calls.append(directory)
        return super()._rmdir(directory, identity=identity, dirfd=dirfd)

    def _prepare_rmdir(self, dirfd):
        # A tmpdir is not cgroupfs: interface files block rmdir. Drop the ones
        # on this inode, not whatever path now uses the name.
        procs = None
        try:
            procs = os.open('cgroup.procs', os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                            dir_fd=dirfd)
        except FileNotFoundError:
            pass
        if procs is not None:
            try:
                chunks = []
                while True:
                    chunk = os.read(procs, 4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(procs)
            if b''.join(chunks).strip():
                raise OSError(errno.EBUSY, 'procs not empty')
        for name in os.listdir(dirfd):
            info = os.lstat(name, dir_fd=dirfd)
            if stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError(errno.EBUSY, 'child cgroup remains')
            os.unlink(name, dir_fd=dirfd)


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

        def always_busy(fs, directory, identity=None, dirfd=None):
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

        def flaky(directory, identity=None, dirfd=None):
            if remaining['n']:
                remaining['n'] -= 1
                self.fs.rmdir_calls.append(Path(directory))
                raise OSError(errno.EBUSY, 'dying task')
            real(directory, identity=identity, dirfd=dirfd)

        self.fs._rmdir = flaky
        self.fs.remove(leaf, identity=cgroup_identity(leaf),
                       deadline=time.monotonic() + 1)
        self.assertFalse(leaf.exists())

    def test_read_text_returns_none_when_read_raises(self):
        # ENODEV/EIO while a cgroup is torn down must not escape cleanup.
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        deadline = time.monotonic() + 1
        with patch.object(cgroup_leaf.os, 'read', side_effect=OSError(errno.ENODEV, 'gone')):
            self.assertIsNone(self.fs._read_text(leaf / 'cgroup.procs', deadline))
            self.assertIsNone(self.fs.populated(leaf, deadline=deadline))
            self.assertIs(self.fs._procs_empty(leaf, deadline), False)

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

    def test_sweep_kills_only_a_marked_leaf_id(self):
        # systemd scope, even with our xattr copied onto it: the name is not a leaf id.
        scope = self.root_path / 'run-1234.scope'
        unmarked = self.root_path / 'run-othertool'  # valid leaf id, no marker
        marked = self.root_path / 'run-ours'
        kept = self.root_path / 'run-kept'
        other = self.root_path / 'app.slice'
        for path in (scope, unmarked, marked, kept):
            write_cgroup(path, procs='')
        other.mkdir()
        (other / 'payload').write_text('foreign')
        (scope / 'payload').write_text('systemd')
        (unmarked / 'payload').write_text('other-tool')
        cgroup_leaf.mark_owned(scope)
        cgroup_leaf.mark_owned(marked)
        cgroup_leaf.mark_owned(kept)
        link = self.root_path / 'run-link'
        link.symlink_to(scope, target_is_directory=True)
        root = DelegatedRoot(self.root_path, self.fs)
        with self.assertLogs(cgroup_leaf.log.name, level='WARNING'):
            result = root.sweep(keep={'run-kept'})
        self.assertEqual(set(result.outcomes), {'run-ours'})
        self.assertEqual(result.outcomes['run-ours'].state, 'empty')
        self.assertFalse(marked.exists())
        self.assertEqual(result.reported, (
            ('app.slice', 'foreign-name'),
            ('run-1234.scope', 'foreign-name'),
            ('run-link', 'symlink'),
            ('run-othertool', 'unmarked'),
        ))
        self.assertEqual((scope / 'payload').read_text(), 'systemd')
        self.assertEqual((scope / 'cgroup.kill').read_text(), '')
        self.assertEqual(os.getxattr(scope, cgroup_leaf.OWNERSHIP_XATTR, follow_symlinks=False),
                         cgroup_leaf.OWNERSHIP_VALUE)
        self.assertEqual((unmarked / 'payload').read_text(), 'other-tool')
        self.assertEqual((unmarked / 'cgroup.kill').read_text(), '')
        with self.assertRaises(OSError):
            os.getxattr(unmarked, cgroup_leaf.OWNERSHIP_XATTR, follow_symlinks=False)
        self.assertEqual((other / 'payload').read_text(), 'foreign')
        self.assertTrue(link.is_symlink())
        self.assertTrue(kept.is_dir())
        self.assertEqual(self.fs.killed, ['run-ours'])
        self.assertEqual([path.name for path in self.fs.rmdir_calls], ['run-ours'])

    def _sweep_with_marker_errno(self, name, err):
        leaf = self.root_path / name
        write_cgroup(leaf, procs='')
        (leaf / 'payload').write_text(name)
        cgroup_leaf.mark_owned(leaf)
        real = os.getxattr

        def fail(target, attr, *args, **kwargs):
            if attr == cgroup_leaf.OWNERSHIP_XATTR:
                raise OSError(err, os.strerror(err))
            return real(target, attr, *args, **kwargs)

        root = DelegatedRoot(self.root_path, self.fs)
        with patch.object(cgroup_leaf.os, 'getxattr', fail), \
             self.assertLogs(cgroup_leaf.log.name, level='WARNING'):
            result = root.sweep(keep=())
        self.assertEqual(result.outcomes, {})
        self.assertEqual(self.fs.killed, [])
        self.assertEqual(self.fs.rmdir_calls, [])
        self.assertEqual((leaf / 'payload').read_text(), name)
        self.assertEqual((leaf / 'cgroup.kill').read_text(), '')
        self.assertEqual(os.getxattr(leaf, cgroup_leaf.OWNERSHIP_XATTR, follow_symlinks=False),
                         cgroup_leaf.OWNERSHIP_VALUE)
        return result

    def test_sweep_enodata_on_a_marked_leaf_is_unmarked(self):
        # ENODATA means the attribute is absent. A leaf whose marker was just
        # set, then read back as ENODATA, is unmarked and is not killed.
        result = self._sweep_with_marker_errno('run-nodata', errno.ENODATA)
        self.assertEqual(result.reported, (('run-nodata', 'unmarked'),))

    def test_sweep_eopnotsupp_is_marker_unreadable(self):
        # EOPNOTSUPP is not "unmarked". The leaf may still be ours, so sweep
        # must not kill it and must report the errno.
        err = errno.EOPNOTSUPP
        result = self._sweep_with_marker_errno('run-noxattr', err)
        self.assertEqual(result.reported, (('run-noxattr', f'marker-unreadable:{err}'),))

    def test_adopt_refuses_unreadable_marker_without_removing(self):
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        (leaf / 'payload').write_text('kept')
        cgroup_leaf.mark_owned(leaf)
        real = os.getxattr

        def fail(target, attr, *args, **kwargs):
            if attr == cgroup_leaf.OWNERSHIP_XATTR:
                raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))
            return real(target, attr, *args, **kwargs)

        root = DelegatedRoot(self.root_path, self.fs)
        with patch.object(cgroup_leaf.os, 'getxattr', fail):
            with self.assertRaises(PermissionError) as caught:
                root.adopt('run-a')
        self.assertIn('unreadable', str(caught.exception))
        self.assertIn(str(errno.EOPNOTSUPP), str(caught.exception))
        self.assertEqual((leaf / 'payload').read_text(), 'kept')
        self.assertTrue((leaf / 'cgroup.kill').is_file())

    def test_sweep_leaves_unmarked_non_id_run_names(self):
        # The run- prefix is not ownership. These names are not leaf ids, and
        # none of them carry the marker, so sweep must not kill or remove them.
        names = (
            'run-app.slice',
            'run-1234.service',
            'run-1234.SCOPE',
            'run-1234.scope.bak',
            'run-',
            'run-not.valid',
            'run-1234.scope',
            'run-busy.name',
            'app.slice',
        )
        for name in names:
            path = self.root_path / name
            if name == 'run-busy.name':
                write_cgroup(path, procs='5\n')
            elif name == 'app.slice':
                path.mkdir()
            else:
                write_cgroup(path, procs='')
            (path / 'payload').write_text(name)
        root = DelegatedRoot(self.root_path, self.fs)
        start = time.monotonic()
        with self.assertLogs(cgroup_leaf.log.name, level='WARNING'):
            result = root.sweep(keep=(), kill_timeout=0.6)
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertEqual(result.outcomes, {})
        self.assertEqual(self.fs.killed, [])
        self.assertEqual(self.fs.rmdir_calls, [])
        self.assertEqual(
            [name for name, reason in result.reported],
            sorted(names),
        )
        self.assertTrue(all(reason == 'foreign-name' for _name, reason in result.reported))
        for name in names:
            path = self.root_path / name
            self.assertEqual((path / 'payload').read_text(), name)
            if name != 'app.slice':
                self.assertEqual((path / 'cgroup.kill').read_text(), '')

    def test_sweep_does_not_kill_scope_renamed_onto_classified_leaf(self):
        # Classification saw run-ours. Before the directory fd is opened, that
        # inode is moved aside and run-1.scope (marker copied) takes the name.
        # The opened inode is not the classified one, so it must not be killed.
        leaf = self.root_path / 'run-ours'
        scope = self.root_path / 'run-1.scope'
        aside = self.root_path / 'run-ours-aside'
        write_cgroup(leaf, procs='')
        write_cgroup(scope, procs='')
        (leaf / 'marker').write_text('ours')
        (scope / 'marker').write_text('systemd')
        cgroup_leaf.mark_owned(leaf)
        cgroup_leaf.mark_owned(scope)
        classified = cgroup_identity(leaf)
        swapped = {'done': False}
        real_open = os.open

        def swap_open(path, flags, *args, **kwargs):
            # Full path (older open) or the leaf name relative to the root dirfd.
            if (not swapped['done'] and _is_leaf_directory_open(path, flags, leaf)):
                swapped['done'] = True
                leaf.rename(aside)
                scope.rename(leaf)
            return real_open(path, flags, *args, **kwargs)

        root = DelegatedRoot(self.root_path, self.fs)
        with patch.object(cgroup_leaf.os, 'open', swap_open), \
             self.assertLogs(cgroup_leaf.log.name, level='WARNING'):
            result = root.sweep(keep=(), kill_timeout=0.4)
        self.assertTrue(swapped['done'])
        self.assertNotEqual(cgroup_identity(leaf), classified)
        self.assertTrue((leaf / 'marker').is_file())
        self.assertEqual((leaf / 'marker').read_text(), 'systemd')
        self.assertEqual((leaf / 'cgroup.kill').read_text(), '')
        self.assertEqual((aside / 'marker').read_text(), 'ours')
        self.assertEqual((aside / 'cgroup.kill').read_text(), '')
        self.assertNotIn('run-ours', result.outcomes)
        self.assertNotIn('run-1.scope', result.outcomes)
        self.assertEqual(self.fs.killed, [])
        self.assertEqual(self.fs.rmdir_calls, [])

    def test_kill_does_not_write_a_replaced_cgroup(self):
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        cgroup_leaf.mark_owned(leaf)
        run = LeafRun(self.fs, leaf, 0.2)
        real = run._same_cgroup

        def swap():
            ok = real()
            old = self.root_path / 'run-a-old'
            if ok and not old.exists():
                leaf.rename(old)
                write_cgroup(leaf, procs='')
                (leaf / 'marker').write_text('foreign')
            return ok

        run._same_cgroup = swap
        with self.assertLogs(cgroup_leaf.log.name, level='ERROR'):
            outcome = run.close()
        self.assertEqual(outcome.state, 'unknown')
        self.assertEqual((leaf / 'marker').read_text(), 'foreign')
        self.assertEqual((leaf / 'cgroup.kill').read_text(), '')
        self.assertEqual((self.root_path / 'run-a-old' / 'cgroup.kill').read_text(), '1')
        self.assertTrue(leaf.is_dir())

    def test_replaced_child_cgroup_is_not_removed(self):
        leaf = self.root_path / 'run-a'
        child = leaf / 'sub'
        old = leaf / 'sub-old'
        write_cgroup(leaf, procs='')
        write_cgroup(child, procs='')
        (child / 'marker').write_text('original')
        real = Cgroup2._child_cgroups

        def swap(fs, directory, deadline):
            found = real(fs, directory, deadline)
            if directory == leaf and found and not old.exists():
                path = found[0][0] if isinstance(found[0], tuple) else found[0]
                path.rename(old)
                write_cgroup(path, procs='')
                (path / 'marker').write_text('foreign')
            return found

        self.fs._child_cgroups = swap.__get__(self.fs, TmpCgroup)
        raised = None
        try:
            self.fs.remove(leaf, identity=cgroup_identity(leaf), deadline=time.monotonic() + 1)
        except OSError as exc:
            raised = exc
        self.assertTrue((old / 'marker').is_file())
        self.assertEqual((old / 'marker').read_text(), 'original')
        self.assertIsInstance(raised, OSError)
        self.assertEqual(raised.errno, errno.EBUSY)
        self.assertEqual((child / 'marker').read_text(), 'foreign')
        self.assertTrue(leaf.is_dir())
        self.assertEqual(self.fs.rmdir_calls, [])

    def test_rmdir_swap_after_identity_check_is_not_booked_removed(self):
        # The empty and identity checks have passed. A swap inside rmdir must
        # not delete the replacement and must not book the original as removed.
        leaf = self.root_path / 'run-a'
        aside = self.root_path / 'run-a-old'
        write_cgroup(leaf, procs='')
        (leaf / 'marker').write_text('original')
        run = LeafRun(self.fs, leaf, 0.2)
        real = TmpCgroup._rmdir

        def swap(fs, directory, *args, **kwargs):
            if Path(directory) == leaf and not aside.exists():
                leaf.rename(aside)
                write_cgroup(leaf, procs='')
                (leaf / 'marker').write_text('foreign')
            return real(fs, directory, *args, **kwargs)

        self.fs._rmdir = swap.__get__(self.fs, TmpCgroup)
        outcome = run.close()
        self.assertTrue((aside / 'marker').is_file())
        self.assertEqual((aside / 'marker').read_text(), 'original')
        self.assertTrue((leaf / 'marker').is_file())
        self.assertEqual((leaf / 'marker').read_text(), 'foreign')
        self.assertFalse(run.removed)
        self.assertEqual(outcome.state, 'unknown')
        self.assertTrue(outcome.errors)

    def test_rmdir_books_removed_when_fstat_nlink_stays_linked(self):
        # Linux 6.12 cgroupfs: kernfs_refresh_inode sets a directory's nlink to
        # subdirs+2 on every getattr, and __kernfs_remove does not clear_nlink.
        # fstat of a held fd therefore stays >= 2 after a real rmdir. tmpfs
        # drops nlink to 0, which is why a suite on tmpfs stays green. This
        # replaces that 0 with 2 after the inode is really unlinked.
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        run = LeafRun(self.fs, leaf, 0.2)
        run.adopted = True
        real_fstat = os.fstat

        def kernfs_fstat(fd):
            info = real_fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_nlink == 0:
                fields = list(info)
                fields[3] = 2
                return os.stat_result(fields)
            return info

        with patch.object(cgroup_leaf.os, 'fstat', kernfs_fstat):
            with run:
                pass
        self.assertTrue(run.removed)
        self.assertEqual(run.outcome.state, 'empty')
        self.assertEqual(run.outcome.errors, ())
        self.assertFalse(leaf.exists())

    def test_second_close_after_swap_inside_rmdir_keeps_held_inode(self):
        # During os.rmdir the held leaf is renamed to escaped.scope and an
        # empty directory is put at run-a. rmdir deletes that empty directory.
        # The first close must not book the original removed. A second close
        # must not book it either: the path is missing, but the held inode
        # is still escaped.scope. The fd stays open, and sweep leaves the
        # foreign name in place.
        leaf = self.root_path / 'run-a'
        escaped = self.root_path / 'escaped.scope'
        write_cgroup(leaf, procs='')
        cgroup_leaf.mark_owned(leaf)
        run = LeafRun(self.fs, leaf, 0.2)
        held = run.identity
        real_rmdir = os.rmdir
        swapped = {'done': False}

        def swap_rmdir(path, *, dir_fd=None):
            if (not swapped['done'] and dir_fd is not None
                    and os.fsdecode(path) == leaf.name):
                swapped['done'] = True
                os.rename(leaf.name, escaped.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                os.mkdir(leaf.name, dir_fd=dir_fd)
            return real_rmdir(path, dir_fd=dir_fd)

        with patch.object(cgroup_leaf.os, 'rmdir', swap_rmdir), \
             self.assertLogs(cgroup_leaf.log.name, level='ERROR'):
            first = run.close()
        self.assertTrue(swapped['done'])
        self.assertFalse(leaf.exists())
        self.assertFalse(run.removed)
        self.assertEqual(first.state, 'unknown')
        self.assertTrue(first.errors)
        self.assertIsNotNone(run.dirfd)
        self.assertEqual(cgroup_identity(escaped), held)
        self.assertEqual(os.fstat(run.dirfd).st_ino, held[1])
        self.assertEqual(os.getxattr(escaped, cgroup_leaf.OWNERSHIP_XATTR, follow_symlinks=False),
                         cgroup_leaf.OWNERSHIP_VALUE)
        held_fd = run.dirfd
        second = run.close()
        self.assertFalse(run.removed)
        self.assertEqual(second.state, 'unknown')
        self.assertTrue(second.errors)
        self.assertIs(run.dirfd, held_fd)
        self.assertEqual(os.fstat(run.dirfd).st_ino, held[1])
        self.assertEqual(cgroup_identity(escaped), held)
        root = DelegatedRoot(self.root_path, self.fs)
        with self.assertLogs(cgroup_leaf.log.name, level='WARNING'):
            result = root.sweep(keep=())
        self.assertEqual(result.outcomes, {})
        self.assertIn((escaped.name, 'foreign-name'), result.reported)
        self.assertEqual(cgroup_identity(escaped), held)
        self.assertEqual(self.fs.killed, ['run-a'])
        self.assertNotIn(escaped.name, [path.name for path in self.fs.rmdir_calls])

    def test_rmdir_after_empty_check_does_not_remove_replacement(self):
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        identity = cgroup_identity(leaf)
        real = Cgroup2._confirmed_empty

        def swap(fs, directory, deadline):
            ok = real(fs, directory, deadline)
            old = self.root_path / 'run-a-old'
            if ok and directory == leaf and not old.exists():
                leaf.rename(old)
                write_cgroup(leaf, procs='')
                (leaf / 'marker').write_text('foreign')
            return ok

        self.fs._confirmed_empty = swap.__get__(self.fs, TmpCgroup)
        with self.assertRaises(OSError) as caught:
            self.fs.remove(leaf, identity=identity, deadline=time.monotonic() + 1)
        self.assertEqual(caught.exception.errno, errno.EBUSY)
        self.assertEqual((leaf / 'marker').read_text(), 'foreign')
        self.assertTrue((self.root_path / 'run-a-old').is_dir())
        self.assertEqual(self.fs.rmdir_calls, [])

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

    def test_adopt_does_not_kill_scope_swapped_after_lstat(self):
        # lstat saw run-a. Before the directory fd is opened, that inode is
        # moved aside and a marked scope takes the name. adopt must not kill,
        # rmdir, or book that scope empty.
        leaf = self.root_path / 'run-a'
        scope = self.root_path / 'run-1.scope'
        aside = self.root_path / 'run-a-aside'
        write_cgroup(leaf, procs='')
        write_cgroup(scope, procs='')
        (leaf / 'payload').write_text('ours')
        (scope / 'payload').write_text('systemd')
        cgroup_leaf.mark_owned(leaf)
        cgroup_leaf.mark_owned(scope)
        classified = cgroup_identity(leaf)
        swapped = {'done': False}
        real_open = os.open

        def swap_open(path, flags, *args, **kwargs):
            if not swapped['done'] and _is_leaf_directory_open(path, flags, leaf):
                swapped['done'] = True
                leaf.rename(aside)
                scope.rename(leaf)
            return real_open(path, flags, *args, **kwargs)

        root = DelegatedRoot(self.root_path, self.fs)
        run = None
        with patch.object(cgroup_leaf.os, 'open', swap_open):
            try:
                run = root.adopt('run-a')
            except PermissionError:
                run = None
        self.assertTrue(swapped['done'])
        if run is not None:
            outcome = run.close()
            self.fail(f'adopted swapped scope and booked {outcome.state}')
        self.assertNotEqual(cgroup_identity(leaf), classified)
        self.assertEqual((leaf / 'payload').read_text(), 'systemd')
        self.assertEqual((leaf / 'cgroup.kill').read_text(), '')
        self.assertEqual((aside / 'payload').read_text(), 'ours')
        self.assertEqual(self.fs.killed, [])
        self.assertEqual(self.fs.rmdir_calls, [])

    def test_make_leaf_does_not_mark_swapped_scope(self):
        leaf = self.root_path / 'run-a'
        scope = self.root_path / 'run-1.scope'
        aside = self.root_path / 'run-a-aside'
        scope.mkdir()
        (scope / 'payload').write_text('systemd')
        real = os.setxattr
        swapped = {'done': False}

        def swap_setxattr(path, attr, value, *args, **kwargs):
            if attr == cgroup_leaf.OWNERSHIP_XATTR and not swapped['done']:
                swapped['done'] = True
                if leaf.exists() and not aside.exists():
                    leaf.rename(aside)
                    scope.rename(leaf)
            return real(path, attr, value, *args, **kwargs)

        with patch.object(cgroup_leaf.os, 'setxattr', swap_setxattr):
            got = self.fs.make_leaf(leaf)
        try:
            self.assertTrue(swapped['done'])
            self.assertEqual(got, cgroup_identity(aside))
            self.assertEqual(os.getxattr(aside, cgroup_leaf.OWNERSHIP_XATTR, follow_symlinks=False),
                             cgroup_leaf.OWNERSHIP_VALUE)
            with self.assertRaises(OSError) as caught:
                os.getxattr(leaf, cgroup_leaf.OWNERSHIP_XATTR, follow_symlinks=False)
            self.assertEqual(caught.exception.errno, errno.ENODATA)
            self.assertEqual((leaf / 'payload').read_text(), 'systemd')
        finally:
            close = getattr(got, 'close', None)
            if close is not None:
                close()

    def test_make_leaf_enospc_discards_only_created_inode(self):
        leaf = self.root_path / 'run-a'
        scope = self.root_path / 'run-1.scope'
        aside = self.root_path / 'run-a-aside'
        scope.mkdir()
        (scope / 'payload').write_text('systemd')
        (scope / 'cgroup.kill').write_text('')
        real = os.setxattr
        swapped = {'done': False}

        def swap_and_enospc(path, attr, value, *args, **kwargs):
            if attr == cgroup_leaf.OWNERSHIP_XATTR and not swapped['done']:
                swapped['done'] = True
                if leaf.exists() and not aside.exists():
                    leaf.rename(aside)
                    scope.rename(leaf)
                raise OSError(errno.ENOSPC, 'no space')
            return real(path, attr, value, *args, **kwargs)

        with patch.object(cgroup_leaf.os, 'setxattr', swap_and_enospc):
            with self.assertRaises(OSError) as caught:
                self.fs.make_leaf(leaf)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertTrue(swapped['done'])
        self.assertTrue((leaf / 'payload').is_file())
        self.assertEqual((leaf / 'payload').read_text(), 'systemd')
        self.assertEqual((leaf / 'cgroup.kill').read_text(), '')
        self.assertFalse(aside.exists())

    def test_make_leaf_does_not_mark_recycled_inode(self):
        # The first lstat of the new directory replaces it. ext4 reuses the
        # inode number once nothing holds the old directory open. make_leaf
        # must not mark that replacement.
        leaf = self.root_path / 'run-a'
        state = {'done': False, 'reused': False}
        real = os.lstat

        def recycle_lstat(path, *args, **kwargs):
            if state['done'] or isinstance(path, int):
                return real(path, *args, **kwargs)
            text = os.fsdecode(path)
            if text != leaf.name and Path(text) != leaf:
                return real(path, *args, **kwargs)
            state['done'] = True
            info = real(path, *args, **kwargs)
            original = info.st_ino
            for _ in range(8):
                os.rmdir(leaf)
                os.mkdir(leaf, 0o755)
                info = real(path, *args, **kwargs)
                if info.st_ino == original:
                    state['reused'] = True
                    break
            return info

        gc.collect()
        before = len(os.listdir('/proc/self/fd'))
        got = None
        try:
            with patch.object(cgroup_leaf.os, 'lstat', recycle_lstat):
                try:
                    got = self.fs.make_leaf(leaf)
                except OSError as exc:
                    self.assertEqual(exc.errno, errno.EBUSY)
                else:
                    self.fail(f'make_leaf marked a recycled directory {got!r} '
                              f'reused={state["reused"]}')
        finally:
            close = getattr(got, 'close', None)
            if close is not None:
                close()
            gc.collect()
        self.assertTrue(state['done'])
        self.assertTrue(leaf.is_dir())
        with self.assertRaises(OSError) as caught:
            os.getxattr(leaf, cgroup_leaf.OWNERSHIP_XATTR, follow_symlinks=False)
        self.assertEqual(caught.exception.errno, errno.ENODATA)
        caught = None
        gc.collect()
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_discard_does_not_remove_sibling_that_reused_inode(self):
        # Opening the new leaf fails after its inode number has been reused by
        # run-b. Discard must not rmdir run-b.
        leaf = self.root_path / 'run-a'
        sibling = self.root_path / 'run-b'
        state = {'done': False, 'reused': False}
        real_open = os.open

        def fail_open(path, flags, *args, **kwargs):
            if (not state['done'] and _is_leaf_directory_open(path, flags, leaf)
                    and leaf.exists()):
                state['done'] = True
                original = os.lstat(leaf).st_ino
                os.rmdir(leaf)
                os.mkdir(sibling)
                (sibling / 'payload').write_text('keep')
                state['reused'] = os.lstat(sibling).st_ino == original
                raise OSError(errno.ENOENT, 'leaf name swapped away')
            return real_open(path, flags, *args, **kwargs)

        gc.collect()
        before = len(os.listdir('/proc/self/fd'))
        with patch.object(cgroup_leaf.os, 'open', fail_open):
            with self.assertRaises(OSError):
                self.fs.make_leaf(leaf)
        gc.collect()
        self.assertTrue(state['done'])
        self.assertTrue(state['reused'])
        self.assertFalse(leaf.exists())
        self.assertTrue(sibling.is_dir())
        self.assertEqual((sibling / 'payload').read_text(), 'keep')
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_adopt_does_not_accept_recycled_inode(self):
        gc.collect()
        before = len(os.listdir('/proc/self/fd'))
        leaf = self.root_path / 'run-a'
        write_cgroup(leaf, procs='')
        cgroup_leaf.mark_owned(leaf)
        real_open_identity = cgroup_leaf.LeafRun._open_identity
        state = {'reused': None}

        def recycle(run, expect=None):
            path = Path(run.leaf)
            original = path.stat().st_ino
            for child in list(path.iterdir()):
                if child.is_symlink() or child.is_file():
                    child.unlink()
                else:
                    raise AssertionError(f'unexpected child {child}')
            os.rmdir(path)
            write_cgroup(path, procs='')
            cgroup_leaf.mark_owned(path)
            (path / 'recycled').write_text('yes')
            state['reused'] = path.stat().st_ino == original
            return real_open_identity(run, expect)

        root = DelegatedRoot(self.root_path, self.fs)
        run = None
        caught = None
        try:
            with patch.object(cgroup_leaf.LeafRun, '_open_identity', recycle):
                try:
                    run = root.adopt('run-a')
                except PermissionError as exc:
                    caught = exc
            self.assertIsNotNone(caught, f'reused={state["reused"]} run={run!r}')
            self.assertEqual((leaf / 'recycled').read_text(), 'yes')
            self.assertEqual((leaf / 'cgroup.kill').read_text(), '')
            self.assertEqual(self.fs.killed, [])
            self.assertEqual(self.fs.rmdir_calls, [])
            caught = None
        finally:
            if run is not None:
                run._release_held_fds()
            gc.collect()
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)


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
