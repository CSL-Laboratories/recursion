"""The start path of the daemon's control adapter survives a dead worker, a bad route number, an interrupt and a FIFO.

Red tests of the sweep triage of daemon (STOP, CSL-238), entries E04, E05, E34, E19 and E18. The tests that
describe a defect fail on master without the change in ``control_adapter.py`` and pass with it. Four controls
pin behaviour that already held and pass on master too: a finite deadline, a regular result file, a small
route result carried whole, and an ordinary route failure waiting for the engine to exit. Every fixture is
synthetic and lies below a temporary folder; no manager, engine or provider runs.
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


def result_fits(value):
    """Whether ``value`` matches a branch of the public ``run.view.v1`` result."""
    root = Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from conformance.rules import fits
    return fits('run.view.v1.json#/$defs/result', value)


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

    # -- E18: run.status stays readable however large the route result's text or detail is ------------
    # A cut result matches the public record (no added member) and fits RESULT_STATUS_BYTES.
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
        self.assertEqual((carried['status'], carried['code'], carried['detail']), ('ok', None, ''))
        self.assertNotIn('truncated', carried)
        self.assertTrue(result_fits(carried), carried)
        self.assertLessEqual(adapter._wire_size(carried), adapter.RESULT_STATUS_BYTES)
        self.assertTrue(carried['text'] and set(carried['text']) == {'x'})
        self.assertLess(len(carried['text']), len(result['text']))
        self.assertEqual(carried['contract'], result['contract'])
        self.assertEqual(carried['request_id'], result['request_id'])
        self.assertEqual(carried['events'], [])

    def test_a_small_route_result_is_carried_whole(self):
        result = contract.result('run-one', None, text='done', events=[{'k': 1}])
        _, server = self.status_server(result)
        status = SocketAPI(str(server.path), timeout_s=3).call('run.status', {'run_id': 'run-one'})
        self.assertEqual(status['code'], 'ok', status)
        self.assertEqual(status['run']['result'], result)

    def test_a_route_result_of_many_escaped_characters_fits_the_budget(self):
        result = contract.result('run-one', None, text=chr(1) * (MAX_FRAME_BYTES // 2), detail='d')
        shown = adapter._status_result(result)
        self.assertNotIn('truncated', shown)
        self.assertTrue(shown['text'])
        self.assertTrue(result_fits(shown), shown)
        self.assertLessEqual(adapter._wire_size(shown), adapter.RESULT_STATUS_BYTES)
        self.assertEqual((shown['status'], shown['code'], shown['detail']), ('ok', None, 'd'))

    def test_a_large_detail_keeps_status_readable(self):
        result = contract.result('run-one', None, text='kept', detail='d' * MAX_FRAME_BYTES)
        fixed = {'status': result['status'], 'code': result['code'], 'detail': result['detail']}
        self.assertGreater(adapter._wire_size(fixed), adapter.RESULT_STATUS_BYTES)
        _, server = self.status_server(result)
        status = SocketAPI(str(server.path), timeout_s=3).call('run.status', {'run_id': 'run-one'})
        self.assertEqual(status['code'], 'ok', status)
        carried = status['run']['result']
        self.assertTrue(result_fits(carried), carried)
        self.assertLessEqual(adapter._wire_size(carried), adapter.RESULT_STATUS_BYTES)
        self.assertEqual(carried['text'], 'kept')
        self.assertTrue(carried['detail'] and set(carried['detail']) <= {'d'})
        self.assertLess(len(carried['detail']), len(result['detail']))
        self.assertNotIn('truncated', carried)

    def test_a_result_under_the_frame_but_over_the_status_budget_stays_readable(self):
        # A budget equal to the whole frame would carry this result uncut, and the answer would then
        # be too large. The carried result has to stay within a quarter of the frame.
        quarter = MAX_FRAME_BYTES // 4
        lo, hi, chosen = 0, MAX_FRAME_BYTES, None
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = contract.result('run-one', None, text='x' * mid)
            size = adapter._wire_size(candidate)
            if size <= MAX_FRAME_BYTES - 200:
                chosen = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        self.assertIsNotNone(chosen)
        self.assertGreater(adapter._wire_size(chosen), quarter)
        self.assertLess(adapter._wire_size(chosen), MAX_FRAME_BYTES)
        _, server = self.status_server(chosen)
        status = SocketAPI(str(server.path), timeout_s=3).call('run.status', {'run_id': 'run-one'})
        self.assertEqual(status['code'], 'ok', status)
        carried = status['run']['result']
        self.assertLessEqual(adapter._wire_size(carried), quarter)
        self.assertTrue(result_fits(carried), carried)

    def test_a_large_event_list_is_shortened_and_still_matches_the_record(self):
        one = contract.event('text', 'vendor', {'n': 1})
        result = contract.result('run-one', None, text='kept', events=[one] * 20000)
        self.assertGreater(adapter._wire_size(result), adapter.RESULT_STATUS_BYTES)
        shown = adapter._status_result(result)
        self.assertEqual(shown['text'], 'kept')
        self.assertLess(len(shown['events']), len(result['events']))
        self.assertTrue(all(event == one for event in shown['events']))
        self.assertLessEqual(adapter._wire_size(shown), adapter.RESULT_STATUS_BYTES)
        self.assertTrue(result_fits(shown), shown)
        self.assertNotIn('truncated', shown)

    def test_a_large_limit_notice_is_dropped_so_the_result_still_fits(self):
        result = contract.result('run-one', None, text='kept')
        result['limit_notice'] = {'blob': 'n' * MAX_FRAME_BYTES}
        shown = adapter._status_result(result)
        self.assertIsNone(shown['limit_notice'])
        self.assertEqual(shown['text'], 'kept')
        self.assertLessEqual(adapter._wire_size(shown), adapter.RESULT_STATUS_BYTES)
        self.assertTrue(result_fits(shown), shown)

    # -- an interrupt one step after the callback still fences the published child -------------------
    def test_an_interrupt_while_recording_the_result_still_fences_the_child(self):
        ctx, process, daemon = self.published_run()
        service = SimpleNamespace(handle=mock.Mock(return_value=contract.result(ctx.run_id, None, text='ok')))
        starter = adapter.RouteStarter(service, {}, daemon, self.root, exit_grace_s=3.0)
        starter._active[ctx.run_id] = ctx
        real = starter._record_result

        def boom(run_id, result):
            starter._record_result = real
            raise KeyboardInterrupt('record')

        starter._record_result = boom
        began = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            starter._drive(ctx, {})
        self.assertLess(time.monotonic() - began, 2, 'an interrupt does not wait out the exit grace')
        daemon.stop.assert_called_once_with(ctx.run_id)
        self.assertEqual(process.fenced, ['route_interrupted'])
        self.assertIsNone(process._out)
        self.assertIsNone(process._pidfd)
        self.assertFalse(starter.active(ctx.run_id))
        self.assertEqual(ctx.result['text'], 'ok')

    def test_an_interrupt_while_finishing_the_run_still_fences_the_child(self):
        ctx, process, daemon = self.published_run()
        service = SimpleNamespace(handle=mock.Mock(return_value=contract.result(ctx.run_id, None, text='ok')))
        starter = adapter.RouteStarter(service, {}, daemon, self.root, exit_grace_s=3.0)
        starter._active[ctx.run_id] = ctx
        real = ctx.finished

        def boom(result):
            ctx.finished = real
            raise KeyboardInterrupt('finish')

        ctx.finished = boom
        with self.assertRaises(KeyboardInterrupt):
            starter._drive(ctx, {})
        daemon.stop.assert_called_once_with(ctx.run_id)
        self.assertEqual(process.fenced, ['route_interrupted'])
        self.assertIsNone(process._out)
        self.assertIsNone(process._pidfd)
        self.assertEqual(ctx.result['text'], 'ok')
        self.assertFalse(starter.active(ctx.run_id))

    def test_an_interrupt_while_fencing_still_fences_and_shuts_the_child(self):
        ctx, process, daemon = self.published_run()
        service = SimpleNamespace(handle=mock.Mock(return_value=contract.result(ctx.run_id, None, text='ok')))
        starter = adapter.RouteStarter(service, {}, daemon, self.root, exit_grace_s=3.0)
        starter._active[ctx.run_id] = ctx
        real = process.fence

        def boom(reason):
            process.fence = real
            raise KeyboardInterrupt('fence')

        process.fence = boom
        with self.assertRaises(KeyboardInterrupt):
            starter._drive(ctx, {})
        self.assertEqual(process.fenced, ['route_interrupted'])
        daemon.stop.assert_called_once_with(ctx.run_id)
        self.assertIsNone(process._out)
        self.assertIsNone(process._pidfd)
        self.assertFalse(starter.active(ctx.run_id))

    def test_an_interrupt_while_shutting_handles_shuts_them_on_the_retry(self):
        ctx, process, daemon = self.published_run()
        service = SimpleNamespace(handle=mock.Mock(return_value=contract.result(ctx.run_id, None, text='ok')))
        starter = adapter.RouteStarter(service, {}, daemon, self.root, exit_grace_s=3.0)
        starter._active[ctx.run_id] = ctx
        real = process.close
        calls = {'n': 0}

        def boom():
            calls['n'] += 1
            if calls['n'] == 1:
                raise KeyboardInterrupt('shut')
            real()

        process.close = boom
        with self.assertRaises(KeyboardInterrupt):
            starter._drive(ctx, {})
        self.assertGreaterEqual(calls['n'], 2)
        self.assertEqual(process.fenced, ['result_recorded'])
        daemon.stop.assert_called_once_with(ctx.run_id)
        self.assertIsNone(process._out)
        self.assertIsNone(process._pidfd)
        self.assertFalse(starter.active(ctx.run_id))

    # -- a failed start does not label the attempt that takes the id next ---------------------------
    def test_a_failed_worker_start_does_not_label_the_next_attempt(self):
        routes = {'claude_code': {'deadline_s': 1, 'route': {}, 'binary': None}}
        starter = adapter.RouteStarter(mock.Mock(), routes, mock.Mock(), self.root)
        api = ControlAPI(InMemoryRunRegistry(), mock.Mock(), starter, time.time, new_run_id=lambda: 'run-one')
        params = {'tenant': 't', 'engine': 'claude_code', 'prompt': 'p', 'client_key': 'one'}
        gap = threading.Event()
        second_done = threading.Event()
        captured = {}

        class Active(dict):
            def pop(self, key, *args):
                value = super().pop(key, *args)
                if not captured.get('armed'):
                    captured['armed'] = True
                    starter._lock.opened = True
                return value

        class GapLock:
            def __init__(self):
                self._mu = threading.Lock()
                self.opened = False

            def __enter__(self):
                self._mu.acquire()
                return self

            def __exit__(self, exc_type, exc, tb):
                opened = self.opened
                self.opened = False
                self._mu.release()
                if opened:
                    gap.set()
                    captured['waited'] = second_done.wait(3)
                return False

        starter._active = Active()
        starter._lock = GapLock()
        calls = {'n': 0}

        def start(self):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('worker unavailable')
            raise ValueError('next')

        def second():
            try:
                if not gap.wait(3):
                    return
                starter({**params, 'run_id': 'run-one'})
            except adapter.StartRefused as exc:
                captured['detail'] = exc.detail
            finally:
                second_done.set()

        follower = threading.Thread(target=second, daemon=True)
        self.addCleanup(lambda: follower.join(1))
        follower.start()
        with mock.patch.object(threading.Thread, 'start', start):
            first = api.call('run.start', params)
        self.assertTrue(captured.get('waited'), 'the next start did not run in the window')
        self.assertEqual(first['code'], 'start_failed')
        self.assertEqual(captured.get('detail'), 'ValueError')
        self.assertEqual(starter.failures['run-one']['detail'], 'ValueError')
        self.assertNotEqual(starter.failures['run-one']['detail'], 'RuntimeError')

    # -- a planted temp directory, a symlink, and a file in the create window -----------------------
    def test_a_result_temp_directory_is_replaced(self):
        folder = self.run_dir()
        tmp = folder / 'result.json.tmp'
        tmp.mkdir()
        starter = adapter.RouteStarter(None, {}, None, self.root)
        starter._record_result('run-one', {'status': 'ok', 'code': None, 'detail': ''})
        self.assertEqual(json.loads((folder / 'result.json').read_text()),
                         {'status': 'ok', 'code': None, 'detail': ''})
        self.assertFalse(tmp.exists())
        self.assertNotIn('run-one', starter.results)

    def test_a_nonempty_temp_directory_is_left_and_the_result_kept(self):
        folder = self.run_dir()
        tmp = folder / 'result.json.tmp'
        tmp.mkdir()
        (tmp / 'keep').write_text('x')
        starter = adapter.RouteStarter(None, {}, None, self.root)
        starter._record_result('run-one', {'status': 'ok', 'code': None, 'detail': ''})
        self.assertEqual((tmp / 'keep').read_text(), 'x')
        self.assertFalse((folder / 'result.json').exists())
        self.assertEqual(starter.results['run-one']['status'], 'ok')

    def test_a_symlink_result_is_not_read(self):
        folder = self.run_dir()
        secret = self.root / 'secret.json'
        secret.write_text(json.dumps({'status': 'ok', 'code': None, 'detail': 'hidden'}))
        (folder / 'result.json').symlink_to(secret)
        control = object.__new__(adapter.DaemonControl)
        control._state, control.starter = self.root, SimpleNamespace(results={})
        self.assertIsNone(control._result('run-one'))

    def test_a_temp_file_planted_in_the_create_window_is_not_written(self):
        folder = self.run_dir()
        starter = adapter.RouteStarter(None, {}, None, self.root)
        real_open = os.open

        def planting(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).endswith('result.json.tmp') and flags & os.O_EXCL:
                real_open(path, os.O_CREAT | os.O_WRONLY | getattr(os, 'O_CLOEXEC', 0), 0o600)
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch('os.open', side_effect=planting):
            starter._record_result('run-one', {'status': 'ok', 'code': None, 'detail': ''})
        self.assertFalse((folder / 'result.json').exists())
        self.assertEqual(starter.results['run-one']['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
