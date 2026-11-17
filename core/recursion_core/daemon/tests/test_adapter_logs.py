"""Route logs, run.list and the routes file at the control-adapter boundary.

A route failure and a failed result write must not log an untrusted message or a private
path. A persisted result whose detail is a lone surrogate must not make run.list raise.
A routes file with a repeated key is refused. Plugin install and control frames are not
covered here.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

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


if __name__ == '__main__':
    unittest.main()
