# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""An interrupt, behind the machine and the service: the driver's own cancel where it declares ``interrupt``, a Stop
of the unit where it does not (``stop_then_resume``), and never a second unit while that Stop is pending.

There is no ``session.interrupt`` handler yet (questions.md, Q4): these tests call the service directly.
"""
from __future__ import annotations

import unittest

from recursion_core.sessions import machine, store
from recursion_core.sessions.tests.support import Lane, UnitRefused, published_rules, stop_answer

OWNER = {'kind': 'owner', 'id': 'local'}
DONE = {'code': None, 'detail': '', 'result_ref': None}


def session_of(lane, session_id):
    with lane.store.transaction() as tx:
        return store.load(tx, session_id)


def work_states(lane):
    with lane.store.transaction() as tx:
        return [(operation['kind'], operation['state']) for operation in store.work(tx)]


class InterruptTest(unittest.TestCase):

    def setUp(self):
        self.lane = Lane()

    def running(self, engine):
        session = self.lane.start(engine=engine, prompt='first')['session']
        self.lane.settle()
        self.assertEqual(self.lane.accept_input(session['session_id']), 'delivered')
        return session_of(self.lane, session['session_id'])

    def events_of(self, kind):
        return [event for event in self.lane.store.events if event['type'] == kind]

    def test_the_driver_s_cancel_is_committed_sent_and_the_turn_ends_only_when_it_is_reported(self):
        live = self.running('codex')
        turn_id = live['active_turn_id']
        outcome, after = self.lane.sessions.interrupt(live['session_id'], turn_id, OWNER)
        self.assertEqual((outcome, after['state']), ('requested', 'live'))
        self.assertEqual(work_states(self.lane)[-1], ('interrupt', 'pending'))
        self.assertEqual(self.events_of('interrupt'), [], 'nothing is said to have happened yet')
        self.lane.settle()
        self.assertEqual([(order.turn_id, order.run_id) for order in self.lane.units.interrupts],
                         [(turn_id, live['worker']['run_id'])])
        self.assertEqual(session_of(self.lane, live['session_id'])['turns'][0]['state'], 'running')
        ended = self.lane.sessions.turn_interrupted(live['session_id'], turn_id, worker_alive=True)
        self.assertEqual((ended['state'], ended['worker'], ended['turns'][0]['state'], ended['turns'][0]['outcome']),
                         ('idle', {'run_id': live['worker']['run_id'], 'alive': True}, 'interrupted', 'interrupted'))
        self.assertEqual([event['payload'] for event in self.events_of('interrupt')],
                         [{'turn_id': turn_id, 'requested_by': OWNER, 'outcome': 'delivered'}])
        self.assertEqual([event['payload']['outcome'] for event in self.events_of('turn.ended')], ['interrupted'])
        self.assertEqual(self.lane.units.stops, [], 'an interrupt that the driver can make is not a Stop')

    def test_after_an_interrupt_a_queued_message_goes_to_the_same_worker_as_the_next_turn(self):
        live = self.running('codex')
        self.lane.call('session.queue', {'command_id': 'cmd-queue-0001', 'session_id': live['session_id'],
                                         'text': 'instead, do this'})
        self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
        self.lane.settle()
        self.lane.sessions.turn_interrupted(live['session_id'], live['active_turn_id'], worker_alive=True)
        self.lane.settle()
        self.assertEqual([(order.run_id, order.how, order.text) for order in self.lane.units.sent],
                         [(live['worker']['run_id'], 'new_turn', 'instead, do this')])

    def test_an_interrupt_aimed_at_another_turn_is_a_conflict_and_asks_nothing(self):
        live = self.running('codex')
        with self.assertRaises(machine.Refused) as caught:
            self.lane.sessions.interrupt(live['session_id'], 'turn-not-this-one', OWNER)
        self.assertEqual((caught.exception.code, caught.exception.detail), ('conflict', 'stale_turn'))
        self.assertEqual(work_states(self.lane), [('launch', 'done')])

    def test_an_interrupt_with_no_live_turn_says_so(self):
        session = self.lane.start(engine='codex')['session']
        outcome, after = self.lane.sessions.interrupt(session['session_id'], 'turn-whatever', OWNER)
        self.assertEqual((outcome, after), ('no_live_turn', session))
        self.assertEqual([event['payload'] for event in self.events_of('interrupt')],
                         [{'turn_id': None, 'requested_by': OWNER, 'outcome': 'no_live_turn'}])
        self.assertEqual(work_states(self.lane), [])

    def test_where_the_driver_cannot_interrupt_a_stop_is_asked_and_the_next_turn_resumes_after_it(self):
        live = self.running('opencode')
        self.lane.sessions.engine_session_reported(live['session_id'], 'engine-conversation-1')
        run_id = live['worker']['run_id']
        self.lane.units.stop_answers = [stop_answer(run_id, 'pending')]
        outcome, after = self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
        self.assertEqual((outcome, after['state']), ('fell_back_to_stop', 'stop_pending'))
        self.assertEqual([event['payload']['outcome'] for event in self.events_of('interrupt')], ['fell_back_to_stop'])
        self.lane.sessions.run_pending()                              # the Stop answers pending
        pending = session_of(self.lane, live['session_id'])
        turn = self.lane.call('session.turn', {'command_id': 'cmd-turn-0001', 'session_id': live['session_id'],
                                               'text': 'next', 'expected_revision': pending['revision']})
        self.assertEqual((turn['code'], turn['detail']), ('busy', 'stop_pending'))
        self.lane.sessions.run_pending()                              # the Stop is confirmed
        idle = session_of(self.lane, live['session_id'])
        self.assertEqual((idle['state'], idle['worker'], idle['turns'][0]['state']), ('idle', None, 'stopped'))
        again = self.lane.call('session.turn', {'command_id': 'cmd-turn-0002', 'session_id': live['session_id'],
                                                'text': 'next', 'expected_revision': idle['revision']})
        self.assertEqual(again['code'], 'ok')
        self.lane.settle()
        self.assertEqual([order.resume for order in self.lane.units.launched], [None, 'engine-conversation-1'])
        self.assertEqual(self.lane.units.interrupts, [])

    def test_a_turn_that_has_not_started_is_stopped_not_interrupted(self):
        session = self.lane.start(engine='codex', prompt='first')['session']
        self.lane.settle()                                            # written, not yet taken
        outcome, after = self.lane.sessions.interrupt(session['session_id'], session['active_turn_id'], OWNER)
        self.assertEqual((outcome, after['state']), ('fell_back_to_stop', 'stop_pending'))
        self.lane.settle()
        settled = session_of(self.lane, session['session_id'])
        self.assertEqual((settled['state'], settled['turns'][0]['state'], settled['turns'][0]['result']['code']),
                         ('idle', 'failed', 'stopped_before_start'))

    def test_a_cancel_the_driver_refuses_while_the_turn_runs_falls_back_to_a_stop(self):
        live = self.running('codex')
        self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
        self.lane.units.then = [UnitRefused('turn_not_in_progress')]
        self.lane.sessions.run_pending()
        self.assertEqual(work_states(self.lane)[1:], [('interrupt', 'failed'), ('stop', 'pending')])
        self.assertEqual(session_of(self.lane, live['session_id'])['state'], 'stop_pending')

    def test_a_cancel_whose_outcome_is_unknown_is_never_retried_and_falls_back_to_a_stop(self):
        live = self.running('codex')
        self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
        self.lane.units.then = [RuntimeError('the pipe broke')]
        with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
            self.lane.settle()
        self.assertEqual(work_states(self.lane)[1:], [('interrupt', 'outcome_unknown'), ('stop', 'done')])
        self.assertEqual(self.lane.units.interrupts, [])
        self.assertEqual(session_of(self.lane, live['session_id'])['turns'][0]['state'], 'stopped')

    def test_a_refused_cancel_for_a_turn_that_already_ended_asks_nothing_more(self):
        live = self.running('codex')
        self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)

        def interrupt(order):        # the turn ends while the cancel is on its way, and the driver refuses it
            self.lane.sessions.turn_finished(live['session_id'], live['active_turn_id'], outcome='done', result=DONE,
                                             worker_alive=True)
            raise UnitRefused('turn_not_in_progress')

        self.lane.units.interrupt = interrupt
        self.lane.sessions.run_pending()
        self.assertEqual(work_states(self.lane)[1:], [('interrupt', 'failed')])
        self.assertEqual(session_of(self.lane, live['session_id'])['state'], 'idle')

    def test_an_interrupt_while_a_stop_is_pending_asks_nothing_more(self):
        live = self.running('codex')
        self.lane.sessions.request_stop(live['session_id'])
        outcome, _ = self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
        self.assertEqual(outcome, 'stop_pending')
        self.assertEqual(work_states(self.lane)[1:], [('stop', 'pending')])

    def test_an_interrupt_whose_turn_ended_or_is_being_stopped_before_its_claim_is_never_sent(self):
        """As a launch or a send is never attempted under a pending Stop (the fence), a cancel is sent only while
        its turn is still the one in progress of a live session; otherwise it fails at its claim, unsent."""
        for end in ('finished', 'replaced', 'stop'):
            with self.subTest(end=end):
                self.lane = Lane()
                live = self.running('codex')
                self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
                if end in ('finished', 'replaced'):
                    idle = self.lane.sessions.turn_finished(live['session_id'], live['active_turn_id'],
                                                            outcome='done', result=DONE, worker_alive=True)
                    if end == 'replaced':          # the next turn is in progress in the same worker
                        answer = self.lane.call('session.turn', {
                            'command_id': 'cmd-turn-0001', 'session_id': live['session_id'], 'text': 'next',
                            'expected_revision': idle['revision']})
                        self.assertEqual(answer['code'], 'ok', answer)
                        self.assertEqual(session_of(self.lane, live['session_id'])['state'], 'live')
                else:
                    self.lane.units.stop_answers = [stop_answer(live['worker']['run_id'], 'pending')]
                    self.lane.sessions.request_stop(live['session_id'])
                self.lane.sessions.run_pending()
                self.assertEqual(self.lane.units.interrupts, [], 'never sent')
                with self.lane.store.transaction() as tx:
                    found = [(operation['state'], operation['outcome']) for operation in store.work(tx)
                             if operation['kind'] == 'interrupt']
                self.assertEqual(found, [('failed', {'code': 'turn_not_in_progress'})])

    def test_a_restart_with_an_interrupt_pending_never_sends_it_and_stops_the_unit(self):
        live = self.running('codex')
        self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
        after = self.lane.restarted()
        self.assertEqual(work_states(after)[1:], [('interrupt', 'outcome_unknown'), ('stop', 'pending')])
        after.settle()
        self.assertEqual(self.lane.units.interrupts, [])
        self.assertEqual(session_of(after, live['session_id'])['state'], 'idle')

    def test_an_interruption_nobody_asked_for_or_of_a_turn_that_has_not_started_is_refused(self):
        live = self.running('codex')
        with self.assertRaises(machine.Refused) as caught:
            self.lane.sessions.turn_interrupted(live['session_id'], live['active_turn_id'], worker_alive=True)
        self.assertEqual(caught.exception.code, 'no_interrupt_asked')
        queued = machine.open_turn(machine.new_session(
            session_id='ses-1', created_at='2026-11-14T09:00:00.000Z', tenant='t1', principal=OWNER,
            route={'engine': 'codex', 'model': None, 'connection': {'kind': 'subscription', 'id': 'login'}},
            workspace=None, budget={'cap_usd': 0}), turn_id='turn-1', run_id='run-1', worker_alive=True)
        with self.assertRaises(machine.Refused) as caught:
            machine.turn_interrupted(queued, 'turn-1', '2026-11-14T09:00:01.000Z', worker_alive=True)
        self.assertEqual(caught.exception.code, 'turn_not_running')

    def test_the_interrupt_events_fit_the_published_payload(self):
        rules = published_rules()
        self.assertIsNotNone(rules)
        live = self.running('codex')
        self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
        self.lane.settle()
        self.lane.sessions.turn_interrupted(live['session_id'], live['active_turn_id'], worker_alive=True)
        other = self.lane.start(engine='codex', command_id='cmd-start-0002')['session']
        self.lane.sessions.interrupt(other['session_id'], 'turn-whatever', OWNER)
        found = self.events_of('interrupt')
        self.assertEqual(len(found), 2)
        for event in found:
            with self.subTest(outcome=event['payload']['outcome']):
                self.assertTrue(rules.fits('event.v1.json#/$defs/interrupt', event['payload']))
                self.assertEqual(event['turn_id'], event['payload']['turn_id'])


if __name__ == '__main__':
    unittest.main()
