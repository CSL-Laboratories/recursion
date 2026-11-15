# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The work intents, the service and the restart: every crash cell the lane's start note names.

    a crash before the intent commit, and after it;
    before the write, and after it;
    before the engine's typed acceptance, and after it;
    two commands with one id at once (test_methods);
    input for a turn that is no longer the active one;
    a restart with a Stop pending.

A CRASH is ``Cut`` raised where the process would end (support.py); a RESTART is a new lane over the same
``FakeStore`` and ``recover()``. What these show is the lane's logic over the shared fake. They do NOT show
durability across a real process death: that needs lane EVENTS' real store, which this slice does not use.
"""
from __future__ import annotations

import threading
import unittest

from recursion_core.sessions import machine, store
from recursion_core.sessions.tests.support import BOUND_S, Cut, Lane, UnitRefused, stop_answer


def only_delivery(lane, session_id):
    with lane.store.transaction() as tx:
        rows = store.deliveries(tx, session_id=session_id)
    assert len(rows) == 1, rows
    return rows[0]


def work_states(lane):
    with lane.store.transaction() as tx:
        return [(operation['kind'], operation['state']) for operation in store.work(tx)]


def session_of(lane, session_id):
    with lane.store.transaction() as tx:
        return store.load(tx, session_id)


def acceptance(row):
    return {'delivery_id': row['delivery_id'], 'session_id': row['session_id'], 'run_id': row['run_id'],
            'turn_id': row['target_turn_id'], 'how': row['how']}


class CrashCellTest(unittest.TestCase):

    def setUp(self):
        self.lane = Lane()

    def started(self, engine='claude_code', prompt='Read the README.'):
        answer = self.lane.start(engine=engine, prompt=prompt)
        self.assertEqual(answer['code'], 'ok')
        return answer['session']

    # ------------------------------------------------------------------------------- the intent commit
    def test_a_crash_before_the_intent_commit_leaves_nothing(self):
        real = self.lane.minter

        def mint(prefix):
            if prefix == 'op-':
                raise Cut()          # the process ends just before the work intent would be written
            return real(prefix)

        self.lane.sessions.mint = mint
        with self.assertRaises(Cut):
            self.lane.start(prompt='Read the README.')
        after = self.lane.restarted()
        after.settle()
        with after.store.transaction() as tx:
            self.assertEqual(store.page(tx)[0], [])
            self.assertEqual(store.work(tx), [])
            self.assertEqual(store.deliveries(tx), [])
        self.assertEqual(after.store.receipts, {})
        self.assertEqual(after.store.events, [])
        self.assertEqual((self.lane.units.launched, self.lane.units.stops), ([], []))

    def test_a_crash_after_the_intent_commit_writes_nothing_and_the_turn_is_shown_as_unknown(self):
        session = self.started()
        after = self.lane.restarted()
        row = only_delivery(after, session['session_id'])
        self.assertEqual((row['state'], row['written']), ('outcome_unknown', False))
        self.assertEqual(work_states(after), [('launch', 'outcome_unknown'), ('stop', 'pending')])
        self.assertEqual(session_of(after, session['session_id'])['state'], 'stop_pending')
        after.settle()
        self.assertEqual(self.lane.units.launched, [], 'never written, and never written after the restart')
        settled = session_of(after, session['session_id'])
        self.assertEqual((settled['state'], settled['worker'], settled['active_turn_id']), ('idle', None, None))
        turn = settled['turns'][0]
        self.assertEqual((turn['state'], turn['result']['code']), ('failed', 'stopped_before_start'))
        self.assertIn(('effect.outcome_unknown', 'daemon_restart'),
                      [(event['type'], event['payload'].get('why')) for event in after.store.events])
        # The same command sent again after the restart is answered from its receipt and starts nothing.
        again = after.start(prompt='Read the README.')
        self.assertEqual(again['session']['session_id'], session['session_id'])
        after.settle()
        self.assertEqual(self.lane.units.launched, [])

    # ------------------------------------------------------------------------------------ the write
    def test_a_crash_before_the_write_is_never_written(self):
        session = self.started()
        self.lane.units.then = ['cut-before']
        with self.assertRaises(Cut):
            self.lane.settle()
        self.assertEqual(work_states(self.lane), [('launch', 'attempting')])
        after = self.lane.restarted()
        self.assertEqual(work_states(after), [('launch', 'outcome_unknown'), ('stop', 'pending')])
        after.settle()
        self.assertEqual(self.lane.units.launched, [])
        self.assertEqual(only_delivery(after, session['session_id'])['state'], 'outcome_unknown')
        self.assertEqual(session_of(after, session['session_id'])['state'], 'idle')

    def test_a_crash_after_the_write_is_not_written_again_and_the_unit_is_stopped(self):
        session = self.started()
        self.lane.units.then = ['cut-after']
        with self.assertRaises(Cut):
            self.lane.settle()
        self.assertEqual(len(self.lane.units.launched), 1)
        after = self.lane.restarted()
        after.settle()
        self.assertEqual(len(self.lane.units.launched), 1, 'a write nobody recorded is not made again')
        run_id = self.lane.units.launched[0].run_id
        self.assertEqual(self.lane.units.stops, [run_id])
        row = only_delivery(after, session['session_id'])
        self.assertEqual((row['state'], row['written']), ('outcome_unknown', False))
        self.assertEqual(session_of(after, session['session_id'])['state'], 'idle')

    # ------------------------------------------------------------------------------ the acceptance
    def test_a_crash_before_the_acceptance_keeps_a_late_acceptance_as_evidence_only(self):
        session = self.started()
        self.lane.settle()
        row = only_delivery(self.lane, session['session_id'])
        self.assertEqual((row['state'], row['written']), ('delivering', True))
        after = self.lane.restarted()
        self.assertEqual(after.sessions.accepted(acceptance(row)), 'not_delivering')
        kept = only_delivery(after, session['session_id'])
        self.assertEqual((kept['state'], kept['evidence']), ('outcome_unknown', [acceptance(row)]))
        self.assertEqual(session_of(after, session['session_id'])['turns'][0]['state'], 'queued')
        after.settle()
        self.assertEqual(session_of(after, session['session_id'])['turns'][0]['state'], 'failed')

    def test_a_crash_after_the_acceptance_keeps_the_delivered_item_and_stops_the_unit(self):
        session = self.lane.start(engine='claude_code')['session']
        item = self.lane.call('session.queue', {'command_id': 'cmd-queue-0001', 'session_id': session['session_id'],
                                                'text': 'Read the README.'})['item']
        self.lane.settle()
        row = only_delivery(self.lane, session['session_id'])
        self.assertEqual(self.lane.sessions.accepted(acceptance(row)), 'delivered')
        running = session_of(self.lane, session['session_id'])
        self.assertEqual((running['state'], running['turns'][0]['state'], running['worker']['alive']),
                         ('live', 'running', True))
        after = self.lane.restarted()
        with after.store.transaction() as tx:
            kept = store.load_item(tx, item['item_id'])
        self.assertEqual((kept['state'], kept['turn_id']), ('delivered', row['target_turn_id']))
        self.assertEqual(session_of(after, session['session_id'])['state'], 'stop_pending')
        after.settle()
        settled = session_of(after, session['session_id'])
        self.assertEqual((settled['state'], settled['turns'][0]['state'], settled['turns'][0]['outcome']),
                         ('idle', 'stopped', 'stopped'))
        self.assertEqual(self.lane.units.stops, [row['run_id']])
        ended = [event for event in after.store.events if event['type'] == 'turn.ended']
        self.assertEqual([(event['turn_id'], event['payload']['outcome']) for event in ended],
                         [(row['target_turn_id'], 'stopped')])
        started = [event for event in after.store.events if event['type'] == 'turn.started']
        self.assertEqual([event['payload'] for event in started],
                         [{'delivery_id': row['delivery_id'], 'index': 0, 'text': 'Read the README.'}])

    # --------------------------------------------------------------------- a turn that is no longer active
    def test_an_acceptance_for_a_turn_that_has_ended_changes_nothing(self):
        session = self.started(engine='codex')
        self.lane.settle()
        row = only_delivery(self.lane, session['session_id'])
        self.lane.sessions.turn_finished(session['session_id'], row['target_turn_id'], outcome='failed',
                                         result={'code': 'engine_gone', 'detail': '', 'result_ref': None},
                                         worker_alive=False)
        self.assertEqual(self.lane.sessions.accepted(acceptance(row)), 'not_delivering')
        after = session_of(self.lane, session['session_id'])
        self.assertEqual((after['state'], after['turns'][0]['state']), ('idle', 'failed'))
        self.assertEqual(self.lane.sessions.accepted({**acceptance(row), 'delivery_id': 'dlv-never'}),
                         'delivery_unknown')

    # -------------------------------------------------------------------------- a Stop that is pending
    def test_a_restart_with_a_stop_pending_never_starts_a_second_unit(self):
        session = self.started(engine='opencode')
        self.lane.sessions.engine_session_reported(session['session_id'], 'engine-conversation-1')
        self.lane.settle()
        row = only_delivery(self.lane, session['session_id'])
        self.lane.sessions.accepted(acceptance(row))
        self.lane.units.stop_answers = [stop_answer(row['run_id'], 'pending')] * 2
        self.lane.sessions.request_stop(session['session_id'])
        self.assertEqual(self.lane.sessions.run_pending(), 1)          # the Stop answers pending
        pending = session_of(self.lane, session['session_id'])
        self.assertEqual((pending['state'], pending['worker']['run_id']), ('stop_pending', row['run_id']))
        turn = self.lane.call('session.turn', {'command_id': 'cmd-turn-0001', 'session_id': session['session_id'],
                                               'text': 'more', 'expected_revision': pending['revision']})
        self.assertEqual((turn['code'], turn['detail']), ('busy', 'stop_pending'))
        self.lane.call('session.queue', {'command_id': 'cmd-queue-0001', 'session_id': session['session_id'],
                                         'text': 'after the stop'})
        self.assertEqual(self.lane.sessions.dispatch_queued(), 0)
        after = self.lane.restarted()
        self.assertEqual(work_states(after), [('launch', 'done'), ('stop', 'pending')], 'one Stop, asked again')
        self.assertEqual(after.sessions.dispatch_queued(), 0)
        self.assertEqual(after.sessions.run_pending(), 1)                # asked again: pending again
        held = session_of(after, session['session_id'])
        self.assertEqual(held['state'], 'stop_pending', 'pending never returns a session to idle')
        self.assertEqual(after.sessions.dispatch_queued(), 0)
        self.assertEqual(len(self.lane.units.launched), 1)
        self.assertEqual(after.sessions.run_pending(), 1)                # the third Stop is confirmed
        after.settle()
        settled = session_of(after, session['session_id'])
        self.assertEqual(settled['turns'][0]['state'], 'stopped')
        self.assertEqual(len(self.lane.units.launched), 2, 'the queued message went only after the Stop settled')
        self.assertEqual(self.lane.units.launched[1].resume, 'engine-conversation-1')
        self.assertEqual(settled['state'], 'live')

    def test_a_stop_answer_this_lane_does_not_know_is_not_a_stop(self):
        session = self.started()
        self.lane.settle()
        run_id = self.lane.units.launched[0].run_id
        self.lane.units.stop_answers = [{'code': 'stopped', 'run_id': 'run-other', 'state': 'stopped', 'repeat': False},
                                        {'code': 'gone', 'run_id': run_id, 'state': 'gone'}, RuntimeError('no answer')]
        self.lane.sessions.request_stop(session['session_id'])
        with self.assertLogs('recursion_core.sessions.service', 'ERROR') as logged:
            for _ in range(3):
                self.lane.sessions.run_pending()
                self.assertEqual(session_of(self.lane, session['session_id'])['state'], 'stop_pending')
        self.assertEqual(len(logged.records), 1)
        self.lane.sessions.run_pending()
        self.assertEqual(session_of(self.lane, session['session_id'])['state'], 'idle')
        self.assertEqual(self.lane.units.stops, [run_id] * 4)


class DeliveryOrderTest(unittest.TestCase):

    def setUp(self):
        self.lane = Lane()

    def running(self, engine):
        session = self.lane.start(engine=engine, prompt='first')['session']
        self.lane.settle()
        self.assertEqual(self.lane.accept_input(session['session_id']), 'delivered')
        return session_of(self.lane, session['session_id'])

    def test_a_queued_message_waits_for_the_turn_s_end_and_then_begins_the_next_turn(self):
        live = self.running('codex')
        item = self.lane.call('session.queue', {'command_id': 'cmd-queue-0001', 'session_id': live['session_id'],
                                                'text': 'then the changelog'})['item']
        self.lane.settle()
        self.assertEqual(self.lane.units.sent, [])
        self.lane.sessions.turn_finished(live['session_id'], live['active_turn_id'], outcome='done',
                                         result={'code': None, 'detail': '', 'result_ref': None}, worker_alive=True)
        self.lane.settle()
        self.assertEqual([(order.run_id, order.how, order.text) for order in self.lane.units.sent],
                         [(live['worker']['run_id'], 'new_turn', 'then the changelog')])
        self.assertEqual(self.lane.accept_input(live['session_id']), 'delivered')
        with self.lane.store.transaction() as tx:
            self.assertEqual(store.load_item(tx, item['item_id'])['state'], 'delivered')
        after = session_of(self.lane, live['session_id'])
        self.assertEqual([turn['state'] for turn in after['turns']], ['ended', 'running'])

    def test_a_steer_goes_into_the_turn_in_progress_where_the_driver_takes_input_mid_turn(self):
        live = self.running('codex')
        item = self.lane.call('session.steer', {'command_id': 'cmd-steer-0001', 'session_id': live['session_id'],
                                                'text': 'skip the docs', 'expected_turn_id': live['active_turn_id']})
        self.lane.settle()
        self.assertEqual([(order.how, order.turn_id) for order in self.lane.units.sent],
                         [('mid_turn', live['active_turn_id'])])
        self.assertIn('steer.sent', [event['type'] for event in self.lane.store.events])
        self.assertEqual(self.lane.accept_input(live['session_id']), 'delivered')
        after = session_of(self.lane, live['session_id'])
        self.assertEqual((after['active_turn_id'], len(after['turns'])), (live['active_turn_id'], 1))
        with self.lane.store.transaction() as tx:
            self.assertEqual(store.load_item(tx, item['item']['item_id'])['turn_id'], live['active_turn_id'])

    def test_a_steer_waits_for_the_next_turn_where_the_driver_cannot_take_input_mid_turn(self):
        live = self.running('opencode')
        self.lane.sessions.engine_session_reported(live['session_id'], 'engine-conversation-1')
        self.lane.call('session.steer', {'command_id': 'cmd-steer-0001', 'session_id': live['session_id'],
                                         'text': 'skip the docs', 'expected_turn_id': live['active_turn_id']})
        self.lane.settle()
        self.assertEqual((self.lane.units.sent, len(self.lane.units.launched)), ([], 1))
        self.lane.sessions.turn_finished(live['session_id'], live['active_turn_id'], outcome='done',
                                         result={'code': None, 'detail': '', 'result_ref': None}, worker_alive=False)
        self.lane.settle()
        self.assertEqual([(order.text, order.resume) for order in self.lane.units.launched[1:]],
                         [('skip the docs', 'engine-conversation-1')])

    def test_items_go_in_their_order_one_delivery_at_a_time(self):
        live = self.running('codex')
        self.lane.call('session.queue', {'command_id': 'cmd-queue-0001', 'session_id': live['session_id'],
                                         'text': 'first in line'})
        self.lane.call('session.steer', {'command_id': 'cmd-steer-0001', 'session_id': live['session_id'],
                                         'text': 'behind it', 'expected_turn_id': live['active_turn_id']})
        self.lane.settle()
        self.assertEqual(self.lane.units.sent, [], 'the steer waits behind the message queued before it')

    def test_a_second_steer_waits_until_the_first_has_its_answer(self):
        live = self.running('codex')
        for number, text in enumerate(('one', 'two'), 1):
            self.lane.call('session.steer', {'command_id': f'cmd-steer-000{number}', 'session_id': live['session_id'],
                                             'text': text, 'expected_turn_id': live['active_turn_id']})
        self.lane.settle()
        self.assertEqual([order.text for order in self.lane.units.sent], ['one'], 'one delivery in flight at a time')
        self.assertEqual(self.lane.accept_input(live['session_id']), 'delivered')
        self.lane.settle()
        self.assertEqual([order.text for order in self.lane.units.sent], ['one', 'two'])

    def test_a_launch_that_definitely_did_not_start_fails_the_turn_and_nothing_is_retried(self):
        self.lane.units.then = [UnitRefused('engine_missing', 'no such binary')]
        session = self.lane.start(prompt='first')['session']
        self.lane.settle()
        after = session_of(self.lane, session['session_id'])
        self.assertEqual((after['state'], after['turns'][0]['state'], after['turns'][0]['result']['code']),
                         ('idle', 'failed', 'engine_missing'))
        self.assertEqual(work_states(self.lane), [('launch', 'failed')])
        self.assertEqual(only_delivery(self.lane, session['session_id'])['state'], 'outcome_unknown')
        self.assertEqual(self.lane.units.launched, [])

    def test_a_launch_whose_outcome_is_unknown_is_never_retried_and_its_unit_is_stopped(self):
        self.lane.units.then = [RuntimeError('the pipe broke')]
        session = self.lane.start(prompt='first')['session']
        with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
            self.lane.settle()
        self.assertEqual(work_states(self.lane), [('launch', 'outcome_unknown'), ('stop', 'done')])
        after = session_of(self.lane, session['session_id'])
        self.assertEqual((after['state'], after['turns'][0]['state']), ('idle', 'failed'))
        self.assertEqual(self.lane.units.launched, [])


class MachineTest(unittest.TestCase):
    """Refusals of the machine that the paths above do not reach."""

    def session(self):
        return machine.new_session(session_id='ses-1', created_at='2026-11-14T09:00:00.000Z', tenant='t1',
                                   principal={'kind': 'owner', 'id': 'local'},
                                   route={'engine': 'codex', 'model': None,
                                          'connection': {'kind': 'subscription', 'id': 'login'}},
                                   workspace=None, budget={'cap_usd': 0})

    def test_a_stop_needs_a_unit_and_is_asked_once(self):
        idle = self.session()
        with self.assertRaises(machine.Refused) as caught:
            machine.request_stop(idle)
        self.assertEqual(caught.exception.code, 'no_unit')
        live = machine.open_turn(idle, turn_id='turn-1', run_id='run-1', worker_alive=False)
        pending = machine.request_stop(live)
        with self.assertRaises(machine.Refused) as caught:
            machine.request_stop(pending)
        self.assertEqual(caught.exception.code, 'stop_already_pending')
        with self.assertRaises(machine.Refused) as caught:
            machine.stop_settled(pending, 'pending', '2026-11-14T09:00:01.000Z')
        self.assertEqual(caught.exception.code, 'stop_not_confirmed')

    def test_a_turn_that_finishes_while_a_stop_is_pending_leaves_the_session_stop_pending(self):
        live = machine.open_turn(self.session(), turn_id='turn-1', run_id='run-1', worker_alive=False)
        running = machine.turn_running(live, 'turn-1', '2026-11-14T09:00:01.000Z')
        pending = machine.request_stop(running)
        ended = machine.turn_finished(pending, 'turn-1', outcome='done',
                                      result={'code': None, 'detail': '', 'result_ref': None},
                                      at='2026-11-14T09:00:02.000Z', worker_alive=False)
        self.assertEqual((ended['state'], ended['active_turn_id'], ended['worker']),
                         ('stop_pending', None, {'run_id': 'run-1', 'alive': False}))
        idle = machine.stop_settled(ended, 'ended', '2026-11-14T09:00:03.000Z')
        self.assertEqual((idle['state'], idle['worker'], idle['turns'][0]['state']), ('idle', None, 'ended'))
        self.assertEqual([record['revision'] for record in (live, running, pending, ended, idle)], [2, 3, 4, 5, 6])

    def test_a_queued_turn_can_only_fail(self):
        live = machine.open_turn(self.session(), turn_id='turn-1', run_id='run-1', worker_alive=False)
        with self.assertRaises(machine.Refused):
            machine.turn_finished(live, 'turn-1', outcome='done',
                                  result={'code': None, 'detail': '', 'result_ref': None},
                                  at='2026-11-14T09:00:01.000Z', worker_alive=False)


class ServiceLifecycleTest(unittest.TestCase):

    def test_the_service_recovers_then_carries_out_work_when_woken_and_joins_on_stop(self):
        lane = Lane()
        recovered, launched = threading.Event(), threading.Event()
        real_recover, real_launch = lane.sessions.recover, lane.units.launch

        def recover():
            real_recover()
            recovered.set()

        def launch(order):
            real_launch(order)
            launched.set()

        lane.sessions.recover, lane.units.launch = recover, launch
        lane.service.interval_s = BOUND_S
        lane.service.start(None)
        try:
            self.assertTrue(recovered.wait(BOUND_S))
            self.assertEqual(lane.start(prompt='first')['code'], 'ok')    # its after-commit callback wakes the service
            self.assertTrue(launched.wait(BOUND_S))
            self.assertTrue(lane.service.ready)
        finally:
            lane.service.stop(None)
        self.assertIsNone(lane.service._thread)
        self.assertEqual(len(lane.units.launched), 1)


if __name__ == '__main__':
    unittest.main()
