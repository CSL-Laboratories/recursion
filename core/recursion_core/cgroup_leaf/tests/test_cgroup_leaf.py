"""CSL-728 same-uid leaf tests. Fake cgroupfs with real processes; not runtime certification.

The fake models a leaf as the process group of its main process (started with
start_new_session). It exercises the real trampoline, pidfd and evidence logic; it does
not prove kernel cgroup behaviour. Set RECURSION_CGROUP_ROOT to a writable delegated
cgroup v2 directory containing this process to run the live cell.
"""
import inspect
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import recursion_core.cgroup_leaf as cgroup_leaf
from recursion_core.cgroup_leaf import (Cgroup2, CleanupIncomplete, DelegatedRoot,
                                        LaunchError)

NOT_LINUX = os.name == 'nt' or not hasattr(os, 'pidfd_open')
LINUX_ONLY = ('Linux-only: the cgroup v2 leaf launcher needs pidfd, pipe2 and uid ownership; it refuses '
              'platform_unsupported on Windows; covered by WindowsRefusalTests')


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

    def kill(self, leaf):
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

    def populated(self, leaf):
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

    def remove(self, leaf):
        for name in ('cgroup.procs', 'cgroup.kill', 'cgroup.events'):
            (leaf / name).unlink(missing_ok=True)
        os.rmdir(leaf)
        if self.interrupt_after_remove:
            self.interrupt_after_remove = False
            raise KeyboardInterrupt('interrupt right after rmdir')


@unittest.skipIf(NOT_LINUX, LINUX_ONLY)
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
                self.fs.populated = lambda leaf: 1 / 0
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

    def test_adopted_leaf_works_as_context_manager_and_bad_leaf_is_not_walked(self):
        orphan = self.orphan_leaf('run-adopted')
        with self.root.adopt('run-adopted') as adopted:
            pass
        orphan.wait(5)
        self.assertEqual(adopted.outcome.state, 'empty')
        self.assertEqual(self.leaves(), [])

        outside = self.mount.parent / 'outside-victim'
        outside.mkdir()
        (outside / 'cgroup.procs').touch()
        touched = []

        class SymlinkSwapFs(FakeCgroup):
            def make_leaf(self, leaf):
                os.symlink(outside, leaf)

            def populated(self, leaf):
                touched.append(os.path.realpath(leaf))
                return super().populated(leaf)

        swap_root = DelegatedRoot(self.root_path, SymlinkSwapFs(self.mount))
        with self.assertRaises(PermissionError):
            swap_root.launch('run-swapped', ['true'])
        self.assertEqual(touched, [])

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


@unittest.skipIf(NOT_LINUX, LINUX_ONLY)
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
