# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The queue's delivery rule, durable: every queue.delivery vector of #368 walked through the lane's own tables.

Each vector is one item and its steps. The item is put into a FakeStore (with a session to belong to), and
each step is made in a transaction of its own, through ``delivery.step``, exactly as the conformance client
reads the step: an acceptance is read from its input.accepted event and from nothing else, a write into a turn
from its steer.sent event. After each step the stored item must be the vector's item, a refusal must carry the
vector's code and change nothing (not the item, not its binding, not the record of the write), and kept
evidence must be the vector's. The event the step appended, when the vector shows one that the rule writes,
must be the vector's in full: log, run, turn, type, source, trust and payload. Five events are filed
differently, each named in ``ENVELOPE_EXCEPTIONS`` with its reason; a named one that stops occurring fails too.
"""
from __future__ import annotations

import os
import unittest

from recursion_core.sessions import delivery, machine, records, store, tables
from recursion_core.sessions.tests.support import SCHEMAS, FakeStore, load_json

VECTORS = SCHEMAS / 'vectors' / 'queue.delivery'
ENVELOPE = ('session_id', 'run_id', 'turn_id', 'type', 'source', 'trust', 'payload')
WRITTEN_BY_THE_RULE = ('steer.queued', 'steer.sent', 'effect.outcome_unknown', 'input.accepted')

# The events this lane files otherwise than the vector shows them: (vector, step) -> {member: (the vector's value,
# this lane's value)}. Reported to SPINE (questions.md Q8); the vectors are SPINE's and are not edited here.
# - d02, d03, d04, d12: an effect.outcome_unknown is filed under the turn the delivery's BINDING aims at
#   (turn-000d), where the vector files it under the turn before (turn-000c). The binding is what the effect was
#   aimed at, so this lane keeps its turn.
# - d07 step 1: the walk's session is idle (it has no unit and no turn in progress), so a queue edit is filed
#   with no run and no turn; the vector's log is of a live session. And the core writes it ('core'), where the
#   vector shows the client's own write ('client'); event.v1 admits both.
_UNKNOWN_TURN = {'turn_id': ('turn-000c', 'turn-000d')}
ENVELOPE_EXCEPTIONS = {
    ('d02', 2): _UNKNOWN_TURN, ('d03', 3): _UNKNOWN_TURN, ('d04', 3): _UNKNOWN_TURN, ('d12', 4): _UNKNOWN_TURN,
    ('d07', 1): {'run_id': ('run-5b1f0c2d9a7e4f36b8c1d2e3f4a5b6c7', None), 'turn_id': ('turn-000c', None),
                 'source': ('client', 'core')},
}


def facts_of(step: dict) -> dict:
    """What the rule is told by a step, read as conformance/wire_client.py reads it."""
    happening, facts, event = step['happening'], step.get('facts'), step.get('event')
    if happening == 'accepted':
        return {'delivery_id': event['payload']['delivery_id'], 'session_id': event['session_id'],
                'run_id': event['run_id'], 'turn_id': event['payload']['turn_id'], 'how': event['payload']['how']}
    if happening == 'written' and event is not None:
        return {'delivery_id': event['payload']['delivery_id'], 'item_id': event['payload']['item_id'],
                'session_id': event['session_id'], 'run_id': event['run_id'], 'turn_id': event['turn_id']}
    return facts or {}


def session_for(session_id: str) -> dict:
    return machine.new_session(session_id=session_id, created_at='2026-11-13T07:30:00.000Z', tenant='t1',
                               principal={'kind': 'owner', 'id': 'local'},
                               route={'engine': 'codex', 'model': None,
                                      'connection': {'kind': 'subscription', 'id': 'made-up-login'}},
                               workspace=None, budget={'cap_usd': 0})


class VectorTest(unittest.TestCase):

    def snapshot(self, db, item_id):
        with db.transaction() as tx:
            item = store.load_item(tx, item_id)
            found = [row for row in store.deliveries(tx) if row['item_id'] == item_id]
        return item, found

    def walk(self, name: str) -> None:
        vector = load_json(VECTORS / name)
        met = set()
        db = FakeStore()
        tables.declare(db)
        item = vector['item']
        with db.transaction() as tx:
            store.insert_session(tx, session_for(item['session_id']))
            store.insert_item(tx, item)
        for number, step in enumerate(vector['steps'], 1):
            with self.subTest(step=number, happening=step['happening']):
                before_item, before_rows = self.snapshot(db, item['item_id'])
                events_before = len(db.events)
                refused = None
                with db.transaction() as tx:
                    try:
                        delivery.step(tx, item['item_id'], step['happening'], facts_of(step))
                    except delivery.Refused as refusal:
                        refused = refusal.code
                after_item, after_rows = self.snapshot(db, item['item_id'])
                if 'refused' in step:
                    self.assertEqual(refused, step['refused'])
                    self.assertEqual(after_item, before_item, 'a refusal changed the item')
                    strip = [{key: value for key, value in row.items() if key != 'evidence'} for row in after_rows]
                    self.assertEqual(strip, [{key: value for key, value in row.items() if key != 'evidence'}
                                             for row in before_rows], 'a refusal changed the binding or the write')
                else:
                    self.assertIsNone(refused, f'refused {refused!r}; the vector takes the step')
                    self.assertEqual(after_item, step['item'])
                if 'evidence' in step:
                    self.assertEqual(len(after_rows), 1)
                    self.assertEqual(after_rows[0]['evidence'], step['evidence'])
                appended = db.events[events_before:]
                shown = step.get('event')
                # A refused write's steer.sent is what the step OBSERVED, not something the rule wrote.
                if shown is not None and 'refused' not in step and shown['type'] in WRITTEN_BY_THE_RULE:
                    self.assertEqual(len(appended), 1, appended)
                    ours = {key: appended[0][key] for key in ENVELOPE}
                    theirs = {key: shown[key] for key in ENVELOPE}
                    for key, (their_value, our_value) in ENVELOPE_EXCEPTIONS.get((name[:3], number), {}).items():
                        self.assertEqual((theirs.pop(key), ours.pop(key)), (their_value, our_value), key)
                        met.add((name[:3], number))
                    self.assertEqual(ours, theirs)
                if refused is not None:
                    self.assertEqual(appended, [], 'a refusal appended an event')
        named = {key for key in ENVELOPE_EXCEPTIONS if key[0] == name[:3]}
        self.assertEqual(met, named, 'a named exception no longer occurs')

    def test_there_are_fourteen_vectors(self):
        self.assertEqual(len([name for name in os.listdir(VECTORS) if name.endswith('.json')]), 14)


def _add(name: str) -> None:
    def test(self) -> None:
        self.walk(name)
    test.__name__ = 'test_' + name[:-5].replace('-', '_')
    test.__doc__ = f'The vector {name}.'
    setattr(VectorTest, test.__name__, test)


for _name in sorted(os.listdir(VECTORS)):
    if _name.endswith('.json'):
        _add(_name)


class DeliveryRuleTest(unittest.TestCase):
    """What the vectors do not show: a turn's own input, a delivery id that is used again, the turn quarantine."""

    def setUp(self):
        self.db = FakeStore()
        tables.declare(self.db)
        self.session_id = 'ses-7f3a9c2e51d04b6a'
        with self.db.transaction() as tx:
            store.insert_session(tx, session_for(self.session_id))

    def binding(self, **change):
        found = {'delivery_id': 'dlv-0001', 'session_id': self.session_id, 'run_id': 'run-1',
                 'worker_incarnation': 'run-1.1', 'target_turn_id': 'turn-0001', 'how': 'new_turn'}
        found.update(change)
        return found

    def test_a_turn_input_is_bound_new_turn_only_and_never_twice(self):
        with self.db.transaction() as tx:
            delivery.begin_input(tx, self.binding())
        for change in ({'how': 'mid_turn', 'delivery_id': 'dlv-0009'}, {'delivery_id': 'dlv-0001'},
                       {'target_turn_id': None, 'delivery_id': 'dlv-0010'}):
            with self.subTest(change=change), self.db.transaction() as tx:
                with self.assertRaises(delivery.Refused) as caught:
                    delivery.begin_input(tx, self.binding(**change))
                self.assertEqual(caught.exception.code, 'binding')

    def test_a_turn_input_is_accepted_member_by_member_and_quarantined_at_its_turns_end(self):
        with self.db.transaction() as tx:
            delivery.begin_input(tx, self.binding())
            delivery.begin_input(tx, self.binding(delivery_id='dlv-0002', target_turn_id='turn-0002'))
        accept = {'delivery_id': 'dlv-0001', 'session_id': self.session_id, 'run_id': 'run-1',
                  'turn_id': 'turn-0001', 'how': 'new_turn'}
        with self.db.transaction() as tx:
            row = store.load_delivery(tx, 'dlv-0001')
            with self.assertRaises(delivery.Refused) as caught:
                delivery.accept(tx, row, {**accept, 'run_id': 'run-2'})
            self.assertEqual(caught.exception.code, 'delivery_mismatch')
            delivery.accept(tx, row, accept)
        with self.db.transaction() as tx:
            self.assertEqual([row['delivery_id'] for row in delivery.quarantine_turn(tx, self.session_id, 'turn-0002')],
                             ['dlv-0002'])
            self.assertEqual(store.load_delivery(tx, 'dlv-0001')['state'], 'delivered')
            with self.assertRaises(delivery.Refused):
                delivery.quarantine_turn(tx, self.session_id, '')

    def test_a_delivery_id_is_never_used_again_even_for_another_item(self):
        with self.db.transaction() as tx:
            session = store.load(tx, self.session_id)
            first = delivery.queue_item(tx, session, item_id='qi-1', text='one', delivery='next_turn',
                                        created_at='2026-11-13T07:30:01.000Z')
            second = delivery.queue_item(tx, session, item_id='qi-2', text='two', delivery='next_turn',
                                         created_at='2026-11-13T07:30:02.000Z')
            self.assertEqual((first['order'], second['order']), (1, 2))
            delivery.begin(tx, 'qi-1', {**self.binding(), 'item_id': 'qi-1'})
        with self.db.transaction() as tx:
            with self.assertRaises(delivery.Refused) as caught:
                delivery.begin(tx, 'qi-2', {**self.binding(), 'item_id': 'qi-2'})
            self.assertEqual(caught.exception.code, 'binding')
            self.assertEqual(store.load_item(tx, 'qi-2')['state'], 'queued')

    def test_a_late_acceptance_is_kept_only_when_the_refusal_is_caught_inside_the_transaction(self):
        with self.db.transaction() as tx:
            delivery.begin_input(tx, self.binding())
            delivery.quarantine_all(tx)
        accept = {'delivery_id': 'dlv-0001', 'session_id': self.session_id, 'run_id': 'run-1',
                  'turn_id': 'turn-0001', 'how': 'new_turn'}
        with self.db.transaction() as tx:
            with self.assertRaises(delivery.Refused):
                delivery.accept(tx, store.load_delivery(tx, 'dlv-0001'), accept)
        with self.db.transaction() as tx:
            row = store.load_delivery(tx, 'dlv-0001')
        self.assertEqual((row['state'], row['evidence']), ('outcome_unknown', [accept]))

    def test_every_event_the_rule_appends_has_the_envelope_the_event_store_takes(self):
        with self.db.transaction() as tx:
            session = store.load(tx, self.session_id)
            delivery.queue_item(tx, session, item_id='qi-1', text='one', delivery='steer',
                                created_at='2026-11-13T07:30:01.000Z')
            delivery.begin(tx, 'qi-1', {**self.binding(how='mid_turn'), 'item_id': 'qi-1'})
            row = store.load_delivery(tx, 'dlv-0001')
            delivery.written(tx, row, {'delivery_id': 'dlv-0001'})
            delivery.quarantine_all(tx)
        self.assertEqual([event['type'] for event in self.db.events],
                         ['steer.queued', 'steer.sent', 'effect.outcome_unknown'])
        for event in self.db.events:
            self.assertEqual(sorted(event), sorted(('session_id', 'run_id', 'turn_id', 'type', 'source', 'trust',
                                                    'payload')))
            self.assertTrue(records.SESSION_ID.fullmatch(event['session_id']))
            self.assertEqual(event['source'], 'core')


if __name__ == '__main__':
    unittest.main()
