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
import threading
import time
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
    if mode == 'trip-crash':
        stored, _ = dbreaker.open_breaker_store(root, clock=lambda: 10.0)
        wrapped = dbreaker.DurableRouteBreaker(ConnectionBreaker(lambda: 10.0), stored)
        assert wrapped.admit('connection-a', 'model-a', 'tenant-a')['admitted']
        with patch.object(stored, 'report', side_effect=sqlite3.OperationalError('database or disk is full')):
            wrapped.record('connection-a', 'model-a', 'tenant-a',
                           {'code': contract.PLAN_LIMIT,
                            'limit': contract.limit_signal('connection', 'witness')})
        os._exit(78)
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

    def open_store(self):
        stored, _ = dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)

        def shut(store=stored):
            try:
                store.close()
            except sqlite3.Error:
                pass

        self.addCleanup(shut)
        return stored

    def plant(self, name, raw):
        security = self.root / 'plugin-security'
        security.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = security / name
        if isinstance(raw, str):
            path.write_text(raw)
        else:
            path.write_bytes(raw)
        os.chmod(path, 0o600)
        return path

    def bind(self, stored, run_id):
        stored.bind_run(run_id, 'connection-a', 'host', 'codex', 'model-a')

    def test_a_refused_pending_trip_does_not_wedge_the_next_open(self):
        self.open_store().close()
        pending = self.plant('breaker-pending.json', json.dumps({'trips': ['bad id'], 'faults': []}) + '\n')
        stored = self.open_store()
        stored.close()
        self.assertFalse(pending.exists(), 'a trip the store refuses stayed and wedges the next open')
        self.open_store().close()

    def test_an_empty_pending_file_is_a_store_refusal(self):
        self.open_store().close()
        pending = self.plant('breaker-pending.json', b'')
        with self.assertRaises(dbreaker.Refused) as caught:
            dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        self.assertEqual(caught.exception.code, 'breaker_store_untrusted')
        self.assertTrue(pending.exists(), 'an unreadable pending file was removed')

    def test_a_pending_file_that_is_not_json_is_a_store_refusal(self):
        self.open_store().close()
        self.plant('breaker-pending.json', b'{')
        with self.assertRaises(dbreaker.Refused) as caught:
            dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        self.assertEqual(caught.exception.code, 'breaker_store_untrusted')

    def test_a_pending_file_that_is_not_utf8_is_a_store_refusal(self):
        self.open_store().close()
        self.plant('breaker-pending.json', b'\xff')
        with self.assertRaises(dbreaker.Refused) as caught:
            dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        self.assertEqual(caught.exception.code, 'breaker_store_untrusted')

    def test_an_error_result_over_one_mebibyte_is_faulted(self):
        stored = self.open_store()
        self.bind(stored, 'run-big')
        stored.close()
        folder = self.root / 'runs' / 'run-big'
        folder.mkdir(parents=True, mode=0o700)
        payload = json.dumps({'code': contract.ENGINE_ERROR, 'detail': 'x' * (1024 * 1024)}).encode()
        self.assertGreater(len(payload), 1024 * 1024)
        self.plant_result(folder, payload)
        stored = self.open_store()
        with self.assertRaises(dbreaker.Refused) as caught:
            stored.check('run-big', 'connection-a', 'host')
        self.assertEqual(caught.exception.code, 'run_faulted')

    def test_an_error_result_that_is_not_utf8_is_faulted(self):
        stored = self.open_store()
        self.bind(stored, 'run-bin')
        stored.close()
        folder = self.root / 'runs' / 'run-bin'
        folder.mkdir(parents=True, mode=0o700)
        self.plant_result(folder, b'\xff\xfe')
        stored = self.open_store()
        with self.assertRaises(dbreaker.Refused) as caught:
            stored.check('run-bin', 'connection-a', 'host')
        self.assertEqual(caught.exception.code, 'run_faulted')

    def test_a_successful_result_is_not_faulted(self):
        stored = self.open_store()
        self.bind(stored, 'run-ok')
        stored.close()
        folder = self.root / 'runs' / 'run-ok'
        folder.mkdir(parents=True, mode=0o700)
        self.plant_result(folder, b'{"code": null}\n')
        stored = self.open_store()
        self.assertEqual(stored.check('run-ok', 'connection-a', 'host')['provider'], 'codex')

    def test_a_symlinked_error_result_is_not_faulted(self):
        stored = self.open_store()
        self.bind(stored, 'run-link')
        stored.close()
        private = self.root / 'private'
        private.mkdir(mode=0o700)
        target = private / 'result.json'
        target.write_text(json.dumps({'code': contract.ENGINE_ERROR}))
        os.chmod(target, 0o600)
        folder = self.root / 'runs' / 'run-link'
        folder.mkdir(parents=True, mode=0o700)
        (folder / 'result.json').symlink_to(target)
        stored = self.open_store()
        self.assertEqual(stored.check('run-link', 'connection-a', 'host')['provider'], 'codex')

    def test_a_read_only_plugin_security_directory_does_not_keep_the_trip(self):
        self.assertIn('same filesystem as the store', dbreaker.__doc__)
        self.assertIn('no journal on another mount', dbreaker.__doc__)
        stored = self.open_store()
        wrapped = dbreaker.DurableRouteBreaker(ConnectionBreaker(lambda: 10.0), stored)
        self.assertTrue(wrapped.admit('connection-a', 'model-a', 'tenant-a')['admitted'])
        security = self.root / 'plugin-security'
        os.chmod(security, 0o500)
        try:
            with self.assertLogs('recursion_core.daemon.breaker', 'ERROR') as logs:
                with patch.object(stored, 'report', side_effect=sqlite3.OperationalError('database or disk is full')):
                    wrapped.record('connection-a', 'model-a', 'tenant-a',
                                   {'code': contract.PLAN_LIMIT,
                                    'limit': contract.limit_signal('connection', 'witness')})
            self.assertFalse((security / 'breaker-pending.json').exists())
            self.assertTrue(any('not kept for the next open' in line for line in logs.output), logs.output)
        finally:
            os.chmod(security, 0o700)
        wrapped.close()
        stored = self.open_store()
        rebooted = dbreaker.DurableRouteBreaker(ConnectionBreaker(lambda: 10.0), stored)
        self.assertTrue(rebooted.admit('connection-a', 'model-a', 'tenant-a')['admitted'])

    def test_an_unreadable_runs_directory_does_not_refuse_the_open(self):
        runs = self.root / 'runs'
        runs.mkdir(mode=0o700)
        os.chmod(runs, 0)
        try:
            with self.assertLogs('recursion_core.daemon.breaker', 'ERROR') as logs:
                stored = self.open_store()
            self.assertTrue(any('runs directory could not be read' in line for line in logs.output), logs.output)
            stored.close()
        finally:
            os.chmod(runs, 0o700)

    def test_many_error_results_are_faulted_in_one_open(self):
        count = 400
        for index in range(count):
            folder = self.root / 'runs' / f'run-{index:04d}'
            folder.mkdir(parents=True, mode=0o700)
            self.plant_result(folder, b'{"code": "engine_error"}\n')
        started = time.monotonic()
        stored = self.open_store()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, f'{count} error results took {elapsed:.2f}s')
        self.bind(stored, 'run-0000')
        with self.assertRaises(dbreaker.Refused) as caught:
            stored.check('run-0000', 'connection-a', 'host')
        self.assertEqual(caught.exception.code, 'run_faulted')
        row = stored._db.execute('SELECT COUNT(*) FROM faulted').fetchone()[0]
        self.assertEqual(row, count)

    def test_a_store_that_cannot_be_weakly_referenced_still_builds(self):
        with self.assertLogs('recursion_core.daemon.breaker', 'WARNING') as logs:
            wrapped = dbreaker.DurableRouteBreaker(ConnectionBreaker(lambda: 10.0), object())
        self.assertIsNone(wrapped._store_file)
        self.assertTrue(any('not kept for the next open' in line for line in logs.output), logs.output)

    def test_two_breakers_keep_every_pending_trip(self):
        stored = self.open_store()
        first = dbreaker.DurableRouteBreaker(ConnectionBreaker(lambda: 10.0), stored)
        second = dbreaker.DurableRouteBreaker(ConnectionBreaker(lambda: 10.0), stored)
        barrier = threading.Barrier(2)

        def keep(wrapper, names):
            barrier.wait(5)
            for name in names:
                wrapper._keep_pending(trip=name)

        left = [f'left-{index:04d}' for index in range(80)]
        right = [f'right-{index:04d}' for index in range(80)]
        threads = [threading.Thread(target=keep, args=(first, left)),
                   threading.Thread(target=keep, args=(second, right))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        pending = self.root / 'plugin-security' / 'breaker-pending.json'
        data = json.loads(pending.read_text())
        self.assertEqual(sorted(data['trips']), sorted(left + right))

    def test_a_trip_the_store_failed_to_write_is_applied_on_the_next_open_without_shutdown(self):
        proc = subprocess.run([*python_argv(), __file__, 'child', 'trip-crash', str(self.root)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 78, proc.stderr)
        stored = self.open_store()
        rebooted = dbreaker.DurableRouteBreaker(ConnectionBreaker(lambda: 10.0), stored)
        self.assertFalse(rebooted.admit('connection-a', 'model-a', 'tenant-a')['admitted'])

    def test_a_dead_pending_temp_is_swept_and_a_live_one_stays(self):
        self.open_store().close()
        security = self.root / 'plugin-security'
        dead = security / '.breaker-pending.json.99999999.tmp'
        own = security / f'.breaker-pending.json.{os.getpid()}.tmp'
        dead.write_text('dead')
        own.write_text('own')
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        live = security / f'.breaker-pending.json.{proc.pid}.tmp'
        try:
            live.write_text('live')
            self.open_store().close()
            self.assertFalse(dead.exists())
            self.assertFalse(own.exists())
            self.assertTrue(live.exists())
        finally:
            proc.kill()
            proc.wait(5)

    def test_a_value_error_opening_the_store_does_not_escape_composition(self):
        from recursion_core.daemon import control_adapter as adapter
        from recursion_core.daemon.launcher import EngineUnitManager
        daemon = types.SimpleNamespace(manager=EngineUnitManager(), registry=object())
        seen = {}

        def capture(*args, **kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(handle=lambda *_args, **_kwargs: None)

        def composition(control, server, door, plugins=None, breaker=None):
            return types.SimpleNamespace(control=control, breaker=breaker, close=lambda: None)

        door = types.SimpleNamespace(publish=lambda: None, close=lambda: None)
        server = types.SimpleNamespace(start=lambda: None, close=lambda: None)
        with patch.object(adapter, 'open_breaker_store', side_effect=json.JSONDecodeError('bad', 'doc', 0)), \
                patch.object(adapter, 'DurableRunRegistry', return_value=types.SimpleNamespace(list=lambda: [])), \
                patch.object(adapter, 'PluginHost', return_value=types.SimpleNamespace(close=lambda: None)), \
                patch.object(adapter, 'ProductionLauncher', return_value=object()), \
                patch.object(adapter, 'ClaudeCodeAdapter', return_value=object()), \
                patch.object(adapter, 'CodexAdapter', return_value=object()), \
                patch.object(adapter, 'RouteService', return_value=object()), \
                patch.object(adapter, 'RouteStarter', return_value=object()), \
                patch.object(adapter, 'ControlAPI', return_value=object()), \
                patch.object(adapter, 'UnitKillTree', return_value=object()), \
                patch.object(adapter, 'DaemonControl', side_effect=capture), \
                patch.object(adapter, 'Composition', side_effect=composition), \
                patch.object(adapter, 'socket_directory', return_value=self.root), \
                patch.object(adapter, 'DeviceDoor', return_value=door), \
                patch.object(adapter, 'UnixControlServer', return_value=server):
            composed = adapter.compose_control(
                daemon, self.root, routes={},
                environ={'XDG_CONFIG_HOME': str(self.root), 'XDG_RUNTIME_DIR': str(self.root)})
        self.assertIsNone(composed.breaker)
        self.assertIn('JSONDecodeError', seen['breaker_store'])
        composed.close()

    def plant_result(self, folder, raw):
        path = folder / 'result.json'
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        return path


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'child':
        child(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
