# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""An interrupt, behind the machine and the service: the driver's own cancel where it declares ``interrupt``, a Stop
of the unit where it does not (``stop_then_resume``), and never a second unit while that Stop is pending.

There is no ``session.interrupt`` handler yet (questions.md, Q4): these tests call the service directly.
"""
from __future__ import annotations

import unittest
from unittest import mock

from recursion_core.sessions import machine, records, store
from recursion_core.sessions.tests.support import Cut, Lane, UnitRefused, published_rules, stop_answer

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


class FakeCommitFailure:
    """The event store's helper, whose next transaction fails to open once ``armed`` is set: a commit that fails
    after an outside call has finished (a store error, or a conflict in a store whose transactions overlap)."""

    def __init__(self, inner) -> None:
        self.inner, self.armed = inner, False

    def transaction(self):
        if self.armed:
            self.armed = False
            raise OSError('the commit failed')
        return self.inner.transaction()

    def __getattr__(self, name):
        return getattr(self.inner, name)


BAD_REQUESTERS = ({}, {'kind': 'owner', 'id': ''}, {'kind': 'boss', 'id': 'x'},
                  {'kind': 'owner', 'id': 'x', 'extra': 1}, {'kind': 'owner'}, {'kind': 'owner', 'id': 'a b'},
                  None, 'owner', ['owner', 'local'])


class InterruptAnswersTest(unittest.TestCase):
    """The first reads of #415: Codex GPT-6 Astra R1-R4, GPT-6.1 Sol R1-R2, DeepSeek F1-F6; and the Liaison's ruling
    on Sol's O1 (Nov 15, 4:26 PM): a second request while a cancel for that turn is pending, attempting or sent
    sends no second cancel and gets the first request's answer."""

    def setUp(self):
        self.lane = Lane()

    def running(self, engine='codex'):
        session = self.lane.start(engine=engine, prompt='first')['session']
        self.lane.settle()
        self.assertEqual(self.lane.accept_input(session['session_id']), 'delivered')
        return session_of(self.lane, session['session_id'])

    def events_of(self, kind):
        return [event for event in self.lane.store.events if event['type'] == kind]

    def work_of(self, kind, lane=None):
        with (lane or self.lane).store.transaction() as tx:
            return [(operation['state'], operation['outcome']) for operation in store.work(tx)
                    if operation['kind'] == kind]

    def next_turn(self, sid, idle):
        answer = self.lane.call('session.turn', {'command_id': 'cmd-turn-0001', 'session_id': sid, 'text': 'second',
                                                 'expected_revision': idle['revision']})
        self.assertEqual(answer['code'], 'ok', answer)
        return session_of(self.lane, sid)

    # ------------------------------------------------------------------------- the requester (R1, F1, F3)
    def test_a_requester_that_is_not_a_principal_is_refused_before_anything_is_written(self):
        for path in ('native', 'fallback', 'no_live_turn'):
            self.lane = Lane()
            if path == 'no_live_turn':
                sid = self.lane.start(engine='codex')['session']['session_id']
                turn_id = 'turn-whatever'
            else:
                live = self.running('codex' if path == 'native' else 'opencode')
                sid, turn_id = live['session_id'], live['active_turn_id']
            for requested_by in BAD_REQUESTERS:
                with self.subTest(path=path, requested_by=requested_by):
                    before = (session_of(self.lane, sid), self.work_of('interrupt') + self.work_of('stop'),
                              len(self.lane.store.events))
                    with self.assertRaises(machine.Refused) as caught:
                        self.lane.sessions.interrupt(sid, turn_id, requested_by)
                    self.assertEqual((caught.exception.code, caught.exception.detail),
                                     ('request_invalid', 'requested_by'))
                    self.assertEqual((session_of(self.lane, sid), self.work_of('interrupt') + self.work_of('stop'),
                                      len(self.lane.store.events)), before, 'nothing written')
            self.lane.settle()
            self.assertEqual(self.lane.units.interrupts, [])

    def test_the_requester_is_kept_as_its_kind_and_id_and_fits_the_published_event(self):
        rules = published_rules()
        live = self.running()
        device = {'kind': 'device', 'id': 'phone-1'}
        self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], device)
        device['id'] = 'changed-after-the-call'                       # the caller's object is not kept
        self.lane.settle()
        self.lane.sessions.turn_interrupted(live['session_id'], live['active_turn_id'], worker_alive=True)
        found = self.events_of('interrupt')
        self.assertEqual([event['payload']['requested_by'] for event in found], [{'kind': 'device', 'id': 'phone-1'}])
        self.assertTrue(rules.fits('event.v1.json#/$defs/interrupt', found[0]['payload']))

    def test_a_requester_changed_after_it_was_checked_is_never_committed_as_changed(self):
        """Round 2 of #415 (Codex GPT-6 Astra and GPT-6.1 Sol, required change 1): the caller's dict was checked and
        only then copied, so another thread that changed it between the two committed what had not been checked."""
        real = records.check_principal
        for path in ('native', 'fallback', 'no_live_turn'):
            with self.subTest(path=path):
                self.lane = Lane()
                if path == 'no_live_turn':
                    sid, turn_id = self.lane.start(engine='codex')['session']['session_id'], 'turn-whatever'
                else:
                    live = self.running('codex' if path == 'native' else 'opencode')
                    sid, turn_id = live['session_id'], live['active_turn_id']
                requested_by = {'kind': 'device', 'id': 'phone-1'}

                def check_then_change(value, where='/principal', requested_by=requested_by):
                    real(value, where)
                    requested_by['id'] = ''                 # the caller's dict, changed once the check has passed

                with mock.patch.object(records, 'check_principal', check_then_change):
                    self.lane.sessions.interrupt(sid, turn_id, requested_by)
                with self.lane.store.transaction() as tx:
                    committed = [operation['payload']['requested_by'] for operation in store.work(tx)
                                 if operation['kind'] == 'interrupt']
                committed += [event['payload']['requested_by'] for event in self.events_of('interrupt')]
                self.assertTrue(committed, 'the request left a record')
                self.assertEqual(committed, [{'kind': 'device', 'id': 'phone-1'}] * len(committed))

    def _requester_paths(self):
        """The three paths of ``interrupt`` that keep the requester: (path, session id, turn id) with a fresh lane."""
        for path in ('native', 'fallback', 'no_live_turn'):
            self.lane = Lane()
            if path == 'no_live_turn':
                yield path, self.lane.start(engine='codex')['session']['session_id'], 'turn-whatever'
            else:
                live = self.running('codex' if path == 'native' else 'opencode')
                yield path, live['session_id'], live['active_turn_id']

    def _committed_requesters(self):
        with self.lane.store.transaction() as tx:
            found = [operation['payload']['requested_by'] for operation in store.work(tx)
                     if operation['kind'] == 'interrupt']
        return found + [event['payload']['requested_by'] for event in self.events_of('interrupt')]

    def test_the_value_that_is_checked_is_the_copy_not_the_callers_dict(self):
        """Review 3 of #415 (Claude Fable 5.2, required change R1). The caller's dict is changed after the copy and
        before the check: the check must be handed the copy (not the caller's object), and what was copied is what is
        taken and committed. A service that checks the caller's dict and uses the copy refuses this valid request."""
        real = records.check_principal
        for path, sid, turn_id in self._requester_paths():
            with self.subTest(path=path):
                requested_by = {'kind': 'device', 'id': 'phone-1'}
                handed = []

                def change_then_check(value, where='/principal', requested_by=requested_by, handed=handed):
                    if where == '/requested_by':            # the session's own principal is checked elsewhere too
                        handed.append(value is requested_by)
                        requested_by['id'] = ''             # the caller's dict, emptied after the copy
                    real(value, where)

                with mock.patch.object(records, 'check_principal', change_then_check):
                    self.lane.sessions.interrupt(sid, turn_id, requested_by)
                self.assertEqual(handed, [False], 'the check is handed the copy, not the caller\'s object')
                committed = self._committed_requesters()
                self.assertTrue(committed, 'the request left a record')
                self.assertEqual(committed, [{'kind': 'device', 'id': 'phone-1'}] * len(committed))

    def test_a_copy_that_is_not_a_principal_is_refused_though_the_callers_dict_is_mended_before_the_check(self):
        """The mirror of the test above: what was copied is not a principal, the caller's dict is mended after the
        copy and before the check. The copy is what is checked, so the request is refused and nothing is committed;
        a service that checks the caller's dict would commit a principal that was never checked."""
        real = records.check_principal
        for path, sid, turn_id in self._requester_paths():
            with self.subTest(path=path):
                requested_by = {'kind': 'device', 'id': ''}         # what is copied is not a principal

                def mend_then_check(value, where='/principal', requested_by=requested_by):
                    if where == '/requested_by':
                        requested_by['id'] = 'phone-1'      # mended after the copy, before the check
                    real(value, where)

                with mock.patch.object(records, 'check_principal', mend_then_check):
                    with self.assertRaises(machine.Refused) as caught:
                        self.lane.sessions.interrupt(sid, turn_id, requested_by)
                self.assertEqual((caught.exception.code, caught.exception.detail), ('request_invalid', 'requested_by'))
                self.assertEqual(self._committed_requesters(), [])
                self.assertEqual(self.work_of('interrupt') + self.work_of('stop'), [], 'nothing written')

    # ---------------------------------------------------------------- one cancel per turn (R2, F2, O1)
    def test_a_second_request_while_a_cancel_is_pending_or_sent_sends_no_second_cancel(self):
        live = self.running()
        sid, turn_id = live['session_id'], live['active_turn_id']
        first = self.lane.sessions.interrupt(sid, turn_id, OWNER)
        second = self.lane.sessions.interrupt(sid, turn_id, {'kind': 'device', 'id': 'phone-1'})
        self.assertEqual((first[0], second[0]), ('requested', 'requested'), "the second gets the first's answer")
        self.assertEqual(len(self.work_of('interrupt')), 1)
        self.lane.settle()
        third = self.lane.sessions.interrupt(sid, turn_id, OWNER)          # the cancel was sent, not yet reported
        self.assertEqual(third[0], 'requested')
        self.lane.settle()
        self.assertEqual(len(self.lane.units.interrupts), 1, 'one cancel reaches the driver')
        self.assertEqual(self.work_of('interrupt'), [('done', {'sent': True})])
        self.assertEqual((session_of(self.lane, sid)['state'], self.work_of('stop')), ('live', []))
        self.lane.sessions.turn_interrupted(sid, turn_id, worker_alive=True)
        self.assertEqual([event['payload']['requested_by'] for event in self.events_of('interrupt')], [OWNER],
                         'the delivered event names the first asker')

    def test_a_request_while_the_cancel_is_in_flight_sends_no_second_cancel(self):
        live = self.running()
        sid, turn_id = live['session_id'], live['active_turn_id']
        self.lane.sessions.interrupt(sid, turn_id, OWNER)
        real, answers = self.lane.units.interrupt, []

        def interrupt(order):
            answers.append(self.lane.sessions.interrupt(sid, turn_id, OWNER)[0])   # asked again while attempting
            real(order)

        self.lane.units.interrupt = interrupt
        self.lane.settle()
        self.assertEqual((answers, len(self.lane.units.interrupts), len(self.work_of('interrupt'))),
                         (['requested'], 1, 1))

    def test_a_new_turn_gets_its_own_cancel(self):
        live = self.running()
        sid, first = live['session_id'], live['active_turn_id']
        self.lane.sessions.interrupt(sid, first, OWNER)
        self.lane.settle()
        idle = self.lane.sessions.turn_interrupted(sid, first, worker_alive=True)
        second = self.next_turn(sid, idle)
        self.lane.settle()
        self.assertEqual(self.lane.accept_input(sid), 'delivered')
        self.assertEqual(self.lane.sessions.interrupt(sid, second['active_turn_id'], OWNER)[0], 'requested')
        self.lane.settle()
        self.assertEqual([order.turn_id for order in self.lane.units.interrupts], [first, second['active_turn_id']])

    # ------------------------------------------------------- a settlement whose commit fails (astra R3)
    def test_a_settlement_that_fails_to_commit_is_kept_and_committed_by_a_later_pass(self):
        for how in ('refused', 'sent'):
            with self.subTest(how=how):
                self.lane = Lane()
                live = self.running()
                sid, turn_id = live['session_id'], live['active_turn_id']
                self.lane.sessions.interrupt(sid, turn_id, OWNER)
                helper = FakeCommitFailure(self.lane.store)
                self.lane.sessions.store = helper
                real = self.lane.units.interrupt

                def interrupt(order, how=how, helper=helper, real=real):
                    real(order)
                    helper.armed = True                     # the commit after the outside call fails, once
                    if how == 'refused':
                        raise UnitRefused('turn_not_in_progress')

                self.lane.units.interrupt = interrupt
                with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
                    self.assertEqual(self.lane.sessions.run_pending(), 1)
                self.assertEqual(self.work_of('interrupt'), [('attempting', None)])
                self.assertEqual(session_of(self.lane, sid)['state'], 'live')
                self.lane.sessions.run_pending()            # a healthy pass commits what was kept
                if how == 'refused':
                    self.assertEqual(self.work_of('interrupt'), [('failed', {'code': 'turn_not_in_progress',
                                                                             'detail': ''})])
                    self.assertEqual(self.lane.units.stops, [live['worker']['run_id']], 'the fallback Stop is asked')
                else:
                    self.assertEqual(self.work_of('interrupt'), [('done', {'sent': True})])
                    self.assertEqual(self.lane.units.stops, [])
                self.lane.sessions.run_pending()
                self.assertEqual(len(self.lane.units.interrupts), 1, 'the cancel is never sent again')

    def test_an_unknown_cancel_whose_settlement_fails_to_commit_is_kept_and_never_sent_again(self):
        """Round 2 of #415 (Codex GPT-6 Astra and GPT-6.1 Sol, required change 2, STRICT): with the ``_settle`` of an
        unknown cancel removed, every other lane test stayed green."""
        live = self.running()
        sid, turn_id = live['session_id'], live['active_turn_id']
        self.lane.sessions.interrupt(sid, turn_id, OWNER)
        helper = FakeCommitFailure(self.lane.store)
        self.lane.sessions.store = helper
        real = self.lane.units.interrupt

        def interrupt(order):
            real(order)
            helper.armed = True                             # the commit after the outside call fails, once
            raise RuntimeError('the pipe broke')

        self.lane.units.interrupt = interrupt
        with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
            self.assertEqual(self.lane.sessions.run_pending(), 1)
        self.assertEqual(self.work_of('interrupt'), [('attempting', None)])
        self.assertEqual(session_of(self.lane, sid)['state'], 'live')
        self.lane.settle()                                  # healthy passes commit what was kept
        self.assertEqual(self.work_of('interrupt'), [('outcome_unknown', {'error': 'RuntimeError'})])
        self.assertEqual([state for state, _ in self.work_of('stop')], ['done'], 'the fallback Stop is asked')
        self.assertEqual(session_of(self.lane, sid)['turns'][0]['state'], 'stopped')
        self.assertEqual(len(self.lane.units.interrupts), 1, 'the cancel is never sent again')

    def test_a_stop_whose_settlement_fails_to_commit_is_kept_and_committed_by_a_later_pass(self):
        """The integration read of Nov 15 (Codex GPT-6.1 Sol, P2): one storage failure after a Stop's answer left its
        intent ``attempting`` and its session ``stop_pending``, and nothing asked again before a restart."""
        for first_answer in ('pending', 'stopped'):
            with self.subTest(first_answer=first_answer):
                self.lane = Lane()
                live = self.running()
                sid, run_id = live['session_id'], live['worker']['run_id']
                self.lane.sessions.request_stop(sid)
                helper = FakeCommitFailure(self.lane.store)
                self.lane.sessions.store = helper
                if first_answer == 'pending':
                    self.lane.units.stop_answers = [stop_answer(run_id, 'pending')]
                real = self.lane.units.stop

                def stop(asked, helper=helper, real=real):
                    answer = real(asked)
                    if len(self.lane.units.stops) == 1:
                        helper.armed = True                 # the commit after the first answer fails, once
                    return answer

                self.lane.units.stop = stop
                with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
                    self.assertEqual(self.lane.sessions.run_pending(), 1)
                self.assertEqual(self.work_of('stop'), [('attempting', None)])
                self.assertEqual(session_of(self.lane, sid)['state'], 'stop_pending')
                self.lane.sessions.run_pending()            # a healthy pass commits what was kept
                after = session_of(self.lane, sid)
                self.assertEqual((after['state'], after['worker']), ('idle', None))
                self.assertEqual([state for state, _ in self.work_of('stop')], ['done'])
                self.assertEqual(self.lane.units.stops, [run_id] * (2 if first_answer == 'pending' else 1),
                                 'a confirmed Stop is not asked again; one recorded as pending is')

    # ------------------------------------------------------------ rules no test held (astra R4, sol R2)
    def test_a_report_under_a_pending_stop_keeps_it_pending_and_records_the_worker_s_life(self):
        for alive in (False, True):
            with self.subTest(worker_alive=alive):
                self.lane = Lane()
                live = self.running()
                sid, turn_id = live['session_id'], live['active_turn_id']
                self.lane.sessions.interrupt(sid, turn_id, OWNER)
                self.lane.settle()
                self.lane.sessions.request_stop(sid)
                after = self.lane.sessions.turn_interrupted(sid, turn_id, worker_alive=alive)
                worker = {'run_id': live['worker']['run_id'], 'alive': alive}
                self.assertEqual((after['state'], after['worker']), ('stop_pending', worker))
                self.assertEqual((after['turns'][0]['state'], after['turns'][0]['result']), ('interrupted', None))

    def test_a_report_with_a_dead_worker_leaves_the_session_idle_with_no_worker(self):
        live = self.running()
        self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
        self.lane.settle()
        after = self.lane.sessions.turn_interrupted(live['session_id'], live['active_turn_id'], worker_alive=False)
        self.assertEqual((after['state'], after['worker']), ('idle', None))
        self.assertEqual((after['turns'][0]['outcome'], after['turns'][0]['result']), ('interrupted', None))

    def test_a_queued_turn_in_a_live_worker_is_stopped_not_cancelled(self):
        live = self.running()
        sid = live['session_id']
        idle = self.lane.sessions.turn_finished(sid, live['active_turn_id'], outcome='done', result=DONE,
                                                worker_alive=True)
        queued = self.next_turn(sid, idle)
        turn = queued['turns'][-1]
        self.assertEqual((turn['state'], queued['worker']['alive']), ('queued', True))
        outcome, after = self.lane.sessions.interrupt(sid, queued['active_turn_id'], OWNER)
        self.assertEqual((outcome, after['state']), ('fell_back_to_stop', 'stop_pending'))
        self.assertEqual(self.work_of('interrupt'), [])

    def test_an_old_cancel_refused_after_the_next_turn_began_asks_no_stop(self):
        live = self.running()
        sid, first = live['session_id'], live['active_turn_id']
        self.lane.sessions.interrupt(sid, first, OWNER)

        def interrupt(order):        # the turn ends and the next begins while the cancel is on its way
            idle = self.lane.sessions.turn_finished(sid, first, outcome='done', result=DONE, worker_alive=True)
            self.next_turn(sid, idle)
            raise UnitRefused('turn_not_in_progress')

        self.lane.units.interrupt = interrupt
        self.lane.sessions.run_pending()
        after = session_of(self.lane, sid)
        self.assertEqual(after['state'], 'live')
        self.assertNotEqual(after['active_turn_id'], first)
        self.assertEqual((self.work_of('stop'), self.events_of('interrupt')), ([], []))

    def test_a_cancel_refused_while_a_stop_is_already_pending_adds_no_stop(self):
        live = self.running()
        sid = live['session_id']
        self.lane.sessions.interrupt(sid, live['active_turn_id'], OWNER)

        def interrupt(order):        # a Stop is asked while the cancel is on its way
            self.lane.sessions.request_stop(sid)
            raise UnitRefused('turn_not_in_progress')

        self.lane.units.interrupt = interrupt
        self.lane.sessions.run_pending()
        self.assertEqual(len(self.work_of('stop')), 1)
        self.assertEqual(self.events_of('interrupt'), [])
        self.assertEqual([state for state, _ in self.work_of('interrupt')], ['failed'], 'settled, not kept')

    def test_a_report_ends_an_unaccepted_steer_s_delivery_and_a_late_acceptance_is_only_evidence(self):
        live = self.running()
        sid, turn_id = live['session_id'], live['active_turn_id']
        self.lane.call('session.steer', {'command_id': 'cmd-steer-0001', 'session_id': sid, 'text': 'skip the docs',
                                         'expected_turn_id': turn_id})
        self.lane.settle()                                            # written mid-turn, not accepted
        self.lane.sessions.interrupt(sid, turn_id, OWNER)
        self.lane.settle()
        self.lane.sessions.turn_interrupted(sid, turn_id, worker_alive=True)
        with self.lane.store.transaction() as tx:
            (row,) = [row for row in store.deliveries(tx, session_id=sid) if row['how'] == 'mid_turn']
        self.assertEqual((row['state'], row['written']), ('outcome_unknown', True))
        facts = {'delivery_id': row['delivery_id'], 'session_id': sid, 'run_id': row['run_id'],
                 'turn_id': row['target_turn_id'], 'how': 'mid_turn'}
        self.assertEqual(self.lane.sessions.accepted(facts), 'not_delivering')
        with self.lane.store.transaction() as tx:
            kept = store.load_delivery(tx, row['delivery_id'])
        self.assertEqual((kept['state'], len(kept['evidence'])), ('outcome_unknown', 1))

    def test_a_report_for_a_later_turn_nobody_asked_to_interrupt_is_refused(self):
        live = self.running()
        sid, first = live['session_id'], live['active_turn_id']
        self.lane.sessions.interrupt(sid, first, OWNER)
        self.lane.settle()
        idle = self.lane.sessions.turn_interrupted(sid, first, worker_alive=True)
        second = self.next_turn(sid, idle)
        self.lane.settle()
        self.assertEqual(self.lane.accept_input(sid), 'delivered')
        with self.assertRaises(machine.Refused) as caught:
            self.lane.sessions.turn_interrupted(sid, second['active_turn_id'], worker_alive=True)
        self.assertEqual(caught.exception.code, 'no_interrupt_asked')

    def test_a_cancel_cut_on_either_side_of_the_port_is_unknown_after_a_restart_and_never_sent_again(self):
        for cut in ('cut-before', 'cut-after'):
            with self.subTest(cut=cut):
                self.lane = Lane()
                live = self.running()
                self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
                self.lane.units.then = [cut]
                with self.assertRaises(Cut):
                    self.lane.sessions.run_pending()
                sent = len(self.lane.units.interrupts)
                self.assertEqual(sent, 0 if cut == 'cut-before' else 1)
                after = self.lane.restarted()
                self.assertEqual([state for state, _ in self.work_of('interrupt', after)], ['outcome_unknown'])
                after.settle()
                self.assertEqual(len(self.lane.units.interrupts), sent, 'never sent again')
                self.assertEqual(session_of(after, live['session_id'])['state'], 'idle')

    def test_a_sent_cancel_is_done_and_stays_done_across_a_restart(self):
        live = self.running()
        self.lane.sessions.interrupt(live['session_id'], live['active_turn_id'], OWNER)
        self.lane.settle()
        self.assertEqual(self.work_of('interrupt'), [('done', {'sent': True})])
        after = self.lane.restarted()
        self.assertEqual(self.work_of('interrupt', after), [('done', {'sent': True})])

    def test_an_attempted_stop_is_asked_again_after_a_restart_never_duplicated(self):
        """DeepSeek F6 (service.py recover: an attempted Stop goes back to pending)."""
        session = self.lane.start(prompt='first')['session']
        run_id = session['worker']['run_id']
        self.lane.settle()
        self.lane.units.stop_answers = [Cut()]
        self.lane.sessions.request_stop(session['session_id'])
        with self.assertRaises(Cut):
            self.lane.sessions.run_pending()
        self.assertEqual([state for state, _ in self.work_of('stop')], ['attempting'])
        after = self.lane.restarted()
        self.assertEqual([state for state, _ in self.work_of('stop', after)], ['pending'], 'one Stop, asked again')
        after.settle()
        self.assertEqual(self.lane.units.stops, [run_id, run_id])
        self.assertEqual(session_of(after, session['session_id'])['state'], 'idle')

    def test_a_request_check_for_a_method_this_lane_does_not_answer_is_a_refusal(self):
        """DeepSeek F4."""
        for method in ('session.interrupt', 'no.such.method'):
            with self.subTest(method=method):
                with self.assertRaises(records.RecordError) as caught:
                    records.check_request(method, {'command_id': 'cmd-00000001'})
                self.assertEqual((caught.exception.path, caught.exception.rule), ('', 'unknown_method'))


if __name__ == '__main__':
    unittest.main()
