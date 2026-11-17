"""Codex GPT-6 Astra's round-3 red tests for the Codex driver (lane DRIVER-CX keeps them).

Copied from review-3-402-gpt-6-astra-evidence/test_confirming.py and test_coverage.py in the lane's folder, the
read of draft #402 at 4b84bc47; the tests of its five required changes failed there and pass at the answer commit.
What differs from the read's files: test_A3_S2_stop_does_not_wait_for_output_to_become_quiet gave the loop 0.5 s
of wall clock, which the lane's rules do not allow in a test, so here its stand-in counts the lines it writes and
fails past 200 (the assertions are the read's); test_S3_daemon_policy_uses_designated_home_and_fixed_names and
test_S3_production_launcher_uses_its_state_home_and_omits_codex_home are left out (they test child_env and the
daemon's launcher, other lanes' code; their results are quoted in the lane's report), and with them the import of
pathlib; the unused import of FakeApprovals is dropped; test_coverage.py's class is Coverage at the end (``import
sys`` added for it; its import of test_confirming dropped, the class it calls is in this file). Fakes only: no real
codex, no credential, no network.
"""

"""Local synthetic regression checks; no engine, network, auth files, or tool execution."""
import argparse
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from recursion_core.routes import contract as c
from recursion_core.routes.driver import DriverStateError
from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import AppServer, Launcher, begin, record, message, ask_command, done, turn, check_answer
from recursion_core.routes.tests.test_codex_attended import tool, LateTyping


class Confirming(unittest.TestCase):
    def opened(self, pending=False, port=None):
        s = AppServer(silent_turns=pending)
        d = cx.CodexDriver(Launcher(s), FakeClock(), approvals=port)
        h = begin(d, record(), {})
        d.send(h, message())
        rid = s.sent[-1]['id']
        while s.out:
            d.pump(h, 0)
        if pending:
            self.feed(d, h, {'method': 'turn/started', 'params': {'threadId': 'thr-1', 'turn': turn('trn-1')}})
        return d, h, s, rid

    def feed(self, d, h, note):
        return d.parse(h, json.dumps(note))

    def test_A1_S1_deferred_end_then_new_input_does_not_start_second_engine_turn(self):
        d, h, s, rid = self.opened(pending=True)
        self.feed(d, h, done()('trn-1'))
        try:
            d.send(h, message(delivery='dlv-2'))
        except DriverStateError:
            pass  # refusal is valid while the original acknowledgement is outstanding
        starts = [m for m in s.sent if m.get('method') == 'turn/start']
        self.assertEqual(len(starts), 1, 'an ended turn awaiting acknowledgement must not start again')
        drafts = self.feed(d, h, {'id': rid, 'result': {'turn': turn('trn-1')}})
        self.assertEqual([x['type'] for x in drafts], ['input.accepted', 'usage', 'result'])

    def test_A1_S1_repeated_started_after_retirement_does_not_readopt_turn(self):
        d, h, s, rid = self.opened(pending=True)
        self.feed(d, h, done('interrupted')('trn-1'))
        self.feed(d, h, {'method': 'turn/started', 'params': {'threadId': 'thr-1', 'turn': turn('trn-1')}})
        self.assertEqual(h._codex.core_turn, {})
        self.assertEqual(h._codex.engine_turn, {})

    def test_A2_close_completes_after_failing_inflight_port(self):
        reached, release = threading.Event(), threading.Event()
        errors = []
        class Port:
            def request(self, *a):
                reached.set()
                release.wait(2)
                raise RuntimeError('synthetic port failure')
        d, h, s, _ = self.opened(port=Port())
        def reader():
            try:
                self.feed(d, h, ask_command()('trn-1'))
            except BaseException as err:
                errors.append(type(err).__name__)
        reading = threading.Thread(target=reader)
        reading.start()
        self.assertTrue(reached.wait(2))
        closing = threading.Thread(target=d.send, args=(h, {'kind': 'close'}))
        closing.start()
        try:
            closing.join(.05)
            self.assertTrue(closing.is_alive())
        finally:
            release.set()
            reading.join(2)
            closing.join(2)
        self.assertFalse(reading.is_alive() or closing.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(s.stdin_closed)

    def test_A3_S2_held_resume_with_pending_approval_still_reads_quit(self):
        module = tool()
        s = AppServer(script={1: [ask_command()]})
        console = module.Console(lambda _: None)
        d = cx.CodexDriver(Launcher(s), FakeClock(), approvals=console)
        calls = []
        original = module.takes_now
        def bounded(line, waiting):
            calls.append(line)
            self.assertLess(len(calls), 20, 'held /resume spins and hides /quit while approval waits')
            return original(line, waiting)
        with patch.object(module, 'takes_now', side_effect=bounded):
            module.session_loop(d, LateTyping(s, ['hello', '/resume', '/quit']), lambda _: None,
                                console, env={}, read_s=0)
        self.assertTrue(s.stdin_closed)

    def test_A3_S2_multiple_waiting_messages_preserve_order(self):
        module = tool()
        s = AppServer(script={1: [done()], 2: [done()], 3: [done()]})
        console = module.Console(lambda _: None)
        d = cx.CodexDriver(Launcher(s), FakeClock(), approvals=console)
        seen = module.session_loop(d, iter(['one', 'two', 'three']), lambda _: None, console, env={}, read_s=0)
        self.assertEqual([x['params']['input'][0]['text'] for x in s.sent if x.get('method') == 'turn/start'], ['one', 'two', 'three'])
        self.assertEqual(len([x for x in seen['drafts'] if x['type'] == 'result']), 3)

    def test_A3_S2_stop_does_not_wait_for_output_to_become_quiet(self):
        module = tool()
        errors = []
        class StreamingServer(AppServer):
            emitted = 0
            def read_line(inner, timeout_s):
                if inner.turns and not inner.out and not inner.stdin_closed:
                    inner.emitted += 1
                    if inner.emitted > 200:  # (lane DRIVER-CX: a count in place of the read's 0.5 s)
                        raise AssertionError('/stop waited for the producer to stop writing')
                    inner.emit({'method': 'item/agentMessage/delta', 'params': {'threadId': 'thr-1', 'turnId': 'trn-1', 'itemId': 'message-1', 'delta': 'inert text'}})
                return super().read_line(timeout_s)
        s = StreamingServer()
        console = module.Console(lambda _: None)
        d = cx.CodexDriver(Launcher(s), FakeClock(), approvals=console)
        try:
            module.session_loop(d, iter(['hello', '/stop', '/quit']), lambda _: None, console, env={}, read_s=0)
        except BaseException as err:
            errors.append(type(err).__name__)
        self.assertEqual(errors, [])
        self.assertTrue(s.stdin_closed)

    def test_A4_main_exception_survives_cleanup_exceptions_and_all_workers_attempted(self):
        module = tool()
        attempted = []
        class Process:
            def __init__(self, *a, **kw):
                self.proc = type('Child', (), {'pid': 12345})()
            def fence(self, why):
                attempted.append(why)
                raise SystemExit(6)
        def loop(driver, *a, **kw):
            driver._launcher(['inert'], env={})
            driver._launcher(['inert'], env={})
            raise ValueError('main operation')
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(out=folder, workdir=folder, codex='inert', model=None)
            with patch.object(module, '_run', return_value='inert'), patch.object(module, 'LocalProcess', Process), patch.object(module, 'session_loop', side_effect=loop), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'main operation'):
                    module.run_mode(args)
        self.assertEqual(attempted, ['attended_run_over'] * 2)

    def test_A5_tagged_internal_resume_error_keeps_engine_error(self):
        s = AppServer(refuse={'thread/resume': {'code': -32000, 'message': 'inert', 'data': {'codexErrorInfo': {'internalServerError': {}}}}})
        with self.assertRaises(c.RouteError) as caught:
            begin(cx.CodexDriver(Launcher(s), FakeClock()), record('thr-1'), {})
        self.assertEqual(caught.exception.code, c.ENGINE_ERROR)
        self.assertTrue(s.stdin_closed)

    def test_A4_cleanup_diagnostic_failure_still_attempts_remaining_workers(self):
        module = tool()
        attempted = []
        class Process:
            def __init__(self, *a, **kw):
                self.proc = type('Child', (), {'pid': 12345})()
            def fence(self, why):
                attempted.append(why)
                if len(attempted) == 1:
                    raise OSError('inert first worker cleanup failure')
        def loop(driver, *a, **kw):
            driver._launcher(['inert'], env={})
            driver._launcher(['inert'], env={})
        def reporting(*args, **kw):
            if args and 'could not be ended' in str(args[0]):
                raise BrokenPipeError('inert closed console output')
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(out=folder, workdir=folder, codex='inert', model=None)
            with patch.object(module, '_run', return_value='inert'), patch.object(module, 'LocalProcess', Process), patch.object(module, 'session_loop', side_effect=loop), patch('builtins.print', side_effect=reporting):
                with self.assertRaises(OSError):
                    module.run_mode(args)
        self.assertEqual(attempted, ['attended_run_over'] * 2, 'a failed diagnostic must not skip another worker')

    def test_A5_jsonrpc_internal_error_is_not_missing_thread(self):
        error = {'code': -32603, 'message': 'Internal error'}
        check_answer('thread/resume', {'id': 1, 'error': error})
        s = AppServer(refuse={'thread/resume': error})
        with self.assertRaises(c.RouteError) as caught:
            begin(cx.CodexDriver(Launcher(s), FakeClock()), record('thr-1'), {})
        self.assertEqual(caught.exception.code, c.ENGINE_ERROR)

    def test_A6_surrogate_object_key_has_encodable_digest_and_result(self):
        d, h, s, _ = self.opened()
        value = {'\udfff': ['\ud800', float('nan')]}
        item = {'type': 'mcpToolCall', 'id': 'call-unicode', 'server': 'docs', 'tool': 'lookup', 'status': 'completed', 'arguments': value, 'result': None}
        drafts = d._tool_call(h._codex, item) + d._tool_result(h._codex, item)
        self.assertEqual([x['type'] for x in drafts], ['tool.call', 'tool.result'])
        self.assertIsNone(drafts[0]['payload']['input'])
        expected = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(drafts[0]['payload']['input_digest'], expected)

    def test_S3_attended_fixed_names_and_case_variants_only(self):
        module = tool()
        parent = {name: 'inert' for name in module.CHILD_ENV_NAMES}
        parent.update({name.lower(): 'inert' for name in module.CHILD_ENV_NAMES})
        parent.update({'OPENAI_API_KEY': 'inert', 'CODEX_API_KEY': 'inert', 'openai_api_key': 'inert', 'OTHER_TOKEN': 'inert', 'EXTRA_NAMES': 'INERT_KEY'})
        result = module.child_env(parent)
        self.assertEqual(set(result), {k for k in parent if k.upper() in module.CHILD_ENV_NAMES})
        self.assertFalse(any('KEY' in k.upper() or 'TOKEN' in k.upper() for k in result))

    def test_S3_driver_forwards_exact_designated_environment(self):
        s = AppServer()
        launcher = Launcher(s)
        env = {'HOME': '/synthetic/designated', 'CODEX_HOME': '/synthetic/login', 'INERT_MARKER': 'not-an-allowlisted-name'}
        begin(cx.CodexDriver(launcher, FakeClock()), record(), env)
        self.assertEqual(launcher.calls[0]['env'], env)
        self.assertFalse(launcher.calls[0]['shell'])

    def test_S4_unfinished_call_identity_can_be_reused_in_next_turn(self):
        d, h, s, _ = self.opened()
        item = {'type': 'commandExecution', 'id': 'same-call', 'command': 'pwd', 'cwd': '/w', 'status': 'inProgress'}
        self.assertEqual(len(d._tool_call(h._codex, item)), 1)
        self.feed(d, h, done('interrupted')('trn-1'))
        d.send(h, {**message(delivery='dlv-2'), 'turn_id': 'turn-2'})
        while s.out:
            d.pump(h, 0)
        self.assertEqual(len(d._tool_call(h._codex, item)), 1)

    def test_S5_foreign_send_does_not_reserve_delivery(self):
        s = AppServer()
        d = cx.CodexDriver(Launcher(s), FakeClock())
        h = begin(d, record(), {})
        other = cx.CodexDriver(Launcher(), FakeClock())
        with self.assertRaises(DriverStateError):
            other.send(h, message())
        self.assertIsNone(h.live_turn)
        d.send(h, message())
        drafts = d.pump(h, 0)
        self.assertEqual([x['type'] for x in drafts], ['input.accepted'])

    def test_S6_capture_callback_contains_both_turn_results(self):
        module = tool()
        s = AppServer(script={1: [done()], 2: [done('interrupted')]})
        console = module.Console(lambda _: None)
        d = cx.CodexDriver(Launcher(s), FakeClock(), approvals=console)
        captured = []
        seen = module.session_loop(d, LateTyping(s, ['one', 'two', '/quit']), lambda _: None, console, env={}, read_s=0, on_draft=captured.append)
        self.assertEqual(captured, seen['drafts'])
        self.assertEqual([x['payload']['code'] for x in captured if x['type'] == 'result'], [None, 'interrupted'])


class Coverage(unittest.TestCase):
    def test_retirement_discards_flushing_state_immediately(self):
        session = cx._Session(AppServer(), 'subscription:inert')
        session.flushing.add('turn-1')
        driver = cx.CodexDriver(Launcher(), FakeClock())
        driver._retire_turn(session, 'turn-1', None)
        self.assertEqual(session.flushing, set())

    def test_missing_canonicalizer_is_not_silent_fallback(self):
        driver = cx.CodexDriver(Launcher(), FakeClock())
        session = cx._Session(AppServer(), 'subscription:inert')
        item = {'type': 'commandExecution', 'id': 'call-1', 'command': 'pwd', 'cwd': '/w'}
        with patch.dict(sys.modules, {'recursion_core.control.methods': None}):
            with self.assertRaises(ImportError):
                driver._tool_call(session, item)

    def test_held_fifo_order(self):
        Confirming.test_A3_S2_multiple_waiting_messages_preserve_order(self)


if __name__ == '__main__':
    unittest.main(verbosity=2)
