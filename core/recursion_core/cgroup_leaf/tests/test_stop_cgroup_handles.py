"""CSL-238 lane STOP: cgroup_leaf handles (triage entries E02, E25, E26, E27, E28, and the late EOF of E40).

No live cgroup or systemd operation: a small fake filesystem keeps one real child in its own process group.
"""
import os
from pathlib import Path
import signal
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import recursion_core.cgroup_leaf as cgroup_leaf
from recursion_core.cgroup_leaf import Cgroup2, DelegatedRoot, LaunchError, LeafRun
from recursion_core.cgroup_leaf.b6_evidence import ATTACH_CALL, SCOPE_ARGV, _pid_key, evaluate

NOT_LINUX = os.name == 'nt' or not hasattr(os, 'pidfd_open')
LINUX_ONLY = 'Linux-only: the cgroup v2 leaf launcher needs pidfd, pipe2 and uid ownership'


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
            if not text:
                return False
            stat = Path(f'/proc/{int(text)}/stat').read_text()
            return stat.rsplit(')', 1)[1].split()[0] != 'Z'
        except FileNotFoundError:
            return False if leaf.exists() else None

    def member(self, pid):
        for path in self.mount.rglob('cgroup.procs'):
            if path.read_text().strip() == str(pid):
                return path.parent
        return None

    def remove(self, leaf):
        for name in ('cgroup.procs', 'cgroup.kill', 'cgroup.events'):
            (leaf / name).unlink(missing_ok=True)
        leaf.rmdir()


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class LeafCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='stop-cg-')
        self.base = Path(self.temp.name).resolve()
        self.mount = self.base / 'cgroup'
        self.root_path = self.mount / 'delegated'
        self.root_path.mkdir(parents=True)
        for name in ('cgroup.procs', 'cgroup.kill'):
            (self.root_path / name).touch()
        self.fs = ProcessGroupFS(self.mount)
        self.root = DelegatedRoot(self.root_path, self.fs)
        self.runs = []

    def spawn(self, name, **kwargs):
        run = self.root.launch(name, ['/bin/sleep', '30'], **kwargs)
        self.runs.append(run)
        return run

    def reap_owned(self, run):
        if run.proc is not None:
            if run.proc.poll() is None:
                run.proc.kill()
            run.proc.wait(timeout=5)
        for attr in ('pidfd', '_pin'):
            fd = getattr(run, attr, None)
            if fd is not None:
                setattr(run, attr, None)
                os.close(fd)

    def tearDown(self):
        for run in self.runs:
            self.reap_owned(run)
        self.temp.cleanup()


@unittest.skipIf(NOT_LINUX, LINUX_ONLY)
class AdoptedHandleTest(LeafCase):
    # E02: an adopted handle holds a name, and a name can be given to a new run
    def test_a_stale_adopted_handle_cannot_kill_a_new_run_with_the_same_name(self):
        failures = []
        for i in range(12):
            name = f'run-reuse{i}'
            old = self.spawn(name, kill_timeout=0.2)
            stale = self.root.adopt(name, kill_timeout=0.2)
            self.runs.append(stale)
            ready = threading.Event()

            def second_caller(ready=ready, stale=stale):
                if not ready.wait(5):
                    failures.append('handoff timed out')
                    return
                try:
                    stale.close()
                except BaseException as exc:
                    failures.append(type(exc).__name__)

            thread = threading.Thread(target=second_caller, daemon=True)
            thread.start()
            old.close()
            replacement = self.spawn(name, kill_timeout=0.2)
            ready.set()
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'the second caller did not finish')
            self.assertIsNone(replacement.proc.poll(), 'the old handle killed the replacement run')
            self.assertEqual(stale.outcome.state, 'empty')
            self.assertEqual(stale.outcome.errors, ())
            self.reap_owned(replacement)
        self.assertEqual(failures, [])

    def test_an_adopted_handle_still_kills_the_leaf_it_adopted(self):
        run = self.spawn('run-live', kill_timeout=2.0)
        adopted = self.root.adopt('run-live', kill_timeout=2.0)
        self.runs.append(adopted)
        outcome = adopted.close()
        self.assertEqual((outcome.state, outcome.errors), ('empty', ()))
        self.assertEqual(run.proc.wait(timeout=5), -signal.SIGKILL)
        self.assertFalse((self.root_path / 'run-live').exists())
        self.assertIsNone(adopted._pin, 'the adopted handle keeps no descriptor once its leaf is removed')

    def test_an_interrupted_removal_of_an_adopted_leaf_still_releases_its_directory(self):
        self.spawn('run-rmdir', kill_timeout=2.0)
        adopted = self.root.adopt('run-rmdir', kill_timeout=2.0)
        self.runs.append(adopted)
        real_remove = self.fs.remove

        def remove_then_interrupt(leaf):
            real_remove(leaf)
            raise KeyboardInterrupt('after the rmdir')

        with patch.object(self.fs, 'remove', side_effect=remove_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                adopted.close()
        self.assertEqual(adopted.close().state, 'empty')
        self.assertIsNone(adopted._pin)

    def test_an_adopted_handle_whose_leaf_is_gone_books_empty_and_holds_nothing(self):
        old = self.spawn('run-gone', kill_timeout=0.2)
        adopted = self.root.adopt('run-gone', kill_timeout=0.2)
        self.runs.append(adopted)
        old.close()
        self.assertEqual(adopted.close().state, 'empty')
        self.assertIsNone(adopted._pin)
        self.assertEqual(adopted.close().state, 'empty')  # the answer is cached

    # E25: a kill deadline that is not a finite number of seconds
    def test_a_kill_deadline_that_is_not_a_finite_number_is_refused_before_a_leaf_exists(self):
        for i, bad in enumerate(('0.1', None, float('nan'), float('inf'), True, -1.0, 10 ** 400)):
            with self.subTest(deadline=repr(bad)[:20]):
                with self.assertRaises(LaunchError):
                    self.spawn(f'run-bad{i}', kill_timeout=bad)
                self.assertFalse((self.root_path / f'run-bad{i}').exists())

    def test_a_kill_deadline_that_is_not_a_finite_number_is_refused_by_adopt_and_by_the_handle(self):
        self.spawn('run-held', kill_timeout=0.2)
        for bad in ('0.1', float('nan')):
            with self.subTest(deadline=repr(bad)):
                with self.assertRaises(LaunchError):
                    self.root.adopt('run-held', kill_timeout=bad)
                with self.assertRaises(LaunchError):
                    LeafRun(self.fs, self.root_path / 'run-held', bad)

    def test_ordinary_kill_deadlines_are_still_accepted(self):
        for i, good in enumerate((1, 0.5, 2.5)):
            run = self.spawn(f'run-ok{i}', kill_timeout=good)
            self.assertEqual(run.kill_timeout, float(good))
            self.assertEqual(run.close().state, 'leaf_empty')
        self.assertEqual(self.spawn('run-zero', kill_timeout=0).kill_timeout, 0.0)  # no wait for the exit

    # E28: an interrupt right after the status pipe's write end is closed
    def test_an_interrupt_after_the_pipe_close_keeps_the_original_interrupt(self):
        self._interrupt_after_pipe_close(reuse=False)

    def test_an_interrupt_after_the_pipe_close_leaves_a_reused_descriptor_alone(self):
        self._interrupt_after_pipe_close(reuse=True)

    def _interrupt_after_pipe_close(self, reuse):
        leaf = self.root_path / 'run-pipe'
        self.fs.make_leaf(leaf)
        run = LeafRun(self.fs, leaf, 0.2)
        self.runs.append(run)
        real_pipe2, real_close = os.pipe2, os.close
        pipe_fds = []
        canary = []
        fired = False

        def pipe2(flags):
            pair = real_pipe2(flags)
            pipe_fds[:] = pair
            return pair

        def close(fd):
            nonlocal fired
            real_close(fd)
            if len(pipe_fds) == 2 and fd == pipe_fds[1] and not fired:
                fired = True
                if reuse:  # another thread takes the number that was just freed
                    canary.append(os.open(self.base / 'owned-canary', os.O_CREAT | os.O_RDWR, 0o600))
                    assert canary[0] == fd
                raise KeyboardInterrupt('injected after the status pipe was closed')

        try:
            with patch.object(os, 'pipe2', side_effect=pipe2), patch.object(os, 'close', side_effect=close):
                with self.assertRaises(KeyboardInterrupt):
                    run._spawn(['/bin/sleep', '30'], None, None, None, None, None, 1)
            if canary:
                os.fstat(canary[0])  # still the canary owner's
        finally:
            for fd in canary:
                try:
                    real_close(fd)
                except OSError:
                    pass


@unittest.skipIf(NOT_LINUX, LINUX_ONLY)
class StartupStatusTest(unittest.TestCase):
    # E26: select() cannot take a descriptor of 1024 or more
    def test_a_high_descriptor_number_is_accepted(self):
        import fcntl
        read_end, write_end = os.pipe()
        high_fd = fcntl.fcntl(read_end, fcntl.F_DUPFD_CLOEXEC, 1024)
        os.close(read_end)
        os.close(write_end)
        try:
            self.assertEqual(LeafRun._read_status(high_fd, 0.2), '')
        finally:
            os.close(high_fd)

    def test_a_high_descriptor_number_reports_what_the_trampoline_wrote(self):
        import fcntl
        read_end, write_end = os.pipe()
        high_fd = fcntl.fcntl(read_end, fcntl.F_DUPFD_CLOEXEC, 1024)
        os.close(read_end)
        os.write(write_end, b'OSError(2)')
        os.close(write_end)
        try:
            self.assertEqual(LeafRun._read_status(high_fd, 0.2), 'OSError(2)')
        finally:
            os.close(high_fd)

    def test_a_high_descriptor_number_is_readable_while_its_writer_is_still_open(self):
        import fcntl
        read_end, write_end = os.pipe()
        high_fd = fcntl.fcntl(read_end, fcntl.F_DUPFD_CLOEXEC, 1024)
        os.close(read_end)
        try:
            self.assertFalse(cgroup_leaf._readable(high_fd, 0.05))
            os.write(write_end, b'x')
            self.assertTrue(cgroup_leaf._readable(high_fd, 0.5))
        finally:
            os.close(write_end)
            os.close(high_fd)

    def test_an_ordinary_descriptor_number_is_still_accepted(self):
        read_end, write_end = os.pipe()
        os.close(write_end)
        try:
            self.assertEqual(LeafRun._read_status(read_end, 0.2), '')
        finally:
            os.close(read_end)

    def test_a_pipe_that_stays_silent_times_out(self):
        read_end, write_end = os.pipe()
        try:
            with self.assertRaises(LaunchError):
                LeafRun._read_status(read_end, 0.05)
        finally:
            os.close(read_end)
            os.close(write_end)

    # E40: an EOF that arrives after the deadline is late, not a confirmation
    def test_an_eof_after_the_deadline_does_not_confirm_the_launch(self):
        now = Clock()

        def select(*args):
            now.value = 2.0  # the wait used up the budget
            return ([99], [], [])

        with patch.object(cgroup_leaf, 'time', SimpleNamespace(monotonic=now)), \
                patch.object(cgroup_leaf.select, 'select', side_effect=select), \
                patch.object(cgroup_leaf.os, 'read', return_value=b''):
            with self.assertRaises(LaunchError):
                LeafRun._read_status(99, 1.0)

    def test_an_eof_in_time_still_confirms_the_launch(self):
        now = Clock()
        with patch.object(cgroup_leaf, 'time', SimpleNamespace(monotonic=now)), \
                patch.object(cgroup_leaf.select, 'select', return_value=([99], [], [])), \
                patch.object(cgroup_leaf.os, 'read', return_value=b''):
            self.assertEqual(LeafRun._read_status(99, 1.0), '')


LEAF = '/operator.slice/run-probe'


def host_record():
    """A generated record that passes; structural only, never evidence of host behaviour."""
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
                elif method == 'direct_write':
                    item.update(target='/outside/cgroup.procs', written_pid=pid, refused=True)
                else:
                    item.update(bus='system', call=ATTACH_CALL, unit='outside.service',
                                subcgroup='', pids=[pid], refused=True)
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


class EvidenceTest(unittest.TestCase):
    # E27: a pid key of thousands of digits is a verdict, not a ValueError
    def test_the_generated_record_passes(self):
        self.assertEqual(evaluate(host_record()), {'verdict': 'pass', 'reasons': []})

    def test_a_pid_key_of_thousands_of_digits_fails_without_an_exception(self):
        record = host_record()
        record['manager_absent']['attempts'][0]['run_pids'] = {'9' * 4301: LEAF}
        self.assertEqual(evaluate(record)['verdict'], 'fail')

    def test_pid_keys(self):
        self.assertEqual(_pid_key('4242'), 4242)
        self.assertEqual(_pid_key(4242), 4242)
        for bad in ('0', '042', '-1', '1' * 4301, '١٢٣', True, 0, -5, None, 1.5):
            self.assertIsNone(_pid_key(bad), repr(bad)[:20])


if __name__ == '__main__':
    unittest.main()
