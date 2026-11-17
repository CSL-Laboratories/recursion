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


class HermesGatewayOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix='e9-gw-')
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, 'home')
        os.mkdir(self.home, 0o700)
        self.turn_path = os.path.join(self.home, '.gateway-turn')
        self._extra: list[int] = []
        self.addCleanup(self._reap)
        os.chmod(FAKE, 0o700)
        self.drive = None

    def _reap(self) -> None:
        drive = self.drive
        if drive is not None and getattr(drive, 'pgid', None):
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
        self.drive = drive
        return drive

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


if __name__ == '__main__':
    unittest.main()
