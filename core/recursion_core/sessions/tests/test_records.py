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
from recursion_core.sessions.tests.support import SCHEMAS, load_json, published_rules, stop_answer

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

    def test_a_turn_id_used_twice_is_refused(self):
        record = self.good()
        record['turns'][1]['turn_id'] = record['turns'][0]['turn_id']
        with self.assertRaises(records.RecordError) as caught:
            records.check_session_kept(record)
        self.assertEqual(caught.exception.rule, 'unique_turn_id')

    def test_an_active_turn_that_is_already_final_is_refused(self):
        record = self.good()
        ended = next(turn for turn in record['turns'] if turn['state'] == 'ended')
        record['active_turn_id'] = ended['turn_id']
        with self.assertRaises(records.RecordError) as caught:
            records.check_session_kept(record)
        self.assertEqual((caught.exception.path, caught.exception.rule), ('/active_turn_id', 'not_final'))

    def test_no_id_pattern_matches_a_value_with_a_line_break(self):
        for pattern, value in ((records.SESSION_ID, 'ses-a'), (records.TURN_ID, 'turn-a'), (records.ITEM_ID, 'qi-a'),
                               (records.DELIVERY_ID, 'dlv-a'), (records.RUN_ID, 'run-a'), (records.TOKEN, 'a.b'),
                               (records.COMMAND_ID, 'cmd-00001'), (records.CODE_WORD, 'code'),
                               (records.ENGINE_NAME, 'codex'), (records.TIMESTAMP, '2026-11-14T09:00:00.000Z')):
            self.assertTrue(records._matches(pattern, value), value)
            for broken in (value + '\n', value + '\r', '\n' + value, value[:2] + '\n' + value[2:]):
                with self.subTest(value=broken):
                    self.assertFalse(records._matches(pattern, broken))

    def test_minted_ids_fit_their_patterns(self):
        for prefix, pattern in (('ses-', records.SESSION_ID), ('turn-', records.TURN_ID), ('qi-', records.ITEM_ID),
                                ('dlv-', records.DELIVERY_ID), ('run-', records.RUN_ID)):
            self.assertTrue(pattern.fullmatch(records.mint(prefix)), prefix)
        self.assertTrue(records.TIMESTAMP.fullmatch(records.timestamp()))


class StopAnswerTest(unittest.TestCase):
    """A Stop settles a session only on a well-formed stop.result.v1 answer (#368, draft 2) that confirms the Stop of
    THIS run (``records.stop_confirmed``). What the hand-written check accepts is checked against the schema itself
    by SPINE's standard-library reader, on every interpreter: an answer settles exactly when the schema admits it,
    its code is 'stopped' or 'ended' and it names the run."""

    RUN = 'run-0001'
    SEEN = {'proof': 'empty', 'cgroup': '/sys/fs/cgroup/made-up.scope', 'cgroup_identity': None,
            'time': '2026-11-14T09:00:00.000Z'}

    def answers(self):
        stopped, ended, pending = (stop_answer(self.RUN, code) for code in ('stopped', 'ended', 'pending'))
        seen = self.SEEN
        return [
            stopped, ended, pending, {key: value for key, value in ended.items() if key != 'claim'},
            {**stopped, 'repeat': True}, {**stopped, 'attestation': seen},
            {**stopped, 'attestation': {**seen, 'proof': 'removed', 'cgroup_identity': {'st_dev': 1, 'st_ino': 2}}},
            {**stopped, 'attestation': {**seen, 'proof': 'removed'}},
            {**stopped, 'attestation': {**seen, 'proof': 'future_proof'}},
            {**stopped, 'attestation': {**seen, 'time': '2026-02-31T00:00:00.000Z'}},
            {**stopped, 'attestation': {**seen, 'cgroup': ''}},
            {**stopped, 'attestation': {**seen, 'extra': 1}},
            {**stopped, 'attestation': {key: value for key, value in seen.items() if key != 'time'}},
            {**stopped, 'attestation': {**seen, 'cgroup_identity': {'st_dev': -1, 'st_ino': 2}}},
            {**stopped, 'attestation': {**seen, 'cgroup_identity': {'st_dev': 1}}},
            {**stopped, 'attestation': {**seen, 'cgroup_identity': {'st_dev': True, 'st_ino': 2}}},
            {**stopped, 'state': 'pending'}, {**ended, 'state': 'stopped'},
            {key: value for key, value in stopped.items() if key != 'repeat'}, {**stopped, 'repeat': 'no'},
            {**stopped, 'reason': 'cgroup_populated'}, {**ended, 'reason': 'cgroup_populated'},
            {**stopped, 'run_id': 'run-other'}, {**stopped, 'run_id': 'bad id'},
            {**stopped, 'code': 'gone', 'state': 'gone'},
            {key: value for key, value in stopped.items() if key != 'state'},
            {**stopped, 'recorded': False}, {**stopped, 'recorded': True},
            {**stopped, 'claim': 'cgroup_empty_whole_run_unverified'},
            {**stopped, 'claim': 'cgroup_removed_whole_run_unverified'},
            {**stopped, 'claim': 'ended_before_stop_removal_unverified'},
            {**ended, 'claim': 'cgroup_empty_whole_run_unverified'}, {**ended, 'repeat': False},
            {**ended, 'attestation': seen}, {**stopped, 'containment_level': 'contained'},
            {**stopped, 'containment_level': 'standard'}, {**stopped, 'fence': {'state': 'fenced', 'incarnation': 1}},
            {**stopped, 'fence': {'state': 'dispatched', 'incarnation': 0}},
            {**stopped, 'fence': {'state': 'open', 'incarnation': 1}}, {**stopped, 'fence': {'state': 'fenced'}},
            {**stopped, 'fence': {'state': 'fenced', 'incarnation': -1}},
            {**stopped, 'fence': {'state': 'fenced', 'incarnation': 1, 'more': 2}}, {**stopped, 'colour': 'red'},
            {'code': 'stopped', 'run_id': self.RUN}, 'stopped', [], 7,
        ]

    def test_an_answer_settles_exactly_when_it_is_a_well_formed_confirmation_of_this_run(self):
        rules = published_rules()
        self.assertIsNotNone(rules, "SPINE's conformance/rules.py (#368) is not in the tree")
        outcomes = {True: 0, False: 0}
        for answer in self.answers():
            confirms = (isinstance(answer, dict) and rules.fits('stop.result.v1.json', answer)
                        and answer['code'] in ('stopped', 'ended') and answer['run_id'] == self.RUN)
            outcomes[confirms] += 1
            with self.subTest(answer=answer):
                self.assertEqual(records.stop_confirmed(answer, self.RUN), answer['code'] if confirms else None)
        self.assertGreaterEqual(min(outcomes.values()), 10, outcomes)

    def test_a_confirmation_of_another_run_settles_nothing(self):
        self.assertIsNone(records.stop_confirmed(stop_answer('run-0002', 'stopped'), self.RUN))
        self.assertEqual(records.stop_confirmed(stop_answer(self.RUN, 'ended'), self.RUN), 'ended')


if __name__ == '__main__':
    unittest.main()
