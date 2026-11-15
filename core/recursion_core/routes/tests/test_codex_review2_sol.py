"""Codex GPT-6.1 Sol's round-2 red tests for the Codex driver (lane DRIVER-CX keeps them).

Copied unchanged from review-2-402-gpt-6.1-sol-evidence/test_new_cases.py in the lane's folder, the read of draft #402 at 749773c8; they failed there and
pass at the answer commit. Left out: test_launch_environment_never_forwards_home (its change 3, withdrawn by the Liaison: the attended
tool passes the operator's own HOME and CODEX_HOME on purpose, outside the daemon). Fakes only: no real codex, no credential, no network.
"""

"""Synthetic second-read cases. Never launches Codex or opens authentication files."""
import dataclasses
import json
import tempfile
import unittest
from unittest.mock import patch

from recursion_core.routes import contract as c
from recursion_core.routes.driver import DriverStateError
from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import (
    AppServer, Launcher, FakeApprovals, begin, record, message, ask_command,
    done, turn, answers, PROTOCOL, check_params, check_answer,
)
from recursion_core.routes.tests.test_codex_attended import tool


class NewCases(unittest.TestCase):
    def open(self, pending=False):
        self.s = AppServer(silent_turns=pending)
        self.port = FakeApprovals()
        self.d = cx.CodexDriver(Launcher(self.s), FakeClock(), approvals=self.port)
        self.h = begin(self.d, record(), {})
        self.d.send(self.h, message())
        self.rid = self.s.sent[-1]['id']
        while self.s.out:
            self.d.pump(self.h, 0)
        if pending:
            self.feed({'method': 'turn/started', 'params': {'threadId': 'thr-1', 'turn': turn('trn-1')}})

    def feed(self, obj):
        return self.d.parse(self.h, json.dumps(obj))

    def ask(self, rid=77):
        return self.feed(ask_command(rid=rid)('trn-1'))

    def answer(self):
        return self.d.send(self.h, {'kind': 'approval_answer', 'approval_id': 'apr-1', 'decision': 'approve'})

    def test_r1_wrong_turn_item_cannot_supply_nullable_action(self):
        self.open()
        self.feed({'method': 'item/started', 'params': {'threadId': 'thr-1', 'turnId': 'foreign',
            'startedAtMs': 1, 'item': {'type': 'commandExecution', 'id': 'it-1', 'command': 'pwd', 'cwd': '/w',
                                    'commandActions': [], 'status': 'inProgress'}}})
        self.feed(ask_command(command=None)('trn-1'))
        self.assertEqual(self.port.requests, [])
        self.assertEqual(answers(self.s, 77)[0]['result'], {'decision': 'decline'})

    def test_r2_f1_deferred_completion_revokes_existing_approval(self):
        self.open(pending=True)
        self.ask()
        self.assertEqual(len(self.port.requests), 1)
        self.assertEqual(self.feed(done()('trn-1')), [])
        with self.assertRaises(DriverStateError):
            self.answer()
        self.assertEqual(answers(self.s, 77), [])

    def test_r2_f8_approval_after_deferred_completion_is_not_published(self):
        self.open(pending=True)
        self.feed(done()('trn-1'))
        self.ask()
        self.assertEqual(self.port.requests, [])

    def test_r2_f1_start_refusal_revokes_early_approval(self):
        self.open(pending=True)
        self.ask()
        self.feed({'id': self.rid, 'error': {'code': -32000, 'message': 'synthetic'}})
        self.assertIsNone(self.h.live_turn)
        with self.assertRaises(DriverStateError):
            self.answer()
        self.assertEqual(answers(self.s, 77), [])

    def test_r3_close_suppresses_held_interrupt_and_unknown_server_request(self):
        self.open(pending=True)
        self.d.interrupt(self.h, 'turn-1')
        self.d.send(self.h, {'kind': 'close'})
        before = list(self.s.sent)
        self.feed({'id': 90, 'method': 'synthetic/unknown', 'params': {}})
        self.feed({'id': self.rid, 'result': {'turn': turn('trn-1')}})
        self.assertEqual(self.s.sent, before)

    def test_r4_refused_start_discards_deferred_success(self):
        self.open(pending=True)
        self.feed(done()('trn-1'))
        out = self.feed({'id': self.rid, 'error': {'code': -32000, 'message': 'synthetic',
                                                'data': {'codexErrorInfo': 'usageLimitExceeded'}}})
        self.assertEqual([d['type'] for d in out], ['limit', 'result'])
        self.assertEqual(out[-1]['payload']['code'], c.PLAN_LIMIT)

    def test_r5_recursive_checks_cover_server_request_and_approval_answer(self):
        with self.assertRaises(AssertionError):
            check_params('server_requests', 'item/fileChange/requestApproval',
                         {'threadId': 't', 'turnId': 'u', 'itemId': 7, 'startedAtMs': 1})
        with self.assertRaises(AssertionError):
            check_answer('item/commandExecution/requestApproval', {'id': 7, 'result': {'decision': 'yes'}})
        with self.assertRaises(AssertionError):
            check_params('client_requests', 'turn/start', {'threadId': 'thr-1', 'input': [
                {'type': 'text', 'text': 'valid first element'}, {'type': 'text', 'text': 7}]})

    def test_r5_reference_closure_contains_request_id_and_file_update(self):
        self.assertTrue({'RequestId', 'FileUpdateChange', 'AbsolutePathBuf'} <= set(PROTOCOL['definitions']))

    def test_r6_queued_message_does_not_hide_later_interrupt(self):
        module = tool()
        class BudgetServer(AppServer):
            def read_line(self, timeout_s):
                if not self.out:
                    self.empty = getattr(self, 'empty', 0) + 1
                    if self.empty > 8:
                        raise RuntimeError('read budget exhausted')
                return super().read_line(timeout_s)
        s = BudgetServer()
        console = module.Console(lambda _: None)
        d = cx.CodexDriver(Launcher(s), FakeClock(), approvals=console)
        consumed = []
        def commands():
            for line in ('hello', 'next turn message', '/interrupt', '/quit'):
                consumed.append(line)
                yield line
        class ReadySource:
            def __init__(self):
                self.source = commands()
            def __iter__(self):
                return self
            def __next__(self):
                return next(self.source)
            def ready(self):
                return True  # all four commands already typed, like Typed's nonempty queue
        try:
            module.session_loop(d, ReadySource(), lambda _: None, console, read_s=0, env={})
        except RuntimeError as err:
            self.assertEqual(str(err), 'read budget exhausted')
        self.assertIn('/interrupt', consumed)

    def test_r7_systemexit_still_fences_every_worker(self):
        module = tool()
        fenced = []
        class Process:
            def __init__(self, *args, **kw):
                self.proc = type('Child', (), {'pid': 12345})()
            def fence(self, reason):
                fenced.append(reason)
        def interrupted(driver, *a, **kw):
            driver._launcher(['synthetic'], env={})
            raise SystemExit(9)
        with tempfile.TemporaryDirectory() as tmp:
            args = type('Args', (), {'out': tmp, 'workdir': tmp, 'codex': 'synthetic', 'model': None})()
            with patch.object(module, '_run', return_value='synthetic'), patch.object(module, 'LocalProcess', Process), \
                 patch.object(module, 'session_loop', side_effect=interrupted):
                with self.assertRaises(SystemExit):
                    module.run_mode(args)
            self.assertEqual(fenced, ['attended_run_over'])

    def test_r8_typed_tag_object_keeps_resume_auth_code(self):
        s = AppServer(refuse={'thread/resume': {'code': -32000, 'message': 'synthetic',
                                               'data': {'codexErrorInfo': {'unauthorized': {}}}}})
        with self.assertRaises(c.RouteError) as caught:
            begin(cx.CodexDriver(Launcher(s), FakeClock()), record('thr-1'), {})
        self.assertEqual(caught.exception.code, c.AUTH_FAILED)

    def test_f12_frozen_declaration_rejects_normal_mutation(self):
        a = cx.CodexDriver(Launcher(), FakeClock())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.declaration.flags.multi_turn = False
        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.declaration.input_ack = False

    def test_f2_capture_cleanup_runs_when_close_itself_raises(self):
        import io
        module = tool()
        p = module.LocalProcess.__new__(module.LocalProcess)
        p._stdin, p._stderr = io.StringIO(), io.BytesIO()
        with patch.object(p, 'close_stdin', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                p.fence('test')
        self.assertTrue(p._stdin.closed and p._stderr.closed)

    def test_f3_nan_input_keeps_one_call_and_result(self):
        self.open()
        item = {'id': 'nan-call', 'type': 'dynamicToolCall', 'tool': 'synthetic',
                'arguments': {'n': float('nan')}, 'status': 'inProgress'}
        first = self.feed({'method': 'item/started', 'params': {'threadId': 'thr-1', 'turnId': 'trn-1', 'item': item}})
        last = self.feed({'method': 'item/completed', 'params': {'threadId': 'thr-1', 'turnId': 'trn-1',
                                                             'item': {**item, 'status': 'completed'}}})
        self.assertEqual([d['type'] for d in first + last], ['tool.call', 'tool.result'])
        self.assertIsNone(first[0]['payload']['input'])

    def test_f4_repeated_start_before_completion_gets_one_call(self):
        self.open()
        item = {'id': 'dup', 'type': 'commandExecution', 'command': 'pwd', 'cwd': '/w', 'status': 'inProgress'}
        note = {'method': 'item/started', 'params': {'threadId': 'thr-1', 'turnId': 'trn-1', 'item': item}}
        self.assertEqual([d['type'] for d in self.feed(note) + self.feed(note)], ['tool.call'])

    def test_f5_protocol_error_ends_loop_after_prior_drafts(self):
        from recursion_core.routes.tests.test_codex_attended import LateTyping
        from recursion_core.routes.tests.test_codex_driver import Raw
        module = tool()
        s = AppServer(script={1: [lambda _: Raw('not-json')]})
        said = []
        console = module.Console(said.append)
        d = cx.CodexDriver(Launcher(s), FakeClock(), approvals=console)
        recorded = []
        module.session_loop(d, LateTyping(s, ['hello', '/quit']), said.append, console,
                            env={}, read_s=0, on_draft=recorded.append)
        self.assertEqual(recorded[0]['type'], 'input.accepted')
        self.assertTrue(any('the worker\'s output failed' in text for text in said))

    def test_f6_failed_approval_pipe_is_typed_without_error_text(self):
        self.open()
        self.ask()
        with patch.object(self.s, 'send', side_effect=OSError('SYNTHETIC-SECRET-MARKER')):
            with self.assertRaises(c.RouteError) as caught:
                self.answer()
        self.assertEqual(caught.exception.code, c.ENGINE_ERROR)
        self.assertNotIn('SYNTHETIC-SECRET-MARKER', str(caught.exception))

    def test_f7_string_and_integer_request_ids_are_distinct_and_once_only(self):
        self.open()
        self.ask(77)
        self.ask('77')
        self.assertEqual(len(self.port.requests), 2)
        self.answer()
        self.ask(77)
        self.assertEqual(answers(self.s, 77), [{'id': 77, 'result': {'decision': 'accept'}}])
        self.assertEqual(len(self.port.requests), 2)

    def test_f9_runtimeerror_port_declines_once(self):
        self.open()
        with patch.object(self.port, 'request', side_effect=RuntimeError('synthetic')):
            self.ask()
        self.assertEqual(answers(self.s, 77), [{'id': 77, 'result': {'decision': 'decline'}}])
        self.assertEqual(self.h._codex.bindings, {})

    def test_f10_nonobject_turn_in_start_answer_ends_live_turn(self):
        self.open(pending=True)
        drafts = self.feed({'id': self.rid, 'result': {'turn': []}})
        self.assertEqual(drafts[0]['payload']['code'], c.PROTOCOL_ERROR)
        self.assertIsNone(self.h.live_turn)

    def test_f11_carriage_return_call_id_is_rejected(self):
        self.open()
        self.assertEqual(self.feed({'method': 'item/started', 'params': {'threadId': 'thr-1',
            'turnId': 'trn-1', 'item': {'id': 'bad\rid', 'type': 'commandExecution', 'command': 'pwd'}}}), [])

    def test_f13_interrupted_turn_drops_uncompleted_calls(self):
        self.open()
        note = {'method': 'item/started', 'params': {'threadId': 'thr-1', 'turnId': 'trn-1',
            'startedAtMs': 1, 'item': {'id': 'unfinished', 'type': 'commandExecution', 'command': 'pwd', 'cwd': '/w',
                                      'commandActions': [], 'status': 'inProgress'}}}
        check_params('server_notifications', 'item/started', note['params'])
        self.feed(note)
        self.feed(done(status='interrupted')('trn-1'))
        self.assertEqual(self.h._codex.calls, set())

    def test_f14_refused_foreign_close_cannot_close_owner_handle(self):
        self.open()
        other = cx.CodexDriver(Launcher(), FakeClock(), approvals=FakeApprovals())
        with self.assertRaises(DriverStateError):
            other.send(self.h, {'kind': 'close'})
        self.assertFalse(self.h.closed)
        self.d.interrupt(self.h, 'turn-1')

    def test_f15_docstring_matches_single_drafts_capture(self):
        module = tool()
        self.assertIn('``drafts.jsonl``', module.__doc__)
        self.assertNotIn('worker-N.drafts.jsonl', module.__doc__)

    def test_f16_empty_limit_id_has_documented_null_bucket(self):
        self.open()
        draft = self.d._rate_limits(self.h._codex, {'limitId': '', 'secondary': {'usedPercent': 100}})[0]
        self.assertIsNone(draft['payload']['bucket'])
        self.assertIn('null otherwise', cx.__doc__)

    def test_launch_synthetic_credential_is_only_in_explicit_child_env(self):
        import contextlib
        import io
        marker = 'SYNTHETIC-REVIEW-CREDENTIAL-NOT-REAL'
        s = AppServer(script={1: [done()]})
        launcher = Launcher(s)
        d = cx.CodexDriver(launcher, FakeClock())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            h = begin(d, record(), {'CODEX_API_KEY': marker})
            d.send(h, message())
            drafts = []
            while s.out:
                drafts += d.pump(h, 0) or []
        self.assertEqual(launcher.calls[0]['env']['CODEX_API_KEY'], marker)
        self.assertNotIn(marker, json.dumps(s.sent) + json.dumps(drafts) + output.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
