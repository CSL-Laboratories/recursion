"""The start path of the daemon's control adapter survives a dead worker, a bad route number, an interrupt and a FIFO.

Red tests of the sweep triage of daemon (STOP, CSL-238), entries E04, E05, E34, E19 and E18: each fails on
master without the change in ``control_adapter.py`` and passes with it. Every fixture is synthetic and lies
below a temporary folder; no manager, engine or provider runs.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from recursion_core.control.api import ControlAPI
from recursion_core.control.client import SocketAPI
from recursion_core.control.registry import EMPTY, InMemoryRunRegistry
from recursion_core.control_transport import UnixControlServer
from recursion_core.control_transport.framing import MAX_FRAME_BYTES
from recursion_core.daemon import control_adapter as adapter, launcher
from recursion_core.routes import contract

LINUX = sys.platform.startswith('linux')
CHILD_LIMIT_S = 20


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class StartPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='start-path-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_dir(self):
        folder = self.root / 'runs' / 'run-one'
        folder.mkdir(parents=True, mode=0o700)
        return folder

    def child(self, code):
        """``code`` in a fresh interpreter with the root as argv[1]; a child that blocks is killed and fails the test."""
        try:
            return subprocess.run([sys.executable, '-B', '-c', code, str(self.root)], capture_output=True,
                                  text=True, timeout=CHILD_LIMIT_S, check=False)
        except subprocess.TimeoutExpired:
            self.fail(f'blocked for {CHILD_LIMIT_S} s on a FIFO; the child was killed')

    # -- E04: a worker that never started owns no run ----------------------------------------------
    def test_a_failed_worker_start_releases_the_active_run(self):
        routes = {'claude_code': {'deadline_s': 1, 'route': {}, 'binary': None}}
        starter = adapter.RouteStarter(mock.Mock(), routes, mock.Mock(), self.root)
        api = ControlAPI(InMemoryRunRegistry(), mock.Mock(), starter, time.time, new_run_id=lambda: 'run-one')
        params = {'tenant': 't', 'engine': 'claude_code', 'prompt': 'p', 'client_key': 'one'}
        with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError('worker unavailable')):
            first = api.call('run.start', params)
        self.assertEqual(first['code'], 'start_failed')
        self.assertFalse(starter.active('run-one'), 'a thread that never started owns no run')
        self.assertEqual(starter.failures['run-one']['code'], 'worker_start_failed')
        self.assertEqual(starter.failures['run-one']['detail'], 'RuntimeError', 'the message stays out')
        # The same id is not "in flight" for ever: a retry reaches the worker start again.
        for _ in range(2):
            with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError('worker unavailable')):
                with self.assertRaises(adapter.StartRefused) as refused:
                    starter({**params, 'run_id': 'run-one'})
            self.assertEqual(refused.exception.code, 'worker_start_failed')

    # -- E05: a deadline is a finite number of seconds ---------------------------------------------
    def test_a_deadline_must_be_finite_and_convertible(self):
        path = self.root / 'routes.json'
        for label, text in (('nan', 'NaN'), ('infinite', 'Infinity'), ('minus infinite', '-Infinity'),
                            ('overflow', '1' + '0' * 400)):
            with self.subTest(kind=label):
                path.write_text('{"routes": {"claude_code": {"deadline_s": %s, "model": "m", '
                                '"connection": {"kind": "subscription", "id": "local"}}}}' % text)
                os.chmod(path, 0o600)
                with self.assertRaises(adapter.RoutesUnreadable) as caught:
                    adapter.load_routes(path)
                self.assertEqual(str(caught.exception), 'route claude_code: deadline_s')

    def test_a_finite_deadline_still_loads_as_a_float(self):
        path = self.root / 'routes.json'
        path.write_text('{"routes": {"codex": {"deadline_s": 90, "model": "m", '
                        '"connection": {"kind": "subscription", "id": "local"}}}}')
        os.chmod(path, 0o600)
        loaded = adapter.load_routes(path)
        self.assertEqual(loaded['codex']['deadline_s'], 90.0)
        self.assertIsInstance(loaded['codex']['deadline_s'], float)

    # -- E34: an interrupt from the route callback still ends the published child ---------------------
    def published_run(self):
        sleeper = subprocess.Popen(['/bin/sleep', '4'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)

        def finish():
            if sleeper.poll() is None:
                sleeper.kill()
            sleeper.wait(timeout=5)
        self.addCleanup(finish)
        out = self.root / 'stdout.jsonl'
        out.touch()
        daemon = SimpleNamespace(stop=mock.Mock(), closed=False,
                                 reconcile=mock.Mock(return_value=SimpleNamespace(state='running')))
        process = launcher.UnitEngineProcess(daemon, 'run-witness', None, out, os.pidfd_open(sleeper.pid))
        self.addCleanup(process.close)
        ctx = launcher.RunContext('run-witness')
        ctx.launched('/test/recursion-run-run-witness.service', process)
        return ctx, process, daemon

    def test_an_interrupt_in_the_route_callback_cleans_up_the_published_child(self):
        for interrupt in (KeyboardInterrupt, SystemExit):
            with self.subTest(interrupt=interrupt.__name__):
                ctx, process, daemon = self.published_run()
                service = SimpleNamespace(handle=mock.Mock(side_effect=interrupt('callback')))
                starter = adapter.RouteStarter(service, {}, daemon, self.root, exit_grace_s=3.0)
                starter._active[ctx.run_id] = ctx
                began = time.monotonic()
                with self.assertRaises(interrupt):
                    starter._drive(ctx, {})
                self.assertLess(time.monotonic() - began, 2, 'an interrupt does not wait out the exit grace')
                daemon.reconcile.assert_not_called()
                daemon.stop.assert_called_once_with('run-witness')
                self.assertEqual(process.fenced, ['route_interrupted'])
                self.assertIsNone(process._out)
                self.assertIsNone(process._pidfd)
                self.assertFalse(starter.active(ctx.run_id))
                self.assertEqual(ctx.result['code'], contract.ENGINE_ERROR, 'a waiting start is released')
                self.assertEqual(ctx.result['detail'], f'route failure: {interrupt.__name__}')

    def test_an_ordinary_route_failure_still_waits_for_the_engine_to_exit(self):
        ctx, process, daemon = self.published_run()
        daemon.reconcile = mock.Mock(return_value=SimpleNamespace(state='terminated'))
        service = SimpleNamespace(handle=mock.Mock(side_effect=RuntimeError('route bug')))
        starter = adapter.RouteStarter(service, {}, daemon, self.root, exit_grace_s=30.0)
        starter._active[ctx.run_id] = ctx
        starter._drive(ctx, {})
        daemon.reconcile.assert_called_once_with('run-witness')
        daemon.stop.assert_not_called()
        self.assertEqual(process.fenced, [])
        self.assertIsNone(process._out)
        self.assertFalse(starter.active(ctx.run_id))

    # -- E19: a FIFO in a run's folder is not a file to wait on ---------------------------------------
    def test_a_result_fifo_does_not_block_a_read(self):
        os.mkfifo(self.run_dir() / 'result.json', 0o600)
        done = self.child('from pathlib import Path\n'
                          'from types import SimpleNamespace\n'
                          'import sys\n'
                          'from recursion_core.daemon.control_adapter import DaemonControl\n'
                          'control = object.__new__(DaemonControl)\n'
                          'control._state = Path(sys.argv[1])\n'
                          'control.starter = SimpleNamespace(results={})\n'
                          'print(repr(control._result("run-one")))\n')
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'None'), done.stderr)

    def test_a_stdout_fifo_does_not_block_a_read(self):
        os.mkfifo(self.run_dir() / 'stdout.jsonl', 0o600)
        done = self.child('from pathlib import Path\n'
                          'import sys\n'
                          'from recursion_core.daemon.control_adapter import DaemonControl\n'
                          'control = object.__new__(DaemonControl)\n'
                          'control._state = Path(sys.argv[1])\n'
                          'print(control._output("run-one", True))\n')
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "{'unavailable': 'OSError'}"), done.stderr)

    def test_a_result_temp_fifo_does_not_block_the_write(self):
        folder = self.run_dir()
        os.mkfifo(folder / 'result.json.tmp', 0o600)
        done = self.child('from pathlib import Path\n'
                          'import sys\n'
                          'from recursion_core.daemon.control_adapter import RouteStarter\n'
                          'starter = RouteStarter(None, {}, None, Path(sys.argv[1]))\n'
                          'starter._record_result("run-one", {"status": "ok"})\n'
                          'print(starter.results)\n')
        self.assertEqual((done.returncode, done.stdout.strip()), (0, '{}'), done.stderr)
        self.assertEqual(json.loads((folder / 'result.json').read_text()), {'status': 'ok'})
        self.assertFalse((folder / 'result.json.tmp').exists())

    def test_a_regular_result_and_stdout_are_still_read(self):
        folder = self.run_dir()
        (folder / 'result.json').write_text(json.dumps({'status': 'ok', 'code': None}))
        (folder / 'stdout.jsonl').write_bytes(b'{"a":1}\n{"b":2}\n')
        control = object.__new__(adapter.DaemonControl)
        control._state, control.starter = self.root, SimpleNamespace(results={})
        self.assertEqual(control._result('run-one'), {'status': 'ok', 'code': None})
        self.assertEqual(control._output('run-one', False),
                         {'bytes': 16, 'lines': ['{"a":1}', '{"b":2}'], 'complete': True})

    # -- E18: run.status answers however large the route result is ------------------------------------
    def status_server(self, result):
        registry = InMemoryRunRegistry()
        registry.put({'run_id': 'run-one', 'tenant': 't', 'engine': 'claude_code',
                      'state': 'running', 'cgroup': '/slice/recursion-run-run-one.service',
                      'started_at': 1.0, 'stop': None, 'start_key': None})
        kill = SimpleNamespace(request_stop=lambda group: None, confirm=lambda group, timeout: EMPTY)
        api = ControlAPI(registry, kill, mock.Mock(), time.time)
        unit_registry = mock.Mock()
        unit_registry.get.side_effect = KeyError('no unit row')
        control = adapter.DaemonControl(api, mock.Mock(), SimpleNamespace(registry=unit_registry),
                                        SimpleNamespace(results={'run-one': result}, failures={}), self.root)
        server = UnixControlServer(self.root / 'socket', control.handle)
        server.start()
        self.addCleanup(server.close)
        return registry, server

    def test_a_large_route_result_keeps_stop_and_status_readable(self):
        result = contract.result('run-one', None, text='x' * MAX_FRAME_BYTES)
        registry, server = self.status_server(result)
        stopped = SocketAPI(str(server.path), timeout_s=3).call('run.stop', {'run_id': 'run-one'})
        self.assertEqual(registry.get('run-one')['state'], 'stopped', 'cleanup really completed')
        self.assertEqual(stopped['code'], 'stopped', stopped)
        status = SocketAPI(str(server.path), timeout_s=3).call('run.status', {'run_id': 'run-one'})
        self.assertEqual(status['code'], 'ok', status)
        carried = status['run']['result']
        self.assertEqual((carried['status'], carried['code'], carried['truncated']), ('ok', None, True))
        self.assertTrue(carried['text'] and set(carried['text']) == {'x'})
        self.assertLess(len(carried['text']), len(result['text']))

    def test_a_small_route_result_is_carried_whole(self):
        result = contract.result('run-one', None, text='done', events=[{'k': 1}])
        _, server = self.status_server(result)
        status = SocketAPI(str(server.path), timeout_s=3).call('run.status', {'run_id': 'run-one'})
        self.assertEqual(status['code'], 'ok', status)
        self.assertEqual(status['run']['result'], result)

    def test_a_route_result_of_many_escaped_characters_fits_the_budget(self):
        result = contract.result('run-one', None, text=chr(1) * (MAX_FRAME_BYTES // 2), detail='d')
        shown = adapter._status_result(result)
        self.assertTrue(shown['truncated'] and shown['text'])
        self.assertLessEqual(adapter._wire_size(shown), adapter.RESULT_STATUS_BYTES)
        self.assertEqual((shown['status'], shown['code'], shown['detail']), ('ok', None, 'd'))


if __name__ == '__main__':
    unittest.main()
