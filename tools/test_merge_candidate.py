"""Adversarial exact-candidate and bounded admission transition tests."""
from __future__ import annotations

import copy
import itertools
import json
import os
import re
from types import SimpleNamespace as NS
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import merge_candidate as p
import candidate_gate as router

REPO = 'CSL-Laboratories/recursion'
VALUE = {'repo': REPO, 'pr': 7, 'head': 'a'*40, 'base': 'b'*40, 'merge': 'c'*40, 'tree': 'd'*40}
WRITER = {'id': 99, 'login': 'gate-writer', 'type': 'User'}
TIME = '2026-10-16T01:00:00Z'


def comment(kind, record, *, cid=1, user=None):
    return {'id': cid, 'user': user or WRITER, 'created_at': TIME, 'updated_at': TIME,
            'body': p.marker(kind, record)}


def request():
    return comment('request', {'candidate': VALUE, 'key': p.key(VALUE)})


def review():
    return {'id': 10, 'user': p.CODEX.copy(), 'commit_id': VALUE['head'], 'state': 'COMMENTED',
            'submitted_at': '2026-10-16T01:00:01Z',
            'body': 'CODEX-CANDIDATE: ' + p.key(VALUE) + '\nCODEX-GATE: READY'}


def claim(run='50', review_id=10):
    return comment('claim', {'candidate': VALUE, 'key': p.key(VALUE), 'run': run,
                   'review': review_id, 'label': 3}, cid=2, user=p.ACTIONS)


class IdentityTests(unittest.TestCase):
    def fixture(self):
        pr = {'number': 7, 'state': 'open', 'draft': False, 'mergeable': True,
              'head': {'sha': VALUE['head'], 'repo': {'full_name': REPO}},
              'base': {'sha': VALUE['base'], 'repo': {'full_name': REPO}},
              'merge_commit_sha': VALUE['merge']}
        merge = {'sha': VALUE['merge'], 'tree': {'sha': VALUE['tree']},
                 'parents': [{'sha': VALUE['base']}, {'sha': VALUE['head']}]}
        return pr, merge

    def test_live_candidate_requires_exact_two_parents_and_real_base(self):
        pr, merge = self.fixture()
        self.assertEqual(p.candidate(pr, merge, VALUE['base'], REPO), VALUE)
        for mutate in [lambda a,b: a.update(draft=True), lambda a,b: a.update(state='closed'),
                       lambda a,b: a.update(mergeable=None), lambda a,b: b['parents'].reverse(),
                       lambda a,b: b['parents'].pop(), lambda a,b: b.update(sha='e'*40),
                       lambda a,b: a['head']['repo'].update(full_name='fork/repo'),
                       lambda a,b: b['tree'].update(sha='short')]:
            a, b = copy.deepcopy(pr), copy.deepcopy(merge)
            mutate(a, b)
            with self.assertRaises(ValueError):
                p.candidate(a, b, VALUE['base'], REPO)
        with self.assertRaises(ValueError):
            p.candidate(pr, merge, 'e'*40, REPO)

    def test_each_identity_dimension_invalidates_the_key(self):
        for field in ('head', 'base', 'merge', 'tree'):
            self.assertNotEqual(p.key(VALUE), p.key({**VALUE, field: 'e'*40}))
        for field in ('head', 'base', 'merge', 'tree'):
            for bad in ('', 'a'*7, 'A'*40, None, True, 'a'*40+'\n'):
                with self.assertRaises(ValueError):
                    p.key({**VALUE, field: bad})

    def test_request_requires_exact_writer_immutable_body_and_unique_snapshot(self):
        req = request()
        self.assertEqual(p.request_for([req], VALUE, WRITER), req)
        for part, bad in [('id', 100), ('type', 'Bot'), ('login', 'impostor')]:
            fake = copy.deepcopy(req)
            fake['user'][part] = bad
            self.assertIsNone(p.request_for([fake], VALUE, WRITER))
        changed = {**req, 'updated_at': '2026-10-16T01:00:01Z'}
        with self.assertRaises(ValueError):
            p.request_for([changed], VALUE, WRITER)
        for items in ([req, req], [req, {**req, 'id': 2}]):
            with self.assertRaises(ValueError):
                p.request_for(items, VALUE, WRITER)

    def test_latest_authentic_review_transitions_are_fail_closed(self):
        good = review()
        self.assertEqual(p.verified_review([good], request(), VALUE, 10), good)
        for part, bad in [('id', 123), ('login', 'fake'), ('type', 'User')]:
            fake = copy.deepcopy(good)
            fake['user'][part] = bad
            with self.assertRaises(ValueError):
                p.verified_review([fake], request(), VALUE, 10)
        for change in ({'commit_id': 'e'*40}, {'state': 'PENDING'}, {'state': 'DISMISSED'},
                       {'submitted_at': TIME}, {'body': 'CODEX-GATE: READY'},
                       {'body': good['body']+'\nCODEX-GATE: MEDIATE'},
                       {'body': good['body'].replace(p.key(VALUE), 'f'*64)}):
            with self.assertRaises(ValueError):
                p.verified_review([{**good, **change}], request(), VALUE, 10)
        for state, body in itertools.product(['PENDING', 'DISMISSED', 'COMMENTED'],
                                             ['', 'CODEX-GATE: MEDIATE']):
            newer = {**good, 'id': 11, 'state': state, 'body': body}
            with self.assertRaises(ValueError):
                p.verified_review([good, newer], request(), VALUE, 10)
        newer = {**good, 'id': 11, 'submitted_at': '2026-10-16T01:00:02Z'}
        with self.assertRaises(ValueError):
            p.verified_review([good, newer], request(), VALUE, 10)
        self.assertEqual(p.verified_review([good, newer], request(), VALUE, 11), newer)

    def test_submission_order_not_creation_id_and_ambiguous_pending_fail(self):
        good = review()
        late_draft = {**good, 'id': 9, 'state': 'CHANGES_REQUESTED',
                      'submitted_at': '2026-10-16T01:00:02Z', 'body': 'Needs fixing'}
        for other in (late_draft, {**good, 'id': 11},
                      {**good, 'id': 12, 'state': 'PENDING', 'commit_id': None}):
            with self.assertRaises(ValueError):
                p.verified_review([good, other], request(), VALUE)

    def test_ready_requires_an_unquoted_unambiguous_terminal_affirmation(self):
        ready = review()
        body = ready['body']
        for text in ('```text\n' + body + '\n```\nDo not approve.',
                     '~~~\n' + body + '\n~~~',
                     '<!--\n' + body + '\n-->',
                     '<pre>\n' + body + '\n</pre>',
                     '> ' + body.replace('\n', '\n> '),
                     '    ' + body.replace('\n', '\n    '),
                     body + '\nDo not approve.',
                     'Do not approve.\n' + body,
                     body + '\nCODEX-GATE: MEDIATE',
                     body + '\n' + body):
            with self.subTest(text=text), self.assertRaises(ValueError):
                p.verified_review([{**ready, 'body': text}], request(), VALUE)
        self.assertEqual(p.verified_review([{**ready, 'body': body + '\n'}], request(), VALUE)['id'], ready['id'])

    def test_base_advance_cannot_reuse_same_head_review(self):
        newer = {**VALUE, 'base': 'e'*40, 'merge': 'f'*40, 'tree': '1'*40}
        new_request = comment('request', {'candidate': newer, 'key': p.key(newer)})
        with self.assertRaises(ValueError):
            p.verified_review([review()], new_request, newer, 10)


class ReservationTests(unittest.TestCase):
    def test_duplicate_new_run_and_any_candidate_rerun_rejected(self):
        self.assertEqual(p.admission([], VALUE, '50', 1, 10), 'reserve')
        for attempt in (1,):
            self.assertEqual(p.admission([claim()], VALUE, '50', attempt, 10), 'resume')
        self.assertEqual(p.admission([claim()], VALUE, '51', 1, 10), 'duplicate')
        for bad in (0, 2, 3, 99, True, '1'):
            with self.assertRaises(ValueError):
                p.admission([claim()], VALUE, '50', bad, 10)
        with self.assertRaises(ValueError):
            p.admission([claim(), {**claim(), 'id': 3}], VALUE, '50', 1, 10)

    def test_untrusted_claim_cannot_deduplicate_or_admit(self):
        forged = {**claim(), 'user': WRITER}
        self.assertEqual(p.admission([forged], VALUE, '50', 1, 10), 'reserve')
        changed = {**claim(), 'updated_at': '2026-10-17T00:00:00Z'}
        with self.assertRaises(ValueError):
            p.admission([changed], VALUE, '50', 1, 10)

    def test_all_full_platform_outcomes_no_docs_or_subset_success(self):
        for values in itertools.product(['success','failure','cancelled','pending','skipped',''], repeat=3):
            results = dict(zip(('linux','windows','macos'), values))
            self.assertEqual(p.full_results(results), values == ('success',)*3)
        self.assertFalse(p.full_results({'linux': 'success'}))


class SnapshotTests(unittest.TestCase):
    def client(self):
        client = mock.Mock(repo=REPO, token='token')
        client.candidate.return_value = VALUE
        client.gh.return_value = json.dumps(WRITER)
        client.comments.return_value = [request()]
        client.reviews.return_value = [review()]
        client.events.return_value = []
        client.pages.return_value = []
        client.get.return_value = {'number': 7, 'state': 'open', 'draft': False,
                                   'head': {'sha': VALUE['head'], 'repo': {'full_name': REPO}}}
        return client

    def test_old_mediate_cannot_poison_new_ready_even_if_legacy_already_acted(self):
        for new_id in (9, 11):
            client = self.client()
            old = {**review(), 'body': 'CODEX-GATE: MEDIATE'}
            newer = {**review(), 'id': new_id, 'submitted_at': '2026-10-16T01:00:02Z'}
            client.reviews.return_value = [old, newer]
            client.candidate.side_effect = AssertionError('no merge lookup')
            with mock.patch.object(router.gate, 'run_verdict', return_value={'action': 'already-acted'}) as route, \
                    mock.patch.object(router.gate, 'fixer_helpers') as helpers:
                result = router.admit(client, {'review': old, 'pull_request': {'number': 7}}, '', '51', 1)
                self.assertEqual(result.get('duplicate'), 'true')
                self.assertEqual(result['admitted'], 'false')
                self.assertFalse(self.readiness_scheduled(result))
                route.assert_not_called()
                helpers.assert_not_called()
                client.reviews.assert_called()
                client.candidate.assert_not_called()

    def test_latest_mediate_revokes_owned_readiness_even_when_already_acted(self):
        client = self.client()
        old = review()
        latest = {**old, 'id': 11, 'body': 'CODEX-GATE: MEDIATE',
                  'submitted_at': '2026-10-16T01:00:02Z'}
        client.reviews.return_value = [old, latest]
        client.candidate.side_effect = AssertionError('no merge lookup')
        with mock.patch.object(router.gate, 'run_verdict', return_value={'action': 'already-acted'}) as route, \
                mock.patch.object(router.gate, 'fixer_helpers') as helpers:
            result = router.admit(client, {'review': latest, 'pull_request': {'number': 7}}, '', '51', 1)
            self.assertEqual(result['admitted'], 'false')
            self.assertNotEqual(result.get('duplicate'), 'true')
            self.assertTrue(self.readiness_scheduled(result))
            helpers.return_value.revoke_owned_ci.assert_called_once()
            route.assert_called_once()
            client.candidate.assert_not_called()

    @staticmethod
    def readiness_scheduled(result):
        text = (ROOT / '.github/workflows/codex-gate.yml').read_text()
        section = text.split('  candidate-readiness:\n', 1)[1]
        expression = re.search(r'    if: >-\n((?:      .*\n)+)', section)[1]
        expression = ' '.join(expression.split()).replace('&&', ' and ').replace('||', ' or ')
        return eval(expression, {'__builtins__': {}}, {
            'always': lambda: True,
            'github': NS(event_name='pull_request_review', event=NS(review=NS(user=NS(login=p.CODEX['login'])))),
            'needs': NS(verdict=NS(outputs=NS(duplicate=result.get('duplicate', ''))))})

    def test_mediate_ambiguous_or_missing_current_review_never_mutates(self):
        mediate = {**review(), 'body': 'CODEX-GATE: MEDIATE'}
        for records in ([], [mediate, {**mediate, 'id': 11}],
                        [mediate, {**mediate, 'id': 11, 'state': 'PENDING'}],
                        [{**mediate, 'user': WRITER}]):
            client = self.client()
            client.reviews.return_value = records
            with mock.patch.object(router.gate, 'run_verdict') as route, \
                    mock.patch.object(router.gate, 'fixer_helpers') as helpers, self.assertRaises(ValueError):
                router.admit(client, {'review': mediate, 'pull_request': {'number': 7}}, '', '51', 1)
            route.assert_not_called()
            helpers.assert_not_called()
            client.candidate.assert_not_called()

    def test_mediate_uses_live_head_and_skips_ineligible_pr_without_mutations(self):
        mediate = {**review(), 'body': 'CODEX-GATE: MEDIATE'}
        client = self.client()
        newer = {**review(), 'id': 11, 'commit_id': 'e'*40}
        client.reviews.return_value = [mediate, newer]
        client.get.return_value['head']['sha'] = newer['commit_id']
        with mock.patch.object(router.gate, 'run_verdict') as route, \
                mock.patch.object(router.gate, 'fixer_helpers') as helpers:
            result = router.admit(client, {'review': mediate, 'pull_request': {'number': 7}}, '', '51', 1)
            self.assertEqual(result['duplicate'], 'true')
            route.assert_not_called()
            helpers.assert_not_called()
        for change in ({'draft': True}, {'state': 'closed'},
                       {'head': {'sha': VALUE['head'], 'repo': {'full_name': 'fork/repo'}}}):
            client = self.client()
            client.get.return_value.update(change)
            with mock.patch.object(router.gate, 'run_verdict') as route, \
                    mock.patch.object(router.gate, 'fixer_helpers') as helpers:
                result = router.admit(client, {'review': mediate, 'pull_request': {'number': 7}}, '', '51', 1)
                self.assertEqual(result['action'], 'skip-ineligible-pr')
                route.assert_not_called()
                helpers.assert_not_called()

    def test_dismissed_mediate_revokes_but_does_not_dispatch(self):
        client = self.client()
        mediate = {**review(), 'body': 'CODEX-GATE: MEDIATE', 'state': 'DISMISSED'}
        client.reviews.return_value = [mediate]
        with mock.patch.object(router.gate, 'run_verdict') as route, \
                mock.patch.object(router.gate, 'fixer_helpers') as helpers:
            result = router.admit(client, {'review': mediate, 'pull_request': {'number': 7}}, '', '51', 1)
            self.assertEqual(result['action'], 'independent-review-invalid')
            self.assertTrue(self.readiness_scheduled(result))
            helpers.return_value.revoke_owned_ci.assert_called_once()
            route.assert_not_called()

    def test_mediate_routes_without_merge_candidate_or_ready_credentials(self):
        for reason, pat in itertools.product(['conflict', 'base advanced', 'merge unavailable'], ['pat', '']):
            client = self.client()
            client.candidate.side_effect = ValueError(reason)
            event = {'review': {**review(), 'body': 'CODEX-GATE: MEDIATE'},
                     'pull_request': {'number': 7}}
            client.reviews.return_value = [event['review']]
            with mock.patch.dict(os.environ, {'GITHUB_SHA': 'e'*40}), \
                    mock.patch.object(router.gate, 'run_verdict', return_value={'action': 'mediate'}) as route:
                result = router.admit(client, event, pat, '50', 1)
                self.assertEqual(result['admitted'], 'false')
                route.assert_called_once_with({**event, 'pull_request': client.get.return_value},
                                              REPO, pat, 'token', 'master', gh=client.gh)
                client.candidate.assert_not_called()
                client.gh.assert_not_called()
                client.post.assert_not_called()

    def test_untrusted_mediate_never_reaches_legacy_router(self):
        client = self.client()
        for user in (WRITER, {**p.CODEX, 'id': 1}, {**p.CODEX, 'type': 'User'}):
            event = {'review': {**review(), 'user': user, 'body': 'CODEX-GATE: MEDIATE'},
                     'pull_request': {'number': 7}}
            with mock.patch.object(router.gate, 'run_verdict') as route, self.assertRaises(ValueError):
                router.admit(client, event, 'pat', '50', 1)
            route.assert_not_called()

    def test_conflicted_mediate_reaches_real_legacy_dispatch_adapter(self):
        calls = []
        def gh(args, token, input_text=None):
            calls.append(args)
            if args == ['api', 'user']:
                return json.dumps(WRITER)
            if any('/comments?' in arg for arg in args):
                return '[]'
            if args == ['api', f'repos/{REPO}/pulls/7']:
                return json.dumps(event['pull_request'])
            if any('/reviews?' in arg for arg in args):
                return json.dumps([event['review']])
            if any('/events?' in arg for arg in args):
                return '[]'
            return '{}'
        client = router.Client(REPO, 'token', gh)
        event = {'review': {**review(), 'body': 'CODEX-GATE: MEDIATE'},
                 'pull_request': {'number': 7, 'state': 'open', 'draft': False, 'mergeable': False,
                                  'head': {'sha': VALUE['head'], 'repo': {'full_name': REPO}}}}
        with mock.patch.object(client, 'candidate', side_effect=AssertionError('no merge lookup')), \
                mock.patch.object(router.gate, 'emit'):
            result = router.admit(client, event, 'pat', '50', 1)
        self.assertEqual(result['action'], 'mediate')
        self.assertEqual(result['admitted'], 'false')
        dispatches = [args for args in calls if args[:2] == ['workflow', 'run']]
        self.assertEqual(len(dispatches), 1)
        self.assertIn('stuck-thread-watch.yml', dispatches[0])
        self.assertFalse(any('ci.yml' in arg for args in calls for arg in args))

    def test_admit_reserves_before_export_and_duplicate_events_do_not_mutate(self):
        client = self.client()
        records = [request()]
        client.comments.side_effect = lambda number: list(records)
        client.post.side_effect = lambda number, body: records.append(
            {'id': 2, 'user': p.ACTIONS, 'body': body, 'created_at': TIME, 'updated_at': TIME})
        helper = mock.Mock()
        helper.label_event_id.side_effect = [0, 3]
        event = {'review': review(), 'pull_request': {'number': 7}}
        with mock.patch.dict(os.environ, {'GITHUB_SHA': VALUE['merge']}), \
                mock.patch.object(router.gate, 'fixer_helpers', return_value=helper), \
                mock.patch.object(router.gate, 'run_verdict', return_value={'action': 'ready'}) as verdict:
            result = router.admit(client, event, 'pat', '50', 1)
            self.assertEqual(result['admitted'], 'true')
            self.assertEqual(json.loads(result['candidate']), VALUE)
            self.assertEqual(p.claims(records, VALUE)[0]['run'], '50')
            client.post.assert_called_once()
            verdict.reset_mock()
            self.assertEqual(router.admit(client, event, 'pat', '51', 1)['duplicate'], 'true')
            verdict.assert_not_called()
            client.post.assert_called_once()

    def test_uncertain_reservation_stops_without_retry_or_matrix_output(self):
        client = self.client()
        helper = mock.Mock()
        helper.label_event_id.side_effect = [0, 3]
        client.post.side_effect = OSError('unknown post outcome')
        with mock.patch.dict(os.environ, {'GITHUB_SHA': VALUE['merge']}), \
                mock.patch.object(router.gate, 'fixer_helpers', return_value=helper), \
                mock.patch.object(router.gate, 'run_verdict', return_value={'action': 'ready'}):
            with self.assertRaises(OSError):
                router.admit(client, {'review': review(), 'pull_request': {'number': 7}}, 'pat', '50', 1)
        client.post.assert_called_once()

    def test_request_deduplication_daily_bound_and_no_retry_after_unknown_post(self):
        client = self.client()
        with mock.patch.object(router.gate, 'post_comment') as post:
            self.assertEqual(router.request(client, 7, 'pat')['action'], 'already-requested')
            post.assert_not_called()
        today = router.datetime.now(router.timezone.utc).isoformat()
        records = []
        for i in range(3):
            value = {**VALUE, 'base': str(i)*40}
            item = comment('request', {'candidate': value, 'key': p.key(value)}, cid=i+1)
            item['created_at'] = item['updated_at'] = today
            records.append(item)
        client.comments.return_value = records
        with mock.patch.object(router.gate, 'post_comment') as post:
            with self.assertRaises(ValueError):
                router.request(client, 7, 'pat')
            post.assert_not_called()
        client.comments.return_value = []
        with mock.patch.object(router.gate, 'post_comment', side_effect=OSError('unknown outcome')) as post:
            with self.assertRaises(OSError):
                router.request(client, 7, 'pat')
            post.assert_called_once()

    def test_invalidation_delegates_only_owned_label_revocation_and_keeps_current_claim(self):
        client = self.client()
        with mock.patch.object(router.gate, 'fixer_helpers') as helpers:
            helpers.return_value.revoke_owned_ci.return_value = False
            self.assertEqual(router.invalidate(client, 7)['action'], 'unowned-or-absent')
            helpers.return_value.revoke_owned_ci.assert_called_once()
            client.comments.return_value = [request(), claim()]
            helpers.reset_mock()
            self.assertEqual(router.invalidate(client, 7)['action'], 'current-candidate')
            helpers.assert_not_called()
        client.gh.assert_not_called()
        client.post.assert_not_called()

    def test_review_edit_or_dismissal_revokes_owned_readiness_without_paid_admission(self):
        for state, body in [('DISMISSED', review()['body']), ('COMMENTED', 'Needs fixing')]:
            client = self.client()
            client.comments.return_value = [request(), claim()]
            changed = {**review(), 'state': state, 'body': body}
            client.reviews.return_value = [changed]
            with mock.patch.dict(os.environ, {'GITHUB_SHA': VALUE['merge']}), \
                    mock.patch.object(router.gate, 'fixer_helpers') as helpers, \
                    mock.patch.object(router.gate, 'run_verdict') as verdict:
                result = router.admit(client, {'action': 'dismissed', 'review': changed,
                                              'pull_request': {'number': 7}}, 'pat', '51', 1)
                self.assertEqual(result['admitted'], 'false')
                helpers.return_value.revoke_owned_ci.assert_called_once()
                verdict.assert_not_called()
                client.post.assert_not_called()

    def test_delayed_old_review_event_does_not_invalidate_new_ready(self):
        client = self.client()
        client.reviews.return_value = [review(), {**review(), 'id': 11,
                                      'submitted_at': '2026-10-16T01:00:02Z'}]
        with mock.patch.dict(os.environ, {'GITHUB_SHA': VALUE['merge']}), \
                mock.patch.object(router.gate, 'fixer_helpers') as helpers:
            result = router.admit(client, {'review': review(), 'pull_request': {'number': 7}}, 'pat', '51', 1)
            self.assertEqual(result['duplicate'], 'true')
            helpers.assert_not_called()

    def test_complete_bounded_listing_and_moving_snapshot(self):
        def gh(args, token):
            return json.dumps([{'id': 1}])
        self.assertEqual(router.Client(REPO, 'token', gh).pages('repos/x/comments'), [{'id': 1}])
        responses = iter([[{'id': 1}], [{'id': 2}]])
        with self.assertRaises(ValueError):
            router.Client(REPO, 'token', lambda *args: json.dumps(next(responses))).pages('repos/x/comments')
        calls = []
        def endless(args, token):
            calls.append(args)
            return json.dumps([{'id': len(calls)*100+i} for i in range(100)])
        with self.assertRaises(ValueError):
            router.Client(REPO, 'token', endless).pages('repos/x/comments')
        self.assertEqual(len(calls), p.MAX_PAGES)
        first = [{'id': n} for n in range(1, 101)]
        responses = iter([first, [{'id': 101, 'body': 'READY'}],
                          first, [{'id': 101, 'body': 'changed'}]])
        with self.assertRaises(ValueError):
            router.Client(REPO, 'token', lambda *args: json.dumps(next(responses))).pages('repos/x/reviews')

    def test_native_event_mismatch_is_rejected_before_label_writes(self):
        client = mock.Mock(repo=REPO)
        client.candidate.return_value = VALUE
        event = {'review': review(), 'pull_request': {'number': 7}}
        with mock.patch.dict(os.environ, {'GITHUB_SHA': 'e'*40}), mock.patch.object(router.gate, 'run_verdict') as mutation:
            with self.assertRaises(ValueError):
                router.admit(client, event, 'pat', '50', 1)
            mutation.assert_not_called()

    def test_hold_or_uncertain_label_never_reserves_matrix(self):
        client = mock.Mock(repo=REPO, token='token')
        client.candidate.return_value = VALUE
        client.gh.return_value = json.dumps(WRITER)
        client.comments.return_value = [request()]
        client.reviews.return_value = [review()]
        client.events.return_value = []
        event = {'review': review(), 'pull_request': {'number': 7}}
        for action in ('paused-findings', 'unverified-ci-label', 'preserve-human-removal', 'skip-readiness'):
            with mock.patch.dict(os.environ, {'GITHUB_SHA': VALUE['merge']}), \
                    mock.patch.object(router.gate, 'run_verdict', return_value={'action': action}):
                with self.assertRaises(ValueError):
                    router.admit(client, event, 'pat', '50', 1)
                client.post.assert_not_called()

    def test_final_rechecks_results_label_and_candidate_without_journal_writes(self):
        client = mock.Mock(repo=REPO, token='token')
        client.candidate.return_value = VALUE
        client.comments.return_value = [request(), claim()]
        client.reviews.return_value = [review()]
        helper = mock.Mock()
        helper.label_event_id.return_value = 3
        helper.readiness_blocked.return_value = False
        with mock.patch.dict(os.environ, {'GITHUB_SHA': VALUE['merge']}), \
                mock.patch.object(router.gate, 'fixer_helpers', return_value=helper):
            router.finish(client, VALUE, 10, WRITER, '50', 1, 'success')
            for result in ('failure','cancelled','skipped','pending',''):
                with self.assertRaises(ValueError):
                    router.finish(client, VALUE, 10, WRITER, '50', 1, result)
            helper.label_event_id.return_value = 4
            with self.assertRaises(ValueError):
                router.finish(client, VALUE, 10, WRITER, '50', 1, 'success')
            helper.label_event_id.return_value = 3
            helper.readiness_blocked.side_effect = lambda *a, **kw: kw['record']('new evidence')
            with self.assertRaises(ValueError):
                router.finish(client, VALUE, 10, WRITER, '50', 1, 'success')
        client.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
