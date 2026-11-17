"""Route logs, run.list and the routes file at the control-adapter boundary.

A route failure and a failed result write must not log an untrusted message or a private
path. A persisted result whose detail is a lone surrogate must not make run.list raise,
and that answer must encode as a control frame. A non-finite detail must encode too,
on run.list and on run.status. A routes file with a repeated key is refused. Plugin
install is not covered here.
"""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from recursion_core.control_transport.framing import encode_frame
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import launcher

LINUX = sys.platform.startswith('linux')


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class AdapterLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='adapter-logs-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_route_exception_log_omits_untrusted_message(self):
        marker = 'SYNTHETIC-PRIVATE-DIAGNOSTIC'
        service = mock.Mock()
        service.handle.side_effect = RuntimeError(marker)
        starter = adapter.RouteStarter(service, {}, SimpleNamespace(closed=False), self.root)
        ctx = launcher.RunContext('run-one')
        with self.assertLogs(adapter.log, 'ERROR') as logs:
            starter._drive(ctx, {})
        self.assertFalse(any(marker in line for line in logs.output))
        self.assertTrue(all(not record.exc_info for record in logs.records))
        self.assertIn('RuntimeError', '\n'.join(record.getMessage() for record in logs.records))
        self.assertEqual(ctx.result['detail'], 'route failure: RuntimeError')
        self.assertNotIn(marker, ctx.result['detail'])

    def test_result_write_log_omits_private_path(self):
        starter = adapter.RouteStarter(mock.Mock(), {}, SimpleNamespace(closed=False), self.root)
        result = {'code': 'engine_error'}
        with self.assertLogs(adapter.log, 'ERROR') as logs:
            starter._record_result('run-one', result)
        self.assertFalse(any(str(self.root) in line for line in logs.output))
        self.assertTrue(all(not record.exc_info for record in logs.records))
        self.assertIn('FileNotFoundError', '\n'.join(record.getMessage() for record in logs.records))
        self.assertEqual(starter.results['run-one'], result)

    def test_settle_log_names_the_exception_type_only(self):
        marker = 'SYNTHETIC-PRIVATE-DIAGNOSTIC'
        path_marker = str(self.root)
        daemon = mock.Mock()
        daemon.closed = False
        daemon.reconcile.side_effect = RuntimeError(f'{marker} {path_marker}')
        starter = adapter.RouteStarter(mock.Mock(), {}, daemon, self.root)
        ctx = launcher.RunContext('run-one')
        ctx.process = mock.Mock()
        with self.assertLogs(adapter.log, 'ERROR') as logs:
            starter._settle(ctx, grace_s=1.0)
        text = '\n'.join(logs.output)
        self.assertNotIn(marker, text)
        self.assertNotIn(path_marker, text)
        self.assertTrue(all(not record.exc_info for record in logs.records))
        self.assertIn('RuntimeError', '\n'.join(record.getMessage() for record in logs.records))
        ctx.process.fence.assert_called_once_with('result_recorded')
        ctx.process.close.assert_called_once()

    def test_registry_read_log_names_the_exception_type_only(self):
        marker = 'SYNTHETIC-PRIVATE-DIAGNOSTIC'
        path_marker = str(self.root)
        daemon = SimpleNamespace(registry=mock.Mock())
        daemon.registry.get.side_effect = RuntimeError(f'{marker} {path_marker}')
        control = adapter.DaemonControl(
            mock.Mock(), None, daemon, SimpleNamespace(results={}), self.root)
        with self.assertLogs(adapter.log, 'ERROR') as logs:
            row = control._row('run-one')
        self.assertIs(row, False)
        text = '\n'.join(logs.output)
        self.assertNotIn(marker, text)
        self.assertNotIn(path_marker, text)
        self.assertTrue(all(not record.exc_info for record in logs.records))
        self.assertIn('RuntimeError', '\n'.join(record.getMessage() for record in logs.records))

    def test_wire_size_of_a_lone_surrogate_matches_surrogatepass_not_backslashreplace(self):
        lone = {'detail': '\ud800'}
        dumped = json.dumps(lone, separators=(',', ':'), ensure_ascii=False, default=str)
        counted = adapter._wire_size(lone)
        self.assertEqual(counted, len(dumped.encode('utf-8', 'surrogatepass')))
        self.assertNotEqual(counted, len(dumped.encode('utf-8', 'backslashreplace')))

    def test_result_with_non_scalar_unicode_does_not_break_list(self):
        run_dir = self.root / 'runs' / 'run-one'
        run_dir.mkdir(parents=True)
        # The result writer uses json.dumps' default (ensure_ascii=True), so the file is legal text
        # and json.loads brings the lone surrogate back.
        (run_dir / 'result.json').write_text(
            json.dumps({'status': 'error', 'code': 'engine_error', 'detail': chr(0xD800)}),
            encoding='utf-8')
        api = mock.Mock()
        api.call.side_effect = lambda *_args, **_kwargs: {
            'code': 'ok', 'runs': [{'run_id': 'run-one', 'state': 'running'}]}
        daemon = SimpleNamespace(registry=mock.Mock())
        daemon.registry.get.side_effect = KeyError('run-one')
        control = adapter.DaemonControl(api, None, daemon, SimpleNamespace(results={}), self.root)
        answer = control.call('run.list')
        self.assertIn('code', answer)
        # The adapter used to return this answer, and the frame encode then raised. The client
        # got no reply. The detail is one lone surrogate in the file, so the frame carries U+FFFD.
        framed = encode_frame({'ok': True, 'result': answer})
        self.assertIsInstance(framed, bytes)
        self.assertEqual(answer['runs'][0]['result']['detail'], '\ufffd')
        ordinary = {'text': 'caf\u00e9' + chr(1)}
        dumped = json.dumps(ordinary, separators=(',', ':'), ensure_ascii=False, default=str)
        self.assertEqual(adapter._wire_size(ordinary), len(dumped.encode('utf-8')))

    def test_routes_boundary_refuses_duplicate_members(self):
        path = self.root / 'routes.json'
        path.write_text('{"routes":{"codex":{"deadline_s":1,"deadline_s":2}}}')
        os.chmod(path, 0o600)
        with self.assertRaises(adapter.RoutesUnreadable) as caught:
            adapter.load_routes(path)
        self.assertNotIn(str(self.root), str(caught.exception))
        plain = self.root / 'plain.json'
        plain.write_text('{"routes":{"codex":{"deadline_s":1}}}')
        os.chmod(plain, 0o600)
        loaded = adapter.load_routes(plain)
        self.assertEqual(loaded['codex']['deadline_s'], 1.0)
        self.assertIsInstance(loaded['codex']['deadline_s'], float)

    def _control(self, payload, *, in_memory=False):
        if in_memory:
            starter = SimpleNamespace(results={'run-one': payload})
        else:
            run_dir = self.root / 'runs' / 'run-one'
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / 'result.json').write_text(json.dumps(payload), encoding='utf-8')
            starter = SimpleNamespace(results={})
        api = mock.Mock()

        def call(method, _params=None, **_kwargs):
            row = {'run_id': 'run-one', 'state': 'running'}
            if method == 'run.list':
                return {'code': 'ok', 'runs': [dict(row)]}
            if method == 'run.status':
                return {'code': 'ok', 'run': dict(row)}
            raise AssertionError(method)

        api.call.side_effect = call
        daemon = SimpleNamespace(registry=mock.Mock())
        daemon.registry.get.side_effect = KeyError('run-one')
        return adapter.DaemonControl(api, None, daemon, starter, self.root)

    def _framed(self, answer):
        framed = encode_frame({'ok': True, 'result': answer})
        self.assertIsInstance(framed, bytes)
        json.loads(framed[4:].decode('utf-8'))
        return framed

    def test_list_and_status_frame_a_surrogate_inside_detail(self):
        payload = {'status': 'error', 'code': 'engine_error', 'detail': 'seen-' + chr(0xD800) + '-tail'}
        control = self._control(payload)
        listed = control.call('run.list')
        self.assertEqual(listed['code'], 'ok')
        self._framed(listed)
        self.assertEqual(listed['runs'][0]['result']['detail'], 'seen-\ufffd-tail')
        status = control.call('run.status')
        self.assertEqual(status['code'], 'ok')
        self._framed(status)
        self.assertEqual(status['run']['result']['detail'], 'seen-\ufffd-tail')
        self.assertEqual(status['run']['result']['code'], 'engine_error')

    def test_list_and_status_frame_a_non_finite_detail(self):
        for name, number in (('nan', float('nan')), ('inf', float('inf')), ('-inf', float('-inf'))):
            with self.subTest(number=name):
                payload = {'status': 'error', 'code': 'engine_error', 'detail': number}
                control = self._control(payload)
                listed = control.call('run.list')
                self.assertEqual(listed['code'], 'ok')
                self._framed(listed)
                self.assertIsNone(listed['runs'][0]['result']['detail'])
                status = control.call('run.status')
                self.assertEqual(status['code'], 'ok')
                self._framed(status)
                self.assertIsNone(status['run']['result']['detail'])

    def test_a_stored_result_is_left_unchanged_and_the_answer_still_frames(self):
        stored = {
            'status': 'error',
            'code': 'engine_error',
            'detail': {'note': 'a' + chr(0xD800), 'n': float('nan'), 'ok': 'caf\u00e9'},
            'events': [float('inf'), 'ok' + chr(0xD800)],
        }
        snapshot = json.dumps(stored)
        control = self._control(stored, in_memory=True)
        listed = control.call('run.list')
        self.assertEqual(listed['code'], 'ok')
        self._framed(listed)
        detail = listed['runs'][0]['result']['detail']
        self.assertEqual(detail['note'], 'a\ufffd')
        self.assertIsNone(detail['n'])
        self.assertEqual(detail['ok'], 'caf\u00e9')
        status = control.call('run.status')
        self._framed(status)
        shown = status['run']['result']
        self.assertEqual(shown['detail']['note'], 'a\ufffd')
        self.assertEqual(shown['events'], [None, 'ok\ufffd'])
        self.assertEqual(json.dumps(stored), snapshot)

    def test_two_callers_frame_one_stored_result(self):
        stored = {'status': 'error', 'code': 'engine_error', 'detail': 'm' + chr(0xD800)}
        snapshot = json.dumps(stored)
        first = self._control(stored, in_memory=True)
        second = self._control(
            {'status': 'error', 'code': 'engine_error', 'detail': 'other'}, in_memory=True)
        second.starter = first.starter
        box = queue.Queue()

        def read(control):
            try:
                answer = control.call('run.list')
                encode_frame({'ok': True, 'result': answer})
                box.put(answer['runs'][0]['result']['detail'])
            except Exception as exc:  # noqa: BLE001 - the caller thread reports the error below
                box.put(exc)

        threads = [threading.Thread(target=read, args=(control,)) for control in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        got = [box.get_nowait() for _ in threads]
        self.assertEqual(got, ['m\ufffd', 'm\ufffd'])
        self.assertEqual(json.dumps(stored), snapshot)


if __name__ == '__main__':
    unittest.main()
