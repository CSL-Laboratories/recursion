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

import copy
import threading
import unittest

from recursion_core.sessions import delivery, machine, records, store
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


def turn_of(lane, session, command_id='cmd-turn-0001', text='next'):
    return lane.call('session.turn', {'command_id': command_id, 'session_id': session['session_id'], 'text': text,
                                      'expected_revision': session['revision']})


DONE = {'code': None, 'detail': '', 'result_ref': None}


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
        # The claim was committed, so nobody can say the port was not called: an absence settles nothing.
        self.assertEqual(after.sessions.run_pending(), 1)
        self.assertEqual(session_of(after, session['session_id'])['state'], 'stop_pending')
        self.lane.units.stop_answers = [stop_answer(session['worker']['run_id'], 'ended')]     # its own word
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
        run_id = self.lane.units.launched[0].run_id
        self.assertIn(run_id, self.lane.units.registered, 'the unit the write started outlives the crash')
        self.lane.units.stop_answers = [stop_answer(run_id, 'pending'), stop_answer(run_id, 'stopped')]
        after = self.lane.restarted()
        self.assertEqual(after.sessions.run_pending(), 1)                # the recovery Stop answers pending
        held = session_of(after, session['session_id'])
        self.assertEqual((held['state'], held['worker']['run_id']), ('stop_pending', run_id))
        busy = turn_of(after, held)
        self.assertEqual((busy['code'], busy['detail']), ('busy', 'stop_pending'), 'no unit beside the survivor')
        self.assertEqual(after.sessions.run_pending(), 1)                # asked again: confirmed
        after.settle()
        self.assertEqual(len(self.lane.units.launched), 1, 'a write nobody recorded is not made again')
        self.assertEqual(self.lane.units.stops, [run_id, run_id])
        row = only_delivery(after, session['session_id'])
        self.assertEqual((row['state'], row['written']), ('outcome_unknown', False))
        self.assertEqual(session_of(after, session['session_id'])['state'], 'idle')

    def test_a_turn_sent_again_after_a_restart_is_answered_from_its_receipt_and_launches_nothing(self):
        session = self.lane.start(engine='codex')['session']
        params = {'command_id': 'cmd-replay-0001', 'session_id': session['session_id'], 'text': 'first',
                  'expected_revision': session['revision']}
        first = self.lane.call('session.turn', params)
        self.assertEqual(first['code'], 'ok')
        self.lane.settle()
        self.assertEqual(self.lane.accept_input(session['session_id']), 'delivered')
        after = self.lane.restarted()
        self.assertEqual(after.call('session.turn', params), first)
        after.settle()
        self.assertEqual(len(self.lane.units.launched), 1)

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

    def test_an_acceptance_whose_facts_are_not_an_object_is_answered_delivery_unknown(self):
        """DeepSeek round 3, F4: the docstring promises a code, never a traceback."""
        session = self.started(engine='codex')
        self.lane.settle()
        for facts in (None, [], 'dlv-x', 7):
            with self.subTest(facts=facts):
                self.assertEqual(self.lane.sessions.accepted(facts), 'delivery_unknown')
        self.assertEqual(only_delivery(self.lane, session['session_id'])['state'], 'delivering', 'nothing changed')

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

    def test_a_malformed_or_contradictory_stopped_answer_is_not_a_stop(self):
        session = self.started()
        self.lane.settle()
        run_id = self.lane.units.launched[0].run_id
        seen = {'proof': 'future_proof', 'cgroup': '/made-up', 'cgroup_identity': None,
                'time': '2026-11-14T09:00:00.000Z'}
        self.lane.units.stop_answers = [{**stop_answer(run_id, 'stopped'), 'attestation': seen},
                                        {'code': 'stopped', 'run_id': run_id, 'state': 'pending', 'repeat': False},
                                        {'code': 'stopped', 'run_id': run_id, 'state': 'stopped'},
                                        {**stop_answer(run_id, 'ended'), 'repeat': False}]
        self.lane.sessions.request_stop(session['session_id'])
        for _ in range(4):
            self.lane.sessions.run_pending()
            self.assertEqual(session_of(self.lane, session['session_id'])['state'], 'stop_pending')
        self.lane.sessions.run_pending()                                 # a well-formed answer, at last
        self.assertEqual(session_of(self.lane, session['session_id'])['state'], 'idle')
        self.assertEqual(self.lane.units.stops, [run_id] * 5)


class FenceTest(unittest.TestCase):
    """A Stop and a launch of the same run never leave a second unit possible (the module text of service.py)."""

    def test_a_stop_waits_for_a_launch_in_flight_and_no_replacement_opens_beside_it(self):
        lane = Lane()
        session = lane.start(engine='opencode', prompt='first')['session']
        sid, first_run = session['session_id'], session['worker']['run_id']
        lane.sessions.engine_session_reported(sid, 'engine-conversation-1')
        entered, release, errors = threading.Event(), threading.Event(), []
        real = lane.units.launch

        def launch(order):
            if order.run_id == first_run:
                entered.set()
                if not release.wait(BOUND_S):
                    raise AssertionError('the launch was never released')
            real(order)

        def carry():
            try:
                lane.sessions.run_pending()
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
                errors.append(exc)

        lane.units.launch = launch
        thread = threading.Thread(target=carry)
        thread.start()
        try:
            self.assertTrue(entered.wait(BOUND_S))
            lane.sessions.request_stop(sid)
            self.assertEqual(lane.sessions.run_pending(), 1)     # the Stop: no run registered yet, a launch in flight
            held = session_of(lane, sid)
            replacement = turn_of(lane, held, text='replacement')
        finally:
            release.set()
            thread.join(BOUND_S)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(held['state'], 'stop_pending', 'None before the launch returned proves nothing')
        self.assertEqual((replacement['code'], replacement['detail']), ('busy', 'stop_pending'))
        self.assertEqual(work_states(lane), [('launch', 'done'), ('stop', 'pending')])
        self.assertEqual(lane.sessions.run_pending(), 1)         # asked again: the run is registered, and stopped
        settled = session_of(lane, sid)
        self.assertEqual((settled['state'], settled['worker']), ('idle', None))
        self.assertEqual([order.run_id for order in lane.units.launched], [first_run])
        self.assertEqual(lane.units.stops, [first_run, first_run])

    def test_an_absence_seen_before_a_launch_ended_settles_nothing_even_when_read_after_it(self):
        """The Stop looks while the launch is inside the port and sees no run; the launch then registers its run
        and ends; only then is the Stop's answer read. That answer is stale: the session stays stop_pending, no
        replacement opens, and the next Stop finds the run and stops it."""
        lane = Lane()
        session = lane.start(engine='opencode', prompt='first')['session']
        sid, first_run = session['session_id'], session['worker']['run_id']
        lane.sessions.engine_session_reported(sid, 'engine-conversation-1')
        entered, release, errors = threading.Event(), threading.Event(), []
        real_launch, real_stop = lane.units.launch, lane.units.stop

        def launch(order):
            entered.set()
            if not release.wait(BOUND_S):
                raise AssertionError('the launch was never released')
            real_launch(order)

        def carry():
            try:
                lane.sessions.run_pending()
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
                errors.append(exc)

        thread = threading.Thread(target=carry)

        def stop(run_id):
            seen = real_stop(run_id)                      # the launch has not registered its run yet
            release.set()
            thread.join(BOUND_S)                          # the launch registers its run and ends ...
            self.assertIn(run_id, lane.units.registered)
            return seen                                   # ... and only now is the absence read

        lane.units.launch = launch
        thread.start()
        try:
            self.assertTrue(entered.wait(BOUND_S))
            lane.units.stop = stop
            lane.sessions.request_stop(sid)
            self.assertEqual(lane.sessions.run_pending(), 1)
        finally:
            release.set()
            thread.join(BOUND_S)
        lane.units.stop = real_stop
        self.assertEqual(errors, [])
        held = session_of(lane, sid)
        self.assertEqual(held['state'], 'stop_pending', 'an absence seen before the launch ended is stale')
        replacement = turn_of(lane, held, text='replacement')
        self.assertEqual((replacement['code'], replacement['detail']), ('busy', 'stop_pending'))
        self.assertEqual(lane.sessions.run_pending(), 1)          # asked again: the run is there, and stopped
        self.assertEqual(session_of(lane, sid)['state'], 'idle')
        self.assertEqual([order.run_id for order in lane.units.launched], [first_run])
        self.assertEqual(lane.units.stops, [first_run, first_run])

    def test_a_launch_not_yet_attempted_is_never_attempted_once_a_stop_is_asked(self):
        lane = Lane()
        session = lane.start(prompt='first')['session']
        lane.sessions.request_stop(session['session_id'])
        self.assertEqual(lane.sessions.run_pending(), 1, 'the launch fails at its claim; only the Stop is tried')
        self.assertEqual(lane.units.launched, [])
        with lane.store.transaction() as tx:
            self.assertEqual([(operation['kind'], operation['state'], operation['outcome'])
                              for operation in store.work(tx)],
                             [('launch', 'failed', {'code': 'stop_requested'}), ('stop', 'done', {'answer': 'ended'})])
        settled = session_of(lane, session['session_id'])
        self.assertEqual((settled['state'], settled['turns'][0]['result']['code']), ('idle', 'stopped_before_start'))
        self.assertEqual(only_delivery(lane, session['session_id'])['state'], 'outcome_unknown')


    def test_an_absence_after_a_launch_returned_settles_nothing(self):
        """DeepSeek round 3, F1: a launch may hand its input over and register its run only after it returned
        (ports.py). A Stop that then finds no run of it settles nothing: the session stays stop_pending, no
        replacement opens, and the Stop is asked until the run is there and stopped."""
        lane = Lane()
        session = lane.start(prompt='first')['session']
        sid, run_id = session['session_id'], session['worker']['run_id']
        late, real_launch = [], lane.units.launch

        def launch(order):
            real_launch(order)
            lane.units.registered.discard(order.run_id)        # handed over; the run is registered later
            late.append(order.run_id)

        lane.units.launch = launch
        lane.settle()
        self.assertEqual((work_states(lane), late), ([('launch', 'done')], [run_id]))
        lane.sessions.request_stop(sid)
        self.assertEqual(lane.sessions.run_pending(), 1)          # the Stop: no run registered yet
        held = session_of(lane, sid)
        self.assertEqual(held['state'], 'stop_pending', 'an absence after a launch returned is not an end')
        self.assertEqual(held['worker']['run_id'], run_id)
        replacement = turn_of(lane, held, text='replacement')
        self.assertEqual((replacement['code'], replacement['detail']), ('busy', 'stop_pending'))
        lane.units.registered.update(late)                        # the run is registered now
        self.assertEqual(lane.sessions.run_pending(), 1)          # asked again: there, and stopped
        settled = session_of(lane, sid)
        self.assertEqual((settled['state'], settled['worker']), ('idle', None))
        self.assertEqual(lane.units.stops, [run_id, run_id])
        self.assertEqual([order.run_id for order in lane.units.launched], [run_id])

    def test_an_absence_after_a_launch_that_raised_settles_nothing(self):
        """DeepSeek round 4, F1r (Codex astra B1, sol R4 on #415): a launch hands its input over, so its unit runs,
        then raises before its run is registered. Nobody can vouch for that outcome, so a Stop that finds no run
        settles nothing: the session stays stop_pending, no replacement turn opens beside the unit, and the Stop
        is asked until the run is there and stopped."""
        lane = Lane()
        session = lane.start(engine='opencode', prompt='first')['session']
        sid, run_id = session['session_id'], session['worker']['run_id']
        lane.sessions.engine_session_reported(sid, 'engine-conversation-1')     # a replacement could resume
        late, real_launch = [], lane.units.launch

        def launch(order):
            real_launch(order)                                 # handed over: the unit runs
            lane.units.registered.discard(order.run_id)        # its registry write lands later
            late.append(order.run_id)
            raise OSError('the pipe broke after the hand-over')

        lane.units.launch = launch
        with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
            self.assertEqual(lane.sessions.run_pending(), 1)  # the launch: outcome unknown, a Stop asked
        self.assertEqual(work_states(lane), [('launch', 'outcome_unknown'), ('stop', 'pending')])
        self.assertEqual(lane.sessions.run_pending(), 1)      # the Stop: no run registered yet
        held = session_of(lane, sid)
        self.assertEqual(held['state'], 'stop_pending', 'an absence after an unknown launch is not an end')
        self.assertEqual(held['worker']['run_id'], run_id)
        replacement = turn_of(lane, held, text='replacement')
        self.assertEqual((replacement['code'], replacement['detail']), ('busy', 'stop_pending'))
        lane.units.registered.update(late)                    # the run is registered now
        self.assertEqual(lane.sessions.run_pending(), 1)      # asked again: there, and stopped
        settled = session_of(lane, sid)
        self.assertEqual((settled['state'], settled['worker']), ('idle', None))
        self.assertEqual(lane.units.stops, [run_id, run_id])
        self.assertEqual([order.run_id for order in lane.units.launched], [run_id], 'one unit, ever')

    def test_an_open_launch_of_another_run_does_not_hold_this_run_s_stop(self):
        """DeepSeek round 3, F7 (m08): only a launch of the Stop's own run is read in its claim. The first session's
        launch was still pending when the daemon restarted, so it definitely never started and its Stop may settle
        on an absence."""
        first_lane = Lane()
        first = first_lane.start(prompt='first')['session']
        lane = first_lane.restarted()                             # the launch never attempted; a recovery Stop
        second = lane.start(prompt='second', command_id='cmd-start-0002')['session']
        self.assertEqual(work_states(lane), [('launch', 'outcome_unknown'), ('stop', 'pending'), ('launch', 'pending')])
        seen = []
        real_stop = lane.units.stop

        def stop(run_id):
            with lane.store.transaction() as tx:
                seen.append([(operation['kind'], operation['state']) for operation in store.work(tx)])
            return real_stop(run_id)

        lane.units.stop = stop
        self.assertEqual(lane.sessions.run_pending(), 2)          # the Stop first, then the other launch
        self.assertEqual(seen, [[('launch', 'outcome_unknown'), ('stop', 'attempting'), ('launch', 'pending')]])
        settled = session_of(lane, first['session_id'])
        self.assertEqual((settled['state'], settled['worker']), ('idle', None),
                         "another run's open launch does not keep this Stop pending")
        self.assertEqual(lane.units.stops, [first['worker']['run_id']])
        self.assertEqual([order.run_id for order in lane.units.launched], [second['worker']['run_id']])

    def test_a_stop_of_a_run_that_is_not_the_session_s_unit_settles_nothing(self):
        """DeepSeek round 3, F7 (m09): a confirmed Stop of another run leaves this session as it was."""
        lane = Lane()
        session = lane.start(prompt='first')['session']
        sid, run_id = session['session_id'], session['worker']['run_id']
        lane.settle()                                             # launched, and its run registered
        lane.units.stop_answers = [stop_answer(run_id, 'pending')]
        lane.sessions.request_stop(sid)
        self.assertEqual(lane.sessions.run_pending(), 1)          # its own Stop: pending
        other = lane.minter('run-')
        with lane.store.transaction() as tx:
            store.insert_work(tx, lane.minter('op-'), sid, 'stop', {'run_id': other})
        lane.units.stop_answers = [stop_answer(run_id, 'pending'), stop_answer(other, 'stopped')]
        self.assertEqual(lane.sessions.run_pending(), 2)
        self.assertEqual(lane.units.stops, [run_id, run_id, other])
        self.assertEqual(work_states(lane), [('launch', 'done'), ('stop', 'pending'), ('stop', 'done')])
        held = session_of(lane, sid)
        self.assertEqual((held['state'], held['worker']['run_id']), ('stop_pending', run_id),
                         "another run's confirmed Stop settles nothing here")


class FreshLaunchTest(unittest.TestCase):
    """A session whose launches never started has no conversation to lose: its next turn launches afresh."""

    def test_a_new_turn_after_a_first_launch_that_never_started_launches_afresh(self):
        lane = Lane()
        lane.units.then = [UnitRefused('engine_missing', 'no such binary'), UnitRefused('engine_missing', 'still')]
        session = lane.start(prompt='first')['session']
        lane.settle()
        idle = session_of(lane, session['session_id'])
        again = turn_of(lane, idle, text='again')
        self.assertEqual(again['code'], 'ok', again)
        lane.settle()                                             # refused once more: still nothing started
        idle = session_of(lane, session['session_id'])
        lane.call('session.queue', {'command_id': 'cmd-queue-0001', 'session_id': idle['session_id'], 'text': 'queued'})
        lane.settle()
        self.assertEqual([(order.text, order.resume) for order in lane.units.launched], [('queued', None)])
        after = session_of(lane, session['session_id'])
        self.assertEqual([turn['state'] for turn in after['turns']], ['failed', 'failed', 'queued'])
        self.assertEqual(len({turn['run_id'] for turn in after['turns']}), 3, 'a new run each time')
        self.assertEqual(work_states(lane), [('launch', 'failed'), ('launch', 'failed'), ('launch', 'done')])

    def test_after_a_launch_whose_outcome_is_unknown_the_next_turn_is_not_a_fresh_launch(self):
        lane = Lane()
        lane.units.then = [RuntimeError('the pipe broke')]
        session = lane.start(prompt='first')['session']
        # the Stop's own word that the run ended: after an attempt nobody can vouch for, an absence proves nothing
        lane.units.stop_answers = [stop_answer(session['worker']['run_id'], 'ended')]
        with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
            lane.settle()
        idle = session_of(lane, session['session_id'])
        self.assertEqual(idle['state'], 'idle')
        refused = turn_of(lane, idle)
        self.assertEqual((refused['code'], refused['detail']), ('unsupported', 'not_implemented'))
        self.assertEqual(lane.units.launched, [])

    def test_a_refused_launch_then_one_whose_outcome_is_unknown_is_not_followed_by_a_fresh_launch(self):
        lane = Lane()
        lane.units.then = [UnitRefused('engine_missing'), RuntimeError('the pipe broke')]
        session = lane.start(prompt='first')['session']
        lane.settle()
        self.assertEqual(turn_of(lane, session_of(lane, session['session_id']), text='again')['code'], 'ok')
        # the Stop's own word that the run ended: after an attempt nobody can vouch for, an absence proves nothing
        lane.units.stop_answers = [stop_answer(session_of(lane, session['session_id'])['worker']['run_id'], 'ended')]
        with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
            lane.settle()
        idle = session_of(lane, session['session_id'])
        self.assertEqual([turn['state'] for turn in idle['turns']], ['failed', 'failed'])
        refused = turn_of(lane, idle, command_id='cmd-turn-0002')
        self.assertEqual((refused['code'], refused['detail']), ('unsupported', 'not_implemented'))
        self.assertEqual(lane.units.launched, [])

    def test_once_the_engine_named_its_conversation_the_next_turn_is_not_a_fresh_launch(self):
        lane = Lane()
        lane.units.then = [UnitRefused('engine_missing')]
        session = lane.start(prompt='first')['session']
        lane.settle()
        lane.sessions.engine_session_reported(session['session_id'], 'engine-conversation-1')
        refused = turn_of(lane, session_of(lane, session['session_id']))
        self.assertEqual((refused['code'], refused['detail']), ('unsupported', 'not_implemented'))


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

    def test_a_steer_queued_before_its_engine_lost_typed_acceptance_is_held(self):
        live = self.running('codex')
        item = self.lane.call('session.steer', {'command_id': 'cmd-steer-0001', 'session_id': live['session_id'],
                                                'text': 'skip the docs', 'expected_turn_id': live['active_turn_id']})
        self.lane.drivers.by_engine['codex']['input_ack'] = False
        self.lane.settle()
        self.assertEqual(self.lane.units.sent, [], 'not sent into the turn in progress')
        self.lane.sessions.turn_finished(live['session_id'], live['active_turn_id'], outcome='done', result=DONE,
                                         worker_alive=True)
        self.lane.settle()
        self.assertEqual(self.lane.units.sent, [], 'nor turned into the next turn')
        with self.lane.store.transaction() as tx:
            self.assertEqual(store.load_item(tx, item['item']['item_id'])['state'], 'queued')

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

    def test_a_driver_that_fails_for_one_session_does_not_end_the_pass_for_the_others(self):
        """DeepSeek round 3, F5: a port that raises for one session is logged, that session keeps its queue, and the
        sessions after it in the same pass are still dispatched."""
        first = self.lane.start(engine='claude_code', command_id='cmd-start-0001')['session']
        second = self.lane.start(engine='codex', command_id='cmd-start-0002')['session']
        self.assertLess(first['session_id'], second['session_id'])     # the pass reaches the failing one first
        items = [self.lane.call('session.queue', {'command_id': f'cmd-queue-000{number}', 'session_id': sid,
                                                  'text': 'queued'})['item']
                 for number, sid in enumerate((first['session_id'], second['session_id']), 1)]
        real = self.lane.drivers.declaration

        def declaration(engine):
            if engine == 'claude_code':
                raise RuntimeError('declaration failed')
            return real(engine)

        self.lane.drivers.declaration = declaration
        with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
            self.assertEqual(self.lane.sessions.dispatch_queued(), 1)
        with self.lane.store.transaction() as tx:
            self.assertEqual([store.load_item(tx, item['item_id'])['state'] for item in items],
                             ['queued', 'delivering'])
        self.assertEqual(session_of(self.lane, first['session_id'])['turns'], [], 'the failed one wrote nothing')

    def test_a_steer_put_into_the_turn_in_progress_counts_as_started(self):
        """DeepSeek round 4, N2: dispatch_queued counts a mid-turn steer it started, and only once."""
        live = self.running('codex')
        self.lane.call('session.steer', {'command_id': 'cmd-steer-0001', 'session_id': live['session_id'],
                                         'text': 'skip the docs', 'expected_turn_id': live['active_turn_id']})
        self.assertEqual(self.lane.sessions.dispatch_queued(), 1)
        self.assertEqual(self.lane.sessions.dispatch_queued(), 0, 'one delivery in flight at a time')

    def test_a_launch_order_carries_the_tenant_the_principal_and_the_budget(self):
        answer = self.lane.call('session.start', {'command_id': 'cmd-start-0001', 'tenant': 't9',
                                                  'engine': 'claude_code', 'prompt': 'first',
                                                  'budget': {'cap_usd': 12.5}})
        self.assertEqual(answer['code'], 'ok')
        self.lane.settle()
        order = self.lane.units.launched[0]
        self.assertEqual((order.tenant, order.principal, order.budget),
                         ('t9', {'kind': 'owner', 'id': 'local'}, {'cap_usd': 12.5}))

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
        # the Stop's own word that the run ended: after an attempt nobody can vouch for, an absence proves nothing
        self.lane.units.stop_answers = [stop_answer(session['worker']['run_id'], 'ended')]
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


class GuardTest(unittest.TestCase):
    """Each rule of the store and the machine that the paths above never break, broken on purpose."""

    def setUp(self):
        self.lane = Lane()

    def idle(self, engine='codex'):
        return self.lane.start(engine=engine)['session']

    def save(self, before, after):
        with self.lane.store.transaction() as tx:
            store.save(tx, before, after)

    def refused_rule(self, write):
        with self.assertRaises(records.RecordError) as caught:
            write()
        return caught.exception.rule

    def test_a_session_s_identity_never_changes(self):
        before = self.idle()
        for key, value in (('session_id', 'ses-other'), ('tenant', 't2'), ('created_at', '2026-11-14T10:00:00.000Z'),
                           ('principal', {'kind': 'internal', 'id': 'other'})):
            with self.subTest(key=key):
                after = {**copy.deepcopy(before), key: value, 'revision': before['revision'] + 1}
                self.assertEqual(self.refused_rule(lambda after=after: self.save(before, after)), 'immutable')

    def test_a_revision_rises_by_exactly_one(self):
        before = self.idle()
        for revision in (before['revision'], before['revision'] + 2):
            with self.subTest(revision=revision):
                after = {**copy.deepcopy(before), 'engine_session_id': 'e', 'revision': revision}
                self.assertEqual(self.refused_rule(lambda after=after: self.save(before, after)), 'rises_by_one')

    def test_turns_are_only_ever_added_to(self):
        session = self.lane.start(engine='codex', prompt='first')['session']
        self.lane.settle()
        self.lane.accept_input(session['session_id'])
        live = session_of(self.lane, session['session_id'])
        before = self.lane.sessions.turn_finished(live['session_id'], live['active_turn_id'], outcome='done',
                                                  result=DONE, worker_alive=False)
        after = {**copy.deepcopy(before), 'turns': [], 'revision': before['revision'] + 1}
        self.assertEqual(self.refused_rule(lambda: self.save(before, after)), 'append_only')

    def test_a_session_written_from_an_old_read_changes_nothing(self):
        before = self.idle()
        self.save(before, machine.engine_session(before, 'one'))
        with self.assertRaises(store.Stale):
            self.save(before, machine.engine_session(before, 'two'))
        self.assertEqual(session_of(self.lane, before['session_id'])['engine_session_id'], 'one')

    def test_the_engine_s_conversation_id_is_set_once(self):
        named = machine.engine_session(self.idle(), 'one')
        for other, code in (('two', 'engine_session_id_changed'), ('one', 'unchanged')):
            with self.subTest(other=other):
                with self.assertRaises(machine.Refused) as caught:
                    machine.engine_session(named, other)
                self.assertEqual(caught.exception.code, code)

    def queued(self):
        session = self.idle()
        return self.lane.call('session.queue', {'command_id': 'cmd-guard-q001', 'session_id': session['session_id'],
                                                'text': 'original'})['item']

    def save_item(self, before, after):
        with self.lane.store.transaction() as tx:
            store.save_item(tx, before, after)

    def test_an_item_s_identity_and_place_never_change(self):
        before = self.queued()
        for key, value in (('item_id', 'qi-other'), ('session_id', 'ses-other'), ('order', 2),
                           ('created_at', '2026-11-14T10:00:00.000Z')):
            with self.subTest(key=key):
                after = {**before, key: value, 'revision': before['revision'] + 1}
                self.assertEqual(self.refused_rule(lambda after=after: self.save_item(before, after)), 'immutable')

    def test_a_final_item_never_changes(self):
        item = self.queued()
        with self.lane.store.transaction() as tx:
            before = delivery.cancel(tx, item['item_id'])
        after = {**before, 'text': 'changed after it was withdrawn'}
        self.assertEqual(self.refused_rule(lambda: self.save_item(before, after)), 'final')

    def test_an_item_written_from_an_old_read_changes_nothing(self):
        before = self.queued()
        with self.lane.store.transaction() as tx:
            delivery.edit(tx, before['item_id'], {'text': 'one'})
        with self.assertRaises(store.Stale):
            self.save_item(before, {**before, 'text': 'two', 'revision': before['revision'] + 1})
        with self.lane.store.transaction() as tx:
            self.assertEqual(store.load_item(tx, before['item_id'])['text'], 'one')

    def test_a_live_worker_that_takes_one_turn_is_never_sent_another(self):
        session = self.lane.start(engine='codex', prompt='first')['session']
        self.lane.settle()
        self.lane.accept_input(session['session_id'])
        live = session_of(self.lane, session['session_id'])
        idle = self.lane.sessions.turn_finished(live['session_id'], live['active_turn_id'], outcome='done',
                                                result=DONE, worker_alive=True)
        self.lane.drivers.by_engine['codex']['flags']['multi_turn'] = False
        answer = turn_of(self.lane, idle)
        self.assertEqual((answer['code'], answer['detail']), ('busy', 'worker_alive'))
        self.assertEqual(self.lane.units.sent, [])

    def test_an_ended_session_opens_no_turn(self):
        before = self.idle()
        ended = {**copy.deepcopy(before), 'state': 'ended', 'revision': before['revision'] + 1}
        self.save(before, ended)
        self.assertEqual(machine.turn_refusal(ended), ('conflict', 'session_ended'))
        answer = turn_of(self.lane, ended)
        self.assertEqual((answer['code'], answer['detail']), ('conflict', 'stale_revision'))


    def test_a_binding_that_is_not_an_object_is_refused_as_one(self):
        """DeepSeek round 4, N3."""
        for binding in (None, 'dlv-a', ['dlv-a']):
            with self.subTest(binding=binding):
                def write(binding=binding):
                    with self.lane.store.transaction() as tx:
                        store.insert_delivery(tx, binding, None)
                with self.assertRaises(records.RecordError) as caught:
                    write()
                self.assertEqual((caught.exception.path, caught.exception.rule), ('', 'object'))

    def test_a_binding_the_schema_refuses_is_never_written(self):
        """DeepSeek round 3, F2: the store checks a binding before it writes it, as store.py says of every record."""
        session = self.idle()
        good = {'delivery_id': 'dlv-a', 'session_id': session['session_id'], 'run_id': 'run-a',
                'worker_incarnation': 'run-a.1', 'target_turn_id': 'turn-a', 'how': 'new_turn'}

        def write(binding, item_id):
            with self.lane.store.transaction() as tx:
                store.insert_delivery(tx, binding, item_id)

        for binding, item_id, rule in (({**good, 'delivery_id': 'nope'}, None, 'pattern'),
                                       ({**good, 'how': 'sideways'}, None, 'enum'),
                                       ({**good, 'item_id': 'qi-a', 'run_id': 'run a'}, 'qi-a', 'pattern'),
                                       ({**good, 'item_id': 'qi-a'}, 'qi-b', 'this_item')):
            with self.subTest(binding=binding, item_id=item_id):
                self.assertEqual(self.refused_rule(lambda binding=binding, item_id=item_id: write(binding, item_id)),
                                 rule)
        with self.lane.store.transaction() as tx:
            self.assertEqual(store.deliveries(tx), [])
        write(good, None)                                          # the control: a whole binding is written
        with self.lane.store.transaction() as tx:
            self.assertEqual([row['delivery_id'] for row in store.deliveries(tx)], ['dlv-a'])


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
        self.assertFalse(lane.service.ready, 'a stopped service is not ready')

    def test_two_starts_at_once_run_one_loop(self):
        for _ in range(20):
            lane = Lane()
            lane.service.interval_s = BOUND_S
            barrier = threading.Barrier(2, timeout=BOUND_S)
            before = sum(thread.name == 'sessions-service' for thread in threading.enumerate())

            def start(lane=lane, barrier=barrier):
                barrier.wait()
                lane.service.start(None)

            starters = [threading.Thread(target=start) for _ in range(2)]
            for starter in starters:
                starter.start()
            for starter in starters:
                starter.join(BOUND_S)
            running = sum(thread.name == 'sessions-service' for thread in threading.enumerate())
            lane.service.stop(None)
            self.assertEqual(running - before, 1)

    def test_a_start_during_a_stop_waits_until_the_old_loop_has_joined(self):
        """Stop holds the service's lock until its loop has joined, so a start made meanwhile finds the lock held
        and starts its loop only after the old one is gone: never two loops at once."""
        lane = Lane()
        in_recovery, release, joining, attempted = (threading.Event() for _ in range(4))
        found_held = []
        real_recover = lane.sessions.recover

        def recover():
            in_recovery.set()
            if not release.wait(BOUND_S):
                raise AssertionError('recovery was never released')
            real_recover()

        class WatchedLock:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if threading.current_thread().name == 'sessions-test-starter':
                    found_held.append(self.lock.locked())
                    attempted.set()
                self.lock.acquire()
                return self

            def __exit__(self, *exc):
                self.lock.release()

        lane.sessions.recover = recover
        lane.service.interval_s = BOUND_S
        lane.service.start(None)
        self.assertTrue(in_recovery.wait(BOUND_S))
        old = lane.service._thread
        real_join = old.join

        def join(timeout=None):
            joining.set()
            real_join(timeout)

        old.join = join
        lane.service._lock = WatchedLock()
        stopper = threading.Thread(target=lane.service.stop, args=(None,), daemon=True)
        starter = threading.Thread(target=lane.service.start, args=(None,), name='sessions-test-starter', daemon=True)
        stopper.start()
        try:
            self.assertTrue(joining.wait(BOUND_S))             # the stop is joining the old loop, still recovering
            starter.start()
            self.assertTrue(attempted.wait(BOUND_S))
        finally:
            release.set()
            stopper.join(BOUND_S)
            starter.join(BOUND_S)
            if found_held != [True]:                           # whatever went wrong, leave no loop running
                lane.service._stopping.set()
                lane.sessions.wake.set()
        self.assertEqual(found_held, [True], 'the start found the lifecycle held by the stop')
        self.assertFalse(old.is_alive())
        self.assertIsNot(lane.service._thread, old)
        lane.service.stop(None)

    def test_a_crash_of_a_pass_leaves_the_service_not_ready_with_its_failure_kept(self):
        lane = Lane()
        recovered = threading.Event()
        real_recover = lane.sessions.recover

        def recover():
            real_recover()
            recovered.set()

        lane.sessions.recover = recover
        lane.units.then = ['cut-before']                 # the launch raises a BaseException on the service's thread
        lane.service.interval_s = BOUND_S
        lane.service.start(None)
        try:
            self.assertTrue(recovered.wait(BOUND_S))
            with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
                self.assertEqual(lane.start(prompt='first')['code'], 'ok')
                lane.service._thread.join(BOUND_S)
            self.assertFalse(lane.service._thread.is_alive())
            self.assertFalse(lane.service.ready)
            self.assertIsInstance(lane.service.failure, Cut)
        finally:
            lane.service.stop(None)

    def test_a_start_after_the_loop_died_starts_a_fresh_loop(self):
        """DeepSeek round 3, F3: a start finds the dead loop's thread gone and starts a new one; the old failure is
        not the new loop's."""
        lane = Lane()
        recovered = threading.Event()
        real_recover = lane.sessions.recover

        def recover():
            real_recover()
            recovered.set()

        lane.sessions.recover = recover
        lane.units.then = ['cut-before']
        lane.service.interval_s = BOUND_S
        lane.service.start(None)
        try:
            self.assertTrue(recovered.wait(BOUND_S))
            with self.assertLogs('recursion_core.sessions.service', 'ERROR'):
                self.assertEqual(lane.start(prompt='first')['code'], 'ok')
                lane.service._thread.join(BOUND_S)
            dead = lane.service._thread
            self.assertFalse(dead.is_alive())
            self.assertIsInstance(lane.service.failure, Cut)
            recovered.clear()
            lane.service.start(None)
            fresh = lane.service._thread
            self.assertIsNot(fresh, dead, 'a start after the loop died starts a fresh loop')
            self.assertIsNone(lane.service.failure, "the old failure is not the new loop's")
            self.assertTrue(recovered.wait(BOUND_S))
            self.assertTrue(fresh.is_alive())
        finally:
            lane.service.stop(None)
        self.assertIsNone(lane.service._thread)


if __name__ == '__main__':
    unittest.main()
