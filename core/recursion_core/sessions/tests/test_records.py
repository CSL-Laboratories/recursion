# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The record checks agree with SPINE's schemas on SPINE's own fixtures (#368, merged into this branch).

Every good fixture of session.v1, queue.item.v1 and the delivery binding passes the lane's hand-written check,
and every bad one is refused AT the place the fixture index names (the check's path starts with the index's
``at``), so a fixture cannot be refused by accident for another reason. These run on every interpreter: the
checks are what keeps a record the schema refuses out of the store on the daemon's own interpreter, which has
no JSON Schema library.
"""
from __future__ import annotations

import unittest

from recursion_core.sessions import records
from recursion_core.sessions.tests.support import SCHEMAS, load_json

CHECKS = {
    'session.v1.json': records.check_session,
    'queue.item.v1.json': records.check_item,
    'queue.item.v1.json#/$defs/delivery_binding': records.check_binding,
}


def entries():
    index = load_json(SCHEMAS / 'fixtures' / 'index.json')['fixtures']
    return [entry for entry in index if entry['schema'] in CHECKS]


class FixtureAgreementTest(unittest.TestCase):

    def test_the_fixtures_are_in_the_tree(self):
        found = entries()
        self.assertGreaterEqual(sum(entry['file'].startswith('good/') for entry in found), 17)
        self.assertGreaterEqual(sum(entry['file'].startswith('bad/') for entry in found), 80)

    def test_every_good_fixture_passes(self):
        for entry in entries():
            if not entry['file'].startswith('good/'):
                continue
            with self.subTest(fixture=entry['file']):
                CHECKS[entry['schema']](load_json(SCHEMAS / 'fixtures' / entry['file']))

    def test_every_bad_fixture_is_refused_where_the_index_says(self):
        for entry in entries():
            if not entry['file'].startswith('bad/'):
                continue
            with self.subTest(fixture=entry['file']):
                with self.assertRaises(records.RecordError) as caught:
                    CHECKS[entry['schema']](load_json(SCHEMAS / 'fixtures' / entry['file']))
                at = entry['expect']['at']
                self.assertTrue(caught.exception.path == at or caught.exception.path.startswith(at + '/')
                                or at == '', f'refused at {caught.exception.path!r}, the index says {at!r}')


class LaneRulesTest(unittest.TestCase):
    """What ``check_session_kept`` adds to the schema: order of turns, one open turn, the active turn is real."""

    def good(self):
        return load_json(SCHEMAS / 'fixtures' / 'good' / 'session.v1' / 'h02-live-three-turns-on-one-worker.json')

    def test_the_published_live_session_passes_the_lane_rules(self):
        records.check_session_kept(self.good())

    def test_turns_out_of_order_are_refused(self):
        record = self.good()
        record['turns'][1]['index'] = 5
        with self.assertRaises(records.RecordError) as caught:
            records.check_session_kept(record)
        self.assertEqual(caught.exception.rule, 'in_order')

    def test_an_active_turn_that_is_not_a_turn_of_the_session_is_refused(self):
        record = self.good()
        record['active_turn_id'] = 'turn-nothere'
        with self.assertRaises(records.RecordError) as caught:
            records.check_session_kept(record)
        self.assertEqual(caught.exception.rule, 'a_turn_of_the_session')

    def test_an_open_turn_that_is_not_the_active_one_is_refused(self):
        record = self.good()
        open_turn = next(turn for turn in record['turns'] if turn['state'] == 'running')
        record['turns'][0] = {**record['turns'][0], 'state': 'queued', 'started_at': None, 'ended_at': None,
                              'outcome': None, 'result': None}
        self.assertNotEqual(record['turns'][0]['turn_id'], open_turn['turn_id'])
        with self.assertRaises(records.RecordError) as caught:
            records.check_session_kept(record)
        self.assertEqual(caught.exception.rule, 'only_the_active_turn_is_open')

    def test_a_day_that_does_not_exist_is_refused(self):
        record = self.good()
        record['created_at'] = '2026-02-31T00:00:00.000Z'
        with self.assertRaises(records.RecordError) as caught:
            records.check_session(record)
        self.assertEqual(caught.exception.path, '/created_at')

    def test_minted_ids_fit_their_patterns(self):
        for prefix, pattern in (('ses-', records.SESSION_ID), ('turn-', records.TURN_ID), ('qi-', records.ITEM_ID),
                                ('dlv-', records.DELIVERY_ID), ('run-', records.RUN_ID)):
            self.assertTrue(pattern.fullmatch(records.mint(prefix)), prefix)
        self.assertTrue(records.TIMESTAMP.fullmatch(records.timestamp()))


if __name__ == '__main__':
    unittest.main()
