"""E30: a trip the store does not take, and a fault lost before it is written, survive a restart.

The two tests are the sweep's ``test_f04_failed_trip_write_does_not_reopen_route_after_restart``
and ``test_f04_crash_after_error_result_does_not_lose_run_fault``. They express the behaviour
the store should have. Fixture bytes are generated here.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from recursion_core.daemon import breaker as dbreaker
from recursion_core.daemon.control_adapter import RouteStarter
from recursion_core.daemon.launcher import RunContext
from recursion_core.routes import ConnectionBreaker
from recursion_core.routes import contract


def python_argv():
    if sys.version_info[:2] == (3, 14):
        return [sys.executable, '-X', 'gil=0', '-B']
    return [sys.executable, '-B']


def child(mode, folder):
    root = Path(folder)
    if mode == 'fault-crash':
        stored, _ = dbreaker.open_breaker_store(root, clock=lambda: 10.0)
        wrapped = dbreaker.DurableRouteBreaker(ConnectionBreaker(lambda: 10.0), stored)
        assert wrapped.bind_run('run-a', 'connection-a', 'codex', 'model-a') is None
        service = types.SimpleNamespace(handle=lambda _: {'status': 'error', 'code': contract.ENGINE_ERROR})
        starter = RouteStarter(service, {}, types.SimpleNamespace(closed=False), root, breaker=wrapped)
        ctx = RunContext('run-a')
        ctx.process = object()  # marks a launched run; no process is actually launched
        def die_before_fault_write(_):
            os._exit(77)
        wrapped.fault_run = die_before_fault_write
        starter._drive(ctx, {})
        raise AssertionError('did not reach crash point')
    raise ValueError(mode)


def emit(name, **values):
    print('WITNESS ' + json.dumps({'case': name, **values}, sort_keys=True), flush=True)


class DiskRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='s6-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_dir(self):
        folder = self.root / 'runs' / 'run-a'
        folder.mkdir(parents=True, mode=0o700)
        return folder

    def test_f04_failed_trip_write_does_not_reopen_route_after_restart(self):
        stored, _ = dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        wrapped = dbreaker.DurableRouteBreaker(ConnectionBreaker(lambda: 10.0), stored)
        self.assertTrue(wrapped.admit('connection-a', 'model-a', 'tenant-a')['admitted'])
        with patch.object(stored, 'report', side_effect=sqlite3.OperationalError('database or disk is full')):
            effect = wrapped.record('connection-a', 'model-a', 'tenant-a',
                                    {'code': contract.PLAN_LIMIT,
                                     'limit': contract.limit_signal('connection', 'witness')})
        before = wrapped.state('connection-a')
        wrapped.close()
        stored, _ = dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        rebooted = dbreaker.DurableRouteBreaker(ConnectionBreaker(lambda: 10.0), stored)
        try:
            admission = rebooted.admit('connection-a', 'model-a', 'tenant-a')
            emit('f04', returned_effect=effect, before_restart=before,
                 admitted_after_restart=admission['admitted'], durable_trip=stored.tripped('connection-a'))
            self.assertFalse(admission['admitted'], 'restart forgot a trip whose disk write failed')
        finally:
            rebooted.close()

    def test_f04_crash_after_error_result_does_not_lose_run_fault(self):
        self.run_dir()
        proc = subprocess.run([*python_argv(), __file__, 'child', 'fault-crash', str(self.root)],
                              capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 77, proc.stderr)
        result = json.loads((self.root / 'runs' / 'run-a' / 'result.json').read_text())
        self.assertEqual(result['code'], contract.ENGINE_ERROR)
        stored, _ = dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        checked = None
        refusal = None
        try:
            try:
                checked = stored.check('run-a', 'connection-a', 'host')
            except dbreaker.Refused as exc:
                refusal = exc.code
            emit('f04-fault-crash', error_result_present=True, stored_check=checked, refusal=refusal)
            self.assertEqual(refusal, 'run_faulted', 'restart trusts a run whose durable result is an error')
        finally:
            stored.close()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'child':
        child(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
