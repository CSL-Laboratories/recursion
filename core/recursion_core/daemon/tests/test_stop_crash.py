"""B8/B14 kill -9 cells with real processes (CSL-738). Not systemd or live-host certification.

Each cell launches a real daemon process (crash_child.py) that starts a real run in
its own session, then kills the daemon with SIGKILL at a chosen point:

- B8 ``running``: run started, no Stop. After kill -9 the run is still alive and a new
  daemon re-adopts it as ``running``.
- B14 ``before_signal``: Stop intent committed, daemon killed before signalling the run.
- B14 ``after_signal``: Stop intent committed and the run signalled, daemon killed
  before it observed the run gone (no confirmation).

For both B14 phases the new daemon must never report the run ``running``: it replays
the recorded Stop and reaches ``terminated`` only when the run is positively gone.
The service manager is the test-only ProcessGroupManager, because systemd is not PID 1
on the box that measured these cells; systemd scopes, reboot and Windows are unmeasured.
"""
from pathlib import Path
import os
import signal
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest

from recursion_core.daemon import Daemon, Registry, UserLock
from recursion_core.daemon.tests.procmanager import ProcessGroupManager, _proc_stat

CHILD = Path(__file__).with_name('crash_child.py')
CELLS = int(os.environ.get('RECURSION_CRASH_CELLS', '10'))
PHASES = ('running', 'before_signal', 'after_signal')


def _python():
    argv = [sys.executable, '-B']
    if sysconfig.get_config_var('Py_GIL_DISABLED'):
        argv += ['-X', 'gil=0']
    return argv


def _row(root, run_id):
    db = sqlite3.connect(f'file:{root / "state" / "runs.sqlite3"}?mode=ro', uri=True)
    try:
        return db.execute('SELECT stop, state FROM runs WHERE run_id=?', (run_id,)).fetchone()
    finally:
        db.close()


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _NoSignal(ProcessGroupManager):
    """Replayed Stop that has not reached the run yet (signal delivery delayed)."""
    def stop(self, unit):
        if self._record(unit) is None:
            raise OSError(f'no record for {unit}')


def run_cell(root: Path, index: int, phase: str):
    """Run one kill -9 cell. Returns a dict of observations; raises AssertionError on failure."""
    run_id = f'{phase.replace("_", "-")}-{index}'
    unit = f'recursion-run-{run_id}.service'
    child = subprocess.Popen([*_python(), str(CHILD), str(root), run_id, phase],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE)
    observed, held_alive = [], None
    manager = ProcessGroupManager(root / 'units')
    try:
        marker = root / 'marker'
        if not _wait(marker.exists):
            raise AssertionError(f'{run_id}: daemon never reached {phase}: '
                                 f'{child.stderr.read1(4096) if child.poll() is not None else b""!r}')
        if marker.read_text() != phase:
            raise AssertionError(f'{run_id}: daemon passed {phase} without blocking')
        pid = manager.pid(unit)
        stop, state = _row(root, run_id)
        before = {'stop': stop, 'state': state}
        os.kill(child.pid, signal.SIGKILL)
        child.wait(10)
        assert child.returncode == -signal.SIGKILL, child.returncode
        survived = manager.alive(unit)
        # The kernel released the daemon's flock on kill -9: a new daemon may start.
        with UserLock(root / 'runtime'):
            registry = Registry(root / 'state')
            daemon = Daemon(registry, manager)
            try:
                if phase == 'before_signal':
                    # First restart sweep with a Stop that does not take effect yet: the
                    # run is alive, the Stop is recorded, so it must read stopping.
                    held = Daemon(registry, _NoSignal(root / 'units'))
                    run = next(r for r in held.sweep() if r.run_id == run_id)
                    observed.append(run.state)
                    held_alive = manager.alive(unit)
                for _ in range(200):
                    run = next(r for r in daemon.sweep() if r.run_id == run_id)
                    observed.append(run.state)
                    if phase == 'running' or run.state == 'terminated':
                        break
                    time.sleep(0.01)
                readopted = observed[0]
                if phase == 'running':
                    alive_after = manager.alive(unit) and manager.pid(unit) == pid
                    final = daemon.stop(run_id).state
                else:
                    alive_after = manager.alive(unit)
                    final = registry.get(run_id).state
                stop_after = registry.get(run_id).stop_requested
            finally:
                daemon.close()
        return {'run_id': run_id, 'phase': phase, 'before': before, 'survived': survived,
                'held_alive': phase != 'before_signal' or held_alive,
                'observed': observed, 'readopted': readopted, 'alive_after': alive_after,
                'final': final, 'stop_after': stop_after, 'pid': pid}
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stderr.close()
        pid = manager.pid(unit)
        if pid and manager.alive(unit):
            os.killpg(pid, signal.SIGKILL)
        (root / 'marker').unlink(missing_ok=True)


def check_cell(result):
    """The pass bar for one cell; returns a list of failures (empty means pass)."""
    failures, phase = [], result['phase']
    if phase == 'running':
        if result['before'] != {'stop': 0, 'state': 'running'}:
            failures.append(f'pre-kill row {result["before"]}')
        if not result['survived']:
            failures.append('run died with the daemon')
        if result['readopted'] != 'running' or not result['alive_after']:
            failures.append(f're-adopt {result["readopted"]} alive={result["alive_after"]}')
        if result['final'] != 'terminated':
            failures.append(f'stop after re-adopt ended {result["final"]}')
    else:
        if result['before'] != {'stop': 1, 'state': 'stopping'}:
            failures.append(f'Stop not durable before kill: {result["before"]}')
        if phase == 'before_signal' and not result['survived']:
            failures.append('run died before any signal')
        if phase == 'before_signal' and (result['observed'][:1] != ['stopping']
                                         or not result['held_alive']):
            failures.append(f'live run with recorded Stop read {result["observed"][:1]}')
        if 'running' in result['observed']:
            failures.append(f're-adopted as running: {result["observed"]}')
        if not result['stop_after']:
            failures.append('recorded Stop lost')
        if result['final'] != 'terminated' or result['alive_after']:
            failures.append(f'final {result["final"]} alive={result["alive_after"]}')
    return failures


def measure(cells=CELLS, phases=PHASES):
    results = {phase: [] for phase in phases}
    with tempfile.TemporaryDirectory() as temp:
        for phase in phases:
            for index in range(cells):
                root = Path(temp) / f'{phase}-{index}'
                root.mkdir(mode=0o700)
                results[phase].append(run_cell(root, index, phase))
    return results


@unittest.skipUnless(sys.platform.startswith('linux') and Path('/proc/self/stat').exists(),
                     'kill -9 cells need Linux /proc')
class KillNineCells(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = measure()

    def _phase(self, phase):
        cells = self.results[phase]
        self.assertEqual(len(cells), CELLS)
        for result in cells:
            with self.subTest(run=result['run_id']):
                self.assertEqual(check_cell(result), [])

    def test_b8_kill9_run_survives_and_is_readopted(self):
        self._phase('running')

    def test_b14_kill9_before_signal_stop_survives(self):
        self._phase('before_signal')

    def test_b14_kill9_after_signal_before_confirmation(self):
        self._phase('after_signal')


class RecordedStopGuard(unittest.TestCase):
    """B14 at the registry: a recorded Stop is never overwritten with running."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = Registry(Path(self.temp.name) / 'state')

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    def test_stale_running_write_after_stop_becomes_stopping(self):
        self.registry.add('one', 'boot-a')
        self.registry.state('one', 'running')
        stale = self.registry.get('one')  # read before the Stop committed
        self.registry.request_stop('one')
        self.registry.state(stale.run_id, 'running')
        run = self.registry.get('one')
        self.assertTrue(run.stop_requested)
        self.assertEqual(run.state, 'stopping')

    def test_other_states_still_recorded_after_stop(self):
        self.registry.add('one', 'boot-a')
        self.registry.request_stop('one')
        self.registry.state('one', 'unknown', 'probe failed')
        self.assertEqual(self.registry.get('one').state, 'unknown')
        self.registry.state('one', 'terminated')
        self.registry.state('one', 'running')
        self.assertEqual(self.registry.get('one').state, 'terminated')

    def test_no_stop_running_unchanged(self):
        self.registry.add('one', 'boot-a')
        self.registry.state('one', 'running')
        self.assertEqual(self.registry.get('one').state, 'running')

    def test_reopened_registry_keeps_stop_after_crash(self):
        self.registry.add('one', 'boot-a')
        self.registry.request_stop('one')
        self.registry.db.close()  # no orderly close: the connection just goes away
        self.registry = Registry(Path(self.temp.name) / 'state')
        run = self.registry.get('one')
        self.assertTrue(run.stop_requested)
        self.assertEqual(run.state, 'stopping')


class ProcessGroupManagerTests(unittest.TestCase):
    def test_reused_pid_with_other_start_time_is_not_the_run(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ProcessGroupManager(Path(temp))
            (Path(temp) / 'recursion-run-x.service').write_text(
                '{"pid": %d, "start": "0"}' % os.getpid())
            self.assertEqual(manager.observe('recursion-run-x.service'), 'empty')
            self.assertIsNotNone(_proc_stat(os.getpid()))

    def test_missing_record_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ProcessGroupManager(Path(temp))
            self.assertEqual(manager.observe('recursion-run-x.service'), 'unknown')
            with self.assertRaises(OSError):
                manager.stop('recursion-run-x.service')


if __name__ == '__main__':
    counts = measure()
    for phase, cells in counts.items():
        passed = sum(not check_cell(c) for c in cells)
        print(f'{phase}: {passed}/{len(cells)} pass; states after restart: '
              f'{sorted({tuple(c["observed"]) for c in cells})}')
        for c in cells:
            for failure in check_cell(c):
                print(f'  FAIL {c["run_id"]}: {failure}')
