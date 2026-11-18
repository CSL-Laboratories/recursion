"""The long-lived Hermes drive owns the gateway inside the run's unit.

Findings B1-2.1, B2-34a.4, B2-34b.1 and B3-2.15. The client-only chat drive (the one-shot argv,
fixtures s06 / exit 3) is a viewer: stopping its unit leaves a gateway process or a parked turn
alive, and a viewer that exits 3 is not a Stop of that turn. The owned drive launches
``hermes gateway run --no-supervise --external-supervisor`` on a dedicated profile in the unit,
and Stop answers stopped only after that gateway's work has ended.

No real Hermes is run. The double is ``fake_gateway.py``.

    PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.hermes.test_hermes_gateway
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from recursion_core.routes.engines import hermes

HERE = Path(__file__).resolve().parent
FAKE = HERE / 'fake_gateway.py'
RUN_ID = 'run-e9gateway'
PROFILE = 'run-e9'

# A process the test starts. It stays in the foreground and writes no turn record.
_SLEEPER = (
    'import signal, sys, time\n'
    'def _end(_signum, _frame):\n'
    '    raise SystemExit(0)\n'
    'signal.signal(signal.SIGINT, _end)\n'
    'signal.signal(signal.SIGTERM, _end)\n'
    'while True:\n'
    '    time.sleep(0.05)\n'
)

# A process the test starts. It forks a child that leaves the launch group and stays in the session.
# The child ends when the flag path (argv 1) appears, or after a short bound so a crashed test cannot leave it.
_SESSION_CHILD = (
    'import json, os, signal, sys, time\n'
    'flag = sys.argv[1]\n'
    'turn = os.environ["RECURSION_E9_GATEWAY_TURN"]\n'
    'ended = False\n'
    'def _end(_signum, _frame):\n'
    '    global ended\n'
    '    ended = True\n'
    'signal.signal(signal.SIGINT, _end)\n'
    'signal.signal(signal.SIGTERM, _end)\n'
    'pid = os.fork()\n'
    'if pid == 0:\n'
    '    os.setpgid(0, 0)\n'
    '    signal.signal(signal.SIGHUP, signal.SIG_IGN)\n'
    '    signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
    '    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
    '    deadline = time.monotonic() + 15\n'
    '    while time.monotonic() < deadline:\n'
    '        if os.path.exists(flag):\n'
    '            os._exit(0)\n'
    '        time.sleep(0.05)\n'
    '    os._exit(0)\n'
    'tmp = turn + ".tmp"\n'
    'with open(tmp, "w", encoding="utf-8") as handle:\n'
    '    json.dump({"alive": True, "pid": os.getpid(), "pgrp": os.getpgrp()}, handle)\n'
    '    handle.flush()\n'
    '    os.fsync(handle.fileno())\n'
    'os.replace(tmp, turn)\n'
    'while not ended:\n'
    '    try:\n'
    '        time.sleep(0.05)\n'
    '    except InterruptedError:\n'
    '        continue\n'
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f'/proc/{pid}/stat', 'rb') as handle:
            data = handle.read()
    except OSError:
        return False
    cut = data.rfind(b')')
    if cut < 0:
        return True
    rest = data[cut + 2:].split()
    return bool(rest) and rest[0] not in (b'Z', b'X')


def _turn(path: str) -> dict | None:
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class _Parked:
    """What today's client-only launch leaves behind: a viewer that has exited 3, and a turn outside the unit."""

    def __init__(self, pid: int, answer: str) -> None:
        self.pid = pid
        self.answer = answer

    @property
    def alive(self) -> bool:
        return _pid_alive(self.pid)


@unittest.skipUnless(os.path.isdir('/proc'), 'the session scan reads /proc; without it Stop stays pending')
class HermesGatewayOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix='e9-gw-')
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, 'home')
        os.mkdir(self.home, 0o700)
        self.turn_path = os.path.join(self.home, 'profiles', PROFILE, '.gateway-turn')
        self._extra: list[int] = []
        self._drives: list = []
        self.addCleanup(self._reap)
        os.chmod(FAKE, 0o700)
        self.drive = None

    def _reap(self) -> None:
        drives = list(self._drives)
        if self.drive is not None and self.drive not in drives:
            drives.append(self.drive)
        for drive in drives:
            if getattr(drive, 'pgid', None):
                try:
                    os.killpg(drive.pgid, signal.SIGKILL)
                except OSError:
                    pass
                proc = getattr(drive, '_proc', None)
                if proc is not None and proc.poll() is None:
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
        for pid in self._extra:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def _today_leaves_the_gateway_alive(self) -> None:
        """The client-only drive, on today's code: Stop of the viewer's unit does not end the parked turn."""
        env = {'PATH': '/usr/bin:/bin', 'HERMES_HOME': self.home, 'HOME': self.home,
               'RECURSION_E9_GATEWAY_TURN': self.turn_path, 'RECURSION_E9_PARK': '1'}
        argv = [sys.executable, '-B', str(FAKE), *hermes.ONE_SHOT_ARGS]
        done = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            code = done.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(done.pid, signal.SIGKILL)
            raise
        self.assertEqual(code, 3, 'the s06/exit 3 viewer did not detach')
        record = _turn(self.turn_path)
        self.assertIsNotNone(record, 'the client-only launch parked no turn to observe')
        assert record is not None
        parked = _Parked(int(record['pid']), 'stopped')
        self._extra.append(parked.pid)
        # The viewer's process group is already empty, so a Stop that watches only the unit's viewer
        # answers stopped. The parked gateway is not in that group.
        try:
            os.killpg(done.pid, signal.SIGKILL)
        except OSError:
            pass
        self.assertFalse(parked.alive,
                         'stopping the run unit left the gateway process or a parked turn alive')
        self.assertNotEqual(parked.answer, 'stopped',
                            'Stop on the run returned stopped while the fake gateway turn was still alive')

    def _drive(self, **kw):
        cls = getattr(hermes, 'HermesGatewayDrive', None)
        if cls is None:
            self._today_leaves_the_gateway_alive()
        drive = cls(str(FAKE), profile=kw.pop('profile', PROFILE), home=self.home, run_id=RUN_ID, **kw)
        self._drives.append(drive)
        self.drive = drive
        return drive

    def _script(self, name: str, source: str) -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(source)
        return path

    def test_stopping_the_unit_ends_the_owned_gateway(self) -> None:
        # B1-2.1: the long-lived drive owns the gateway inside the unit. Stop ends it.
        drive = self._drive()
        drive.launch()
        record = _turn(self.turn_path)
        self.assertEqual(record['argv'], ['gateway', 'run', '--no-supervise', '--external-supervisor',
                                          '--profile', PROFILE])
        self.assertEqual(record['home'], self.home)
        self.assertEqual(record['pgrp'], drive.pgid)
        self.assertTrue(drive.turn_alive())
        self.assertTrue(drive.member_alive())
        answer = drive.stop()
        self.assertEqual(answer, {'code': 'stopped', 'run_id': RUN_ID, 'state': 'stopped', 'repeat': False})
        self.assertFalse(drive.turn_alive())
        self.assertFalse(drive.member_alive())
        again = drive.stop()
        self.assertEqual(again['code'], 'stopped')
        self.assertIs(again['repeat'], True)

    def test_viewer_exit_3_does_not_stop_until_the_gateway_work_ends(self) -> None:
        # B2-34a.4, B2-34b.1, B3-2.15: the chat viewer exits 3 while the gateway keeps the turn.
        drive = self._drive()
        drive.launch()
        code = drive.viewer_exit()
        self.assertEqual(code, 3)
        self.assertTrue(drive.turn_alive(), 'the viewer exit ended the gateway turn')
        self.assertTrue(drive.member_alive())
        answer = drive.stop()
        self.assertEqual(answer['code'], 'stopped')
        self.assertFalse(drive.turn_alive())
        self.assertFalse(drive.member_alive())

    def test_a_shared_or_aliased_profile_is_refused_and_stop_stays_pending(self) -> None:
        drive = self._drive()
        with self.assertRaises(hermes.HermesOwnershipRefused):
            hermes.gateway_argv(str(FAKE), profile='default')
        link = os.path.join(self.home, 'profiles')
        os.symlink(self.tmp.name, link)
        with self.assertRaises(hermes.HermesOwnershipRefused) as caught:
            drive.launch()
        self.assertEqual(caught.exception.cause, 'profile_alias')
        answer = drive.stop()
        self.assertEqual(answer['code'], 'pending')
        self.assertEqual(answer['reason'], 'cgroup_unknown')
        self.assertNotIn('repeat', answer)

    def test_a_launch_that_cannot_start_does_not_answer_stopped(self) -> None:
        missing = os.path.join(self.tmp.name, 'missing-hermes')
        drive = self._drive()
        drive.argv = [missing, *hermes.GATEWAY_ARGS, '--profile', PROFILE]
        with self.assertRaises(OSError):
            drive.launch()
        answer = hermes.route_stop(drive)
        self.assertEqual(answer['code'], 'pending')
        self.assertNotEqual(answer['code'], 'stopped')

    def test_sigint_that_does_not_end_the_gateway_is_not_a_stop(self) -> None:
        with open(os.path.join(self.home, 'ignore-int'), 'w', encoding='ascii') as handle:
            handle.write('1\n')
        drive = self._drive(timeout=0.3)
        drive.launch()
        self.assertTrue(drive.member_alive())
        started = time.monotonic()
        answer = drive.stop()
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual(answer['code'], 'stopped')
        self.assertFalse(drive.member_alive())
        self.assertFalse(drive.turn_alive())

    def test_a_second_caller_sees_one_confirmation(self) -> None:
        drive = self._drive()
        drive.launch()
        answers: list[dict] = []
        barrier = threading.Barrier(2)

        def _call() -> None:
            barrier.wait()
            answers.append(drive.stop())

        threads = [threading.Thread(target=_call), threading.Thread(target=_call)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(item['code'] for item in answers), ['stopped', 'stopped'])
        self.assertEqual(sorted(item['repeat'] for item in answers), [False, True])
        self.assertFalse(drive.turn_alive())

    def test_stop_before_any_launch_is_pending(self) -> None:
        drive = self._drive()
        answer = drive.stop()
        self.assertEqual(answer, {'code': 'pending', 'run_id': RUN_ID, 'state': 'pending',
                                  'reason': 'cgroup_unknown'})

    def test_an_exception_while_stopping_stays_pending(self) -> None:
        drive = self._drive()
        drive.launch()

        def _boom(_sig: int) -> None:
            raise RuntimeError('signal failed')

        drive._signal = _boom  # type: ignore[method-assign]
        answer = drive.stop()
        self.assertEqual(answer['code'], 'pending')
        self.assertEqual(answer['reason'], 'cleanup_error:RuntimeError')
        self.assertTrue(drive.member_alive())
        self.assertFalse(drive.confirmed)

    def test_an_interrupted_stop_does_not_confirm(self) -> None:
        drive = self._drive()
        drive.launch()
        real_signal = drive._signal
        calls = {'n': 0}

        def _interrupt(sig: int) -> None:
            calls['n'] += 1
            if calls['n'] == 1:
                raise KeyboardInterrupt
            return real_signal(sig)

        drive._signal = _interrupt  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            drive.stop()
        self.assertFalse(drive.confirmed)
        self.assertTrue(drive.member_alive())
        answer = drive.stop()
        self.assertEqual(answer['code'], 'stopped')
        self.assertIs(answer['repeat'], False)
        self.assertFalse(drive.member_alive())

    def test_a_profile_swapped_for_a_symlink_is_not_called_stopped(self) -> None:
        drive = self._drive()
        drive.launch()
        profile_dir = os.path.join(self.home, 'profiles', PROFILE)
        held = profile_dir + '.held'
        os.rename(profile_dir, held)
        os.symlink(held, profile_dir)
        answer = drive.stop()
        self.assertEqual(answer['code'], 'pending')
        self.assertEqual(answer['reason'], 'cgroup_changed')
        self.assertFalse(drive.confirmed)

    def test_stop_stays_pending_while_a_session_member_in_another_group_is_alive(self) -> None:
        # R1. A child that left the launch group and stayed in the session is still the unit.
        flag = os.path.join(self.tmp.name, 'child-gone')

        def _release() -> None:
            with open(flag, 'w', encoding='ascii') as handle:
                handle.write('1\n')
            time.sleep(0.3)

        self.addCleanup(_release)
        drive = self._drive(timeout=0.3)
        drive.argv = [sys.executable, '-B', self._script('session_child.py', _SESSION_CHILD), flag]
        drive.launch()
        answer = drive.stop()
        self.assertNotEqual(answer['code'], 'stopped')
        self.assertTrue(drive.member_alive())
        _release()
        deadline = time.monotonic() + 3
        while drive.member_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(drive.member_alive())
        again = drive.stop()
        self.assertEqual(again['code'], 'stopped')

    def test_a_foreground_process_without_a_turn_file_is_the_owned_unit(self) -> None:
        # R2. Readiness is the foreground session leader, not a private turn file.
        drive = self._drive(timeout=0.3)
        drive.argv = [sys.executable, '-B', self._script('sleeper.py', _SLEEPER)]
        drive.launch()
        self.assertTrue(drive._launched)
        self.assertTrue(drive.member_alive())
        self.assertFalse(os.path.exists(drive._turn_path))
        answer = drive.stop()
        self.assertEqual(answer['code'], 'stopped')
        self.assertFalse(drive.member_alive())

    def test_an_interrupted_launch_is_ended_by_stop_and_is_not_stuck(self) -> None:
        # R3. KeyboardInterrupt after Popen must not leave a process Stop will not signal.
        drive = self._drive(timeout=0.4)
        drive.argv = [sys.executable, '-B', self._script('sleeper.py', _SLEEPER)]

        def _interrupt() -> bool:
            raise KeyboardInterrupt

        drive._wait_until_owned = _interrupt  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            drive.launch()
        pid = drive.pgid
        self.assertIsNotNone(pid)
        assert pid is not None
        self.assertTrue(_pid_alive(pid))
        drive.stop()
        self.assertFalse(_pid_alive(pid))
        try:
            drive.launch()
        except hermes.HermesOwnershipRefused as err:
            self.assertNotEqual(err.cause, 'already_launched')
        else:
            drive.stop()

    def test_profile_root_is_refused(self) -> None:
        # R4. The shared name root stays refused. Dropping it from the set makes this red.
        with self.assertRaises(hermes.HermesOwnershipRefused) as caught:
            hermes.gateway_argv(str(FAKE), profile='root')
        self.assertEqual(caught.exception.cause, 'profile')

    def test_two_profiles_on_one_home_keep_separate_turn_records(self) -> None:
        # R5. One profile's turn pid must not keep the other profile's Stop pending.
        with open(os.path.join(self.home, 'ignore-int'), 'w', encoding='ascii') as handle:
            handle.write('1\n')
        first = self._drive(profile='run-a', timeout=0.3)
        second = self._drive(profile='run-b', timeout=0.3)
        first.launch()
        second.launch()
        self.assertNotEqual(first.pgid, second.pgid)
        os.killpg(first.pgid, signal.SIGKILL)
        with open(first._turn_path, 'w', encoding='utf-8') as handle:
            json.dump({'alive': True, 'pid': second.pgid}, handle)
        answer = first.stop()
        self.assertEqual(answer['code'], 'stopped')
        self.assertTrue(second.member_alive())
        self.assertNotEqual(first._turn_path, second._turn_path)
        self.assertTrue(first._turn_path.endswith(os.path.join('profiles', 'run-a', '.gateway-turn')))
        self.assertTrue(second._turn_path.endswith(os.path.join('profiles', 'run-b', '.gateway-turn')))

    def test_stop_does_not_signal_a_pgid_whose_start_time_changed(self) -> None:
        # R6. A populated scan of a pgid that is no longer the launched process is not a signal.
        drive = self._drive(timeout=0.3)
        drive.launch()
        first = drive.stop()
        self.assertEqual(first['code'], 'stopped')
        calls: list[tuple] = []

        def _record(pgid: int, sig: int) -> None:
            calls.append((pgid, sig))
            raise ProcessLookupError

        real_killpg = hermes.os.killpg
        real_scan = hermes._group_populated
        hermes.os.killpg = _record  # type: ignore[assignment]
        hermes._group_populated = lambda _pgid: True  # type: ignore[assignment]
        try:
            drive._launch_start = (getattr(drive, '_launch_start', None) or 0) + 1
            second = drive.stop()
        finally:
            hermes.os.killpg = real_killpg
            hermes._group_populated = real_scan
        self.assertEqual(calls, [])
        self.assertNotEqual(second['code'], 'stopped')

    def test_an_unreadable_proc_stat_is_not_a_dead_pid(self) -> None:
        # R7. Signal 0 succeeded and the stat file could not be opened: that is not death.
        real_open = hermes.os.open

        def _open(path, flags, *args):
            # The only open on this call is the stat file. Raising here is that open failing.
            raise OSError('stat unreadable')

        hermes.os.open = _open  # type: ignore[assignment]
        try:
            result = hermes._pid_alive(os.getpid())
        finally:
            hermes.os.open = real_open
        self.assertIsNot(result, False)


if __name__ == '__main__':
    unittest.main()
