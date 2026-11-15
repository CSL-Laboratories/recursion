# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The handlers of ``control/methods/session.py`` through SPINE's own dispatcher, on a ``Hooks()`` of the test's
own with the lane installed (``support.Lane``), and the shared ``FakeStore``.

What is tested is the published order of checks (methods.v1, rule 3): the receipt first, then the revision or
turn, then ``busy``; that a returned answer is kept and replayed for its id, and a ``NotAccepted`` one is not;
that two commands with one id at once run the handler once; and that every answer has the shape the published
method table gives it (on an interpreter with ``jsonschema``, through SPINE's conformance client).
"""
from __future__ import annotations

import threading
import unittest

from recursion_core.control import hooks as H
from recursion_core.sessions import store
from recursion_core.sessions.tests.support import BOUND_S, SCHEMAS, Lane, wire_client


class MethodsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = wire_client()
        cls.schemas = None if cls.client is None else cls.client.SchemaSet(SCHEMAS)

    def setUp(self):
        self.lane = Lane()

    def call(self, method, params, principal=H.OWNER):
        """Dispatch, and on an interpreter with jsonschema check the answer against the published answer schema."""
        answer = self.lane.call(method, params, principal)
        if self.client is not None:
            ref = f'methods.v1.json#/$defs/answer.{method}'
            problem = self.client.check_instance(self.schemas, {'file': f'good/{method}', 'schema': ref}, answer)
            self.assertIsNone(problem, f'{method} answered {answer!r}')
        return answer

    def test_the_answers_are_checked_against_the_published_schema_on_this_interpreter(self):
        if self.client is None:
            self.skipTest('jsonschema is not installed in this interpreter: answers are not checked against methods.v1')
        self.assertTrue((SCHEMAS / 'methods.v1.json').is_file())

    def start(self, engine='claude_code', prompt=None, command_id='cmd-start-0001', tenant='t1'):
        params = {'command_id': command_id, 'tenant': tenant, 'engine': engine}
        if prompt is not None:
            params['prompt'] = prompt
        return self.call('session.start', params)

    def turn(self, session, text='next', command_id='cmd-turn-0001', revision=None):
        return self.call('session.turn', {'command_id': command_id, 'session_id': session['session_id'], 'text': text,
                                          'expected_revision': session['revision'] if revision is None else revision})

    def finish(self, session_id, worker_alive=False):
        """The engine takes the turn's input and yields."""
        self.lane.settle()
        self.assertEqual(self.lane.accept_input(session_id), 'delivered')
        status = self.call('session.status', {'session_id': session_id})
        turn_id = status['session']['active_turn_id']
        return self.lane.sessions.turn_finished(session_id, turn_id, outcome='done',
                                                result={'code': None, 'detail': 'ok', 'result_ref': None},
                                                worker_alive=worker_alive)

    def receipts(self):
        return self.lane.store.receipts

    # --------------------------------------------------------------------------- start, and its receipt
    def test_a_start_without_a_prompt_is_an_idle_session_and_is_replayed_for_its_id(self):
        first = self.start()
        self.assertEqual(first['code'], 'ok')
        self.assertEqual((first['session']['state'], first['turn_id'], first['session']['revision']), ('idle', None, 1))
        self.assertEqual(first['session']['principal'], {'kind': 'owner', 'id': 'local'})
        again = self.start()
        self.assertEqual(again, first)
        with self.lane.store.transaction() as tx:
            self.assertEqual(len(store.page(tx)[0]), 1)
        self.assertEqual(list(self.receipts()), [('t1', 'session.start', 'cmd-start-0001')])

    def test_a_start_with_a_prompt_commits_the_turn_its_binding_and_its_work_and_writes_nothing(self):
        answer = self.start(prompt='Read the README.')
        session = answer['session']
        self.assertEqual((session['state'], session['active_turn_id'], answer['turn_id']),
                         ('live', answer['turn_id'], session['turns'][0]['turn_id']))
        self.assertEqual(session['turns'][0]['state'], 'queued')
        self.assertEqual(session['worker'], {'run_id': session['turns'][0]['run_id'], 'alive': False})
        with self.lane.store.transaction() as tx:
            rows = store.deliveries(tx)
            work = store.work(tx)
        self.assertEqual([(row['state'], row['how'], row['written'], row['item_id']) for row in rows],
                         [('delivering', 'new_turn', False, None)])
        self.assertEqual([(item['kind'], item['state']) for item in work], [('launch', 'pending')])
        self.assertEqual(self.lane.units.launched, [], 'a command writes nothing toward an engine')

    def test_the_same_id_with_another_payload_is_a_conflict(self):
        self.start()
        answer = self.call('session.start', {'command_id': 'cmd-start-0001', 'tenant': 't1', 'engine': 'codex'})
        self.assertEqual(answer, {'code': 'conflict', 'detail': 'command_id_reused'})

    def test_a_start_the_slice_does_not_honour_is_refused_and_leaves_no_receipt(self):
        cases = [({'parent_session_id': 'ses-parent'}, 'unsupported'), ({'workspace': {'path': '/x'}}, 'unsupported'),
                 ({'containment_level': 'standard'}, 'unsupported'), ({'engine': 'pi'}, 'unavailable'),
                 ({'budget': {'cap_usd': -1}}, 'request_invalid'), ({'prompt': ''}, 'request_invalid'),
                 ({'colour': 'red'}, 'request_invalid')]
        for number, (change, code) in enumerate(cases):
            with self.subTest(change=change):
                params = {'command_id': f'cmd-refused-{number:04d}', 'tenant': 't1', 'engine': 'claude_code', **change}
                self.assertEqual(self.call('session.start', params)['code'], code)
        self.assertEqual(self.receipts(), {})
        with self.lane.store.transaction() as tx:
            self.assertEqual(store.page(tx)[0], [])

    def test_a_route_refusal_is_the_start_s_answer_and_leaves_nothing(self):
        self.lane.routes.refusal = ('reservation_refused', 'cap_zero')
        answer = self.start()
        self.assertEqual(answer, {'code': 'reservation_refused', 'detail': 'cap_zero'})
        self.assertEqual(self.receipts(), {})

    # ---------------------------------------------------------------------------------- turn
    def test_a_turn_with_a_stale_revision_is_a_conflict_that_is_kept_for_its_id(self):
        session = self.start()['session']
        stale = self.turn(session, revision=session['revision'] + 3)
        self.assertEqual(stale, {'code': 'conflict', 'detail': 'stale_revision', 'current_revision': 1,
                                 'session_id': session['session_id']})
        self.assertEqual(self.turn(session, revision=session['revision'] + 3), stale)
        with self.lane.store.transaction() as tx:
            self.assertEqual(store.load(tx, session['session_id'])['turns'], [])
            self.assertEqual(store.work(tx), [])

    def test_a_turn_while_a_turn_is_live_is_busy_and_leaves_no_receipt(self):
        live = self.start(engine='codex', prompt='first')['session']
        busy = self.turn(live, command_id='cmd-turn-busy-01')
        self.assertEqual((busy['code'], busy['detail']), ('busy', 'live'))
        self.assertNotIn(('t1', 'session.turn', 'cmd-turn-busy-01'), self.receipts())
        idle = self.finish(live['session_id'], worker_alive=True)
        later = self.turn(idle, command_id='cmd-turn-busy-01')
        self.assertEqual(later['code'], 'ok', 'the same id may be sent again: busy kept nothing')

    def test_two_turns_with_one_id_at_once_open_one_turn(self):
        session = self.start()['session']
        barrier = threading.Barrier(2, timeout=BOUND_S)
        answers = []

        def send():
            barrier.wait()
            answers.append(self.turn(session, command_id='cmd-twice-0001'))

        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(BOUND_S)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(answers), 2)
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(answers[0]['code'], 'ok')
        with self.lane.store.transaction() as tx:
            self.assertEqual(len(store.load(tx, session['session_id'])['turns']), 1)
            self.assertEqual(len(store.work(tx)), 1)
            self.assertEqual(len(store.deliveries(tx)), 1)

    def test_two_turns_with_two_ids_at_once_open_one_turn_and_the_other_is_busy(self):
        session = self.start()['session']
        barrier = threading.Barrier(2, timeout=BOUND_S)
        answers = []

        def send(command_id):
            barrier.wait()
            answers.append(self.turn(session, command_id=command_id))

        threads = [threading.Thread(target=send, args=(f'cmd-pair-000{n}',)) for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(BOUND_S)
        codes = sorted(answer['code'] for answer in answers)
        self.assertIn(codes, (['busy', 'ok'], ['conflict', 'ok']))
        with self.lane.store.transaction() as tx:
            self.assertEqual(len(store.load(tx, session['session_id'])['turns']), 1)

    def test_a_second_turn_that_cannot_continue_the_conversation_is_refused_before_anything_is_written(self):
        idle = self.finish(self.start(prompt='first')['session']['session_id'])
        answer = self.turn(idle, command_id='cmd-turn-again-1')
        self.assertEqual((answer['code'], answer['detail']), ('unsupported', 'not_implemented'))
        with self.lane.store.transaction() as tx:
            self.assertEqual(len(store.load(tx, idle['session_id'])['turns']), 1)
            self.assertEqual(len(store.work(tx)), 1)

    def test_a_second_turn_resumes_the_engine_session_or_goes_to_the_live_worker(self):
        resumer = self.start(engine='opencode', prompt='first', command_id='cmd-start-res-1')['session']
        self.lane.sessions.engine_session_reported(resumer['session_id'], 'engine-conversation-1')
        idle = self.finish(resumer['session_id'])
        self.assertEqual(self.turn(idle, command_id='cmd-turn-res-01')['code'], 'ok')
        worker = self.start(engine='codex', prompt='first', command_id='cmd-start-wrk-1')['session']
        waiting = self.finish(worker['session_id'], worker_alive=True)
        self.assertEqual(waiting['worker'], {'run_id': worker['worker']['run_id'], 'alive': True})
        self.assertEqual(self.turn(waiting, command_id='cmd-turn-wrk-01')['code'], 'ok')
        self.lane.settle()
        self.assertEqual([order.resume for order in self.lane.units.launched
                          if order.session_id == resumer['session_id']], [None, 'engine-conversation-1'])
        self.assertEqual([order.resume for order in self.lane.units.launched
                          if order.session_id == worker['session_id']], [None])
        self.assertEqual([(order.run_id, order.how) for order in self.lane.units.sent],
                         [(worker['worker']['run_id'], 'new_turn')])

    # ------------------------------------------------------------------------------- steer and queue
    def test_a_steer_aimed_at_a_turn_that_is_no_longer_the_active_one_is_a_conflict(self):
        live = self.start(prompt='first')['session']
        ended_turn = live['active_turn_id']
        self.finish(live['session_id'])
        answer = self.call('session.steer', {'command_id': 'cmd-steer-old-1', 'session_id': live['session_id'],
                                             'text': 'too late', 'expected_turn_id': ended_turn})
        self.assertEqual(answer, {'code': 'conflict', 'detail': 'stale_turn', 'session_id': live['session_id']})
        with self.lane.store.transaction() as tx:
            self.assertEqual(store.items(tx, live['session_id']), [])

    def test_a_steer_and_a_queued_message_are_queued_in_order(self):
        live = self.start(engine='codex', prompt='first')['session']
        steer = self.call('session.steer', {'command_id': 'cmd-steer-0001', 'session_id': live['session_id'],
                                            'text': 'also check the tests', 'expected_turn_id': live['active_turn_id']})
        queued = self.call('session.queue', {'command_id': 'cmd-queue-0001', 'session_id': live['session_id'],
                                             'text': 'then the changelog'})
        self.assertEqual((steer['item']['order'], steer['item']['delivery'], steer['item']['state']),
                         (1, 'steer', 'queued'))
        self.assertEqual((queued['item']['order'], queued['item']['delivery']), (2, 'next_turn'))
        self.assertEqual([event['type'] for event in self.lane.store.events].count('steer.queued'), 2)

    def test_an_item_can_be_changed_only_while_it_is_queued_and_only_at_its_revision(self):
        live = self.start(engine='codex', prompt='first')['session']
        item = self.call('session.queue', {'command_id': 'cmd-queue-0001', 'session_id': live['session_id'],
                                           'text': 'later'})['item']
        stale = self.call('queue.update', {'command_id': 'cmd-update-0001', 'item_id': item['item_id'],
                                           'expected_revision': 2, 'text': 'x'})
        self.assertEqual(stale, {'code': 'conflict', 'detail': 'stale_revision', 'current_revision': 1,
                                 'item_id': item['item_id']})
        edited = self.call('queue.update', {'command_id': 'cmd-update-0002', 'item_id': item['item_id'],
                                            'expected_revision': 1, 'text': 'later, and the version',
                                            'delivery': 'steer'})['item']
        self.assertEqual((edited['revision'], edited['text'], edited['delivery']),
                         (2, 'later, and the version', 'steer'))
        cancelled = self.call('queue.cancel', {'command_id': 'cmd-cancel-0001', 'item_id': item['item_id'],
                                               'expected_revision': 2})['item']
        self.assertEqual(cancelled['state'], 'cancelled')
        refused = self.call('queue.cancel', {'command_id': 'cmd-cancel-0002', 'item_id': item['item_id'],
                                             'expected_revision': 2})
        self.assertEqual((refused['code'], refused['detail']), ('conflict', 'stale_revision'))
        for bad in ({'text': ''}, {'delivery': 'now'}, {}):
            with self.subTest(bad=bad):
                answer = self.call('queue.update', {'command_id': 'cmd-update-bad1', 'item_id': item['item_id'],
                                                    'expected_revision': 2, **bad})
                self.assertEqual(answer['code'], 'request_invalid')

    def test_an_unknown_target_is_refused_and_leaves_no_receipt(self):
        for method, params in (
                ('session.turn', {'command_id': 'cmd-unknown-01', 'session_id': 'ses-nothere', 'text': 'x',
                                  'expected_revision': 1}),
                ('session.queue', {'command_id': 'cmd-unknown-02', 'session_id': 'ses-nothere', 'text': 'x'}),
                ('session.steer', {'command_id': 'cmd-unknown-03', 'session_id': 'ses-nothere', 'text': 'x',
                                   'expected_turn_id': 'turn-x'}),
                ('queue.cancel', {'command_id': 'cmd-unknown-04', 'item_id': 'qi-nothere', 'expected_revision': 1}),
                ('session.status', {'session_id': 'ses-nothere'})):
            with self.subTest(method=method):
                self.assertIn(self.call(method, params)['code'], ('session_unknown', 'item_unknown'))
        self.assertEqual(self.receipts(), {})

    def test_a_malformed_request_is_request_invalid_and_leaves_no_receipt(self):
        session = self.start()['session']
        for method, params, field in (
                ('session.turn', {'command_id': 'short', 'session_id': session['session_id'], 'text': 'x',
                                  'expected_revision': 1}, 'command_id'),
                ('session.turn', {'command_id': 'cmd-bad-00001', 'session_id': session['session_id'], 'text': 'x',
                                  'expected_revision': 0}, 'expected_revision'),
                ('session.turn', {'command_id': 'cmd-bad-00002', 'session_id': session['session_id'], 'text': 'x'},
                 'expected_revision'),
                ('session.queue', {'command_id': 'cmd-bad-00003', 'session_id': session['session_id'], 'text': 7},
                 'text'),
                ('session.list', {'limit': 0}, 'limit'),
                ('session.list', {'state': 'asleep'}, 'state')):
            with self.subTest(method=method, field=field):
                self.assertEqual(self.call(method, params), {'code': 'request_invalid', 'detail': field})
        self.assertEqual(list(self.receipts()), [('t1', 'session.start', 'cmd-start-0001')])

    # ------------------------------------------------------------------------------- reads
    def test_status_carries_the_session_and_its_queue(self):
        live = self.start(prompt='first')['session']
        self.call('session.queue', {'command_id': 'cmd-queue-0001', 'session_id': live['session_id'], 'text': 'q'})
        status = self.call('session.status', {'session_id': live['session_id']})
        self.assertEqual(status['session'], live)
        self.assertEqual([item['text'] for item in status['queue']], ['q'])

    def test_list_pages_in_id_order_and_filters(self):
        made = [self.start(command_id=f'cmd-start-000{n}', tenant='t1' if n < 3 else 't2')['session']['session_id']
                for n in range(1, 4)]
        first = self.call('session.list', {'limit': 2})
        self.assertEqual([found['session_id'] for found in first['sessions']], sorted(made)[:2])
        self.assertEqual(first['next_after'], sorted(made)[1])
        rest = self.call('session.list', {'limit': 2, 'after': first['next_after']})
        self.assertEqual([found['session_id'] for found in rest['sessions']], sorted(made)[2:])
        self.assertNotIn('next_after', rest)
        self.assertEqual(len(self.call('session.list', {'tenant': 't2'})['sessions']), 1)
        self.assertEqual(len(self.call('session.list', {'state': 'live'})['sessions']), 0)

    def test_the_rows_this_slice_does_not_answer_are_unsupported_not_implemented(self):
        for method in ('session.interrupt', 'session.resume', 'session.fork', 'session.export', 'session.stop'):
            with self.subTest(method=method):
                self.assertEqual(self.lane.call(method, {}), {'code': 'unsupported', 'detail': 'not_implemented'})
        self.assertEqual(self.lane.hooks.methods.registered(),
                         ('session.start', 'session.turn', 'session.steer', 'session.queue', 'session.list',
                          'session.status', 'queue.update', 'queue.cancel'))


if __name__ == '__main__':
    unittest.main()
