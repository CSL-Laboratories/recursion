"""Executable adversarial provider identity, retry and recovery regressions."""
import copy
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_gate as gate

SHA = 'a' * 40
REPO = 'CSL-Laboratories/recursion'


def status(identifier=1, state='success', **kw):
    return dict(id=identifier, state=state, context='Devin Review', creator={
        'id': 158243242, 'login': 'devin-ai-integration[bot]', 'type': 'Bot'}, **kw)


def api_for(rows, *, sha=SHA):
    def api(path):
        if path == f'commits/{SHA}/status?per_page=1':
            return {'sha': sha}
        if '/statuses?' in path:
            page = int(path.rsplit('=', 1)[1])
            return rows[(page - 1) * 100:page * 100]
        raise AssertionError(f'Unexpected API/fallback: {path}')
    return api


class EvidenceTests(unittest.TestCase):
    def check(self, rows):
        with patch.object(gate, 'gh_api', side_effect=api_for(rows)):
            return gate.devin_review_succeeded(SHA)

    def test_verified_success(self):
        self.assertTrue(self.check([status()]))

    def test_untrusted_named_status(self):
        for field, value in [('id', 9), ('login', 'impostor'), ('type', 'User')]:
            row = status()
            row['creator'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check([row])

    def test_missing_identity_fails(self):
        row = status()
        del row['creator']
        with self.assertRaises(ValueError):
            self.check([row])

    def test_wrong_sha(self):
        with patch.object(gate, 'gh_api', side_effect=api_for([status()], sha='b' * 40)):
            with self.assertRaises(ValueError):
                gate.devin_review_succeeded(SHA)

    def test_pending_does_not_fall_back_to_old_successful_check(self):
        # api_for raises if implementation attempts the former check-run fallback.
        self.assertFalse(self.check([status(10, 'pending'), status(9, 'success')]))

    def test_named_checks_alone_never_authorize(self):
        self.assertFalse(self.check([]))

    def test_latest_attempt_by_id_not_order_or_timestamp(self):
        for state in ['pending', 'failure', 'error']:
            with self.subTest(state=state):
                self.assertFalse(self.check([status(10, 'success'), status(11, state)]))
        self.assertTrue(self.check([status(12), status(11, 'failure')]))

    def test_pages_and_wrong_provider_cannot_hide_after_first_page(self):
        rows = [dict(id=i, context=f'other-{i}', state='success') for i in range(1, 101)]
        self.assertTrue(self.check(rows + [status(101)]))
        bad = status(101)
        bad['creator']['id'] = 1
        with self.assertRaises(ValueError):
            self.check(rows + [bad])

    def test_duplicate_and_malformed_statuses_fail(self):
        for rows in [[status(), status()], [dict(id='1')], [dict(id=True)], [status(state='neutral')]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.check(rows)

    def test_changed_snapshot_fails(self):
        with patch.object(gate, 'gh_api', side_effect=[{'sha': SHA}, [status()], [status(2, 'pending')]]):
            with self.assertRaises(ValueError):
                gate.devin_review_succeeded(SHA)

    def test_pagination_bound_fails_closed(self):
        rows = [dict(id=i, context='other', state='success') for i in range(1, 1001)]
        with self.assertRaises(ValueError):
            self.check(rows)

    def test_invalid_sha_prevents_network(self):
        for sha in ['', 'HEAD', '0' * 40, '../main', 'a' * 39]:
            with patch.object(gate, 'gh_api') as api, self.assertRaises(ValueError):
                gate.devin_review_succeeded(sha)
            api.assert_not_called()


class TargetAndPollingTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'GITHUB_REPOSITORY': REPO, 'GITHUB_SHA': SHA})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pr = dict(number=42, state='open', draft=False, head={'sha': SHA}, base={'repo': {'full_name': REPO}})

    def test_live_pr_identity(self):
        with patch.object(gate, 'gh_api', return_value=self.pr):
            gate.verify_target('pull_request', SHA, '42')
        for key, value in [('draft', True), ('state', 'closed'), ('number', 43), ('head', {'sha': 'b' * 40}),
                           ('base', {'repo': {'full_name': 'wrong/repo'}})]:
            pr = copy.deepcopy(self.pr)
            pr[key] = value
            with patch.object(gate, 'gh_api', return_value=pr), self.assertRaises(ValueError):
                gate.verify_target('pull_request', SHA, '42')

    def test_status_and_dispatch_cannot_claim_pr_recovery(self):
        for event in ['status', 'workflow_dispatch', 'check_run', '']:
            with self.assertRaises(ValueError):
                gate.verify_target(event, SHA, '42')
        with self.assertRaises(ValueError):
            gate.verify_target('pull_request', SHA, '')

    def test_queue_checks_native_sha_and_never_blindly_passes(self):
        gate.verify_target('merge_group', SHA, '')
        with self.assertRaises(ValueError):
            gate.verify_target('merge_group', 'b' * 40, '')
        with patch.object(gate, 'devin_review_succeeded', return_value=False):
            self.assertFalse(gate.poll_until_success(SHA, seconds=0, interval=1))

    def test_pending_then_success_transition(self):
        with patch.object(gate, 'devin_review_succeeded', side_effect=[False, True]), \
                patch.object(gate.time, 'sleep') as sleep, patch.object(gate.time, 'monotonic', side_effect=[0, 0]), \
                patch.object(gate, 'verify_target') as verify:
            self.assertTrue(gate.poll_until_success(SHA, seconds=10, interval=1, verify=verify))
            self.assertEqual(verify.call_count, 3)
            sleep.assert_called_once_with(1)

    def test_timeout_and_invalid_poll_bounds(self):
        with patch.object(gate, 'devin_review_succeeded', return_value=False), \
                patch.object(gate.time, 'monotonic', side_effect=[0, 180]):
            self.assertFalse(gate.poll_until_success(SHA, seconds=180, interval=15))
        for seconds, interval in [(181, 15), (-1, 15), (180, 0), (180, 31)]:
            with self.assertRaises(ValueError):
                gate.poll_until_success(SHA, seconds=seconds, interval=interval)

    def test_push_racing_success_is_rejected(self):
        with patch.object(gate, 'devin_review_succeeded', return_value=True), \
                patch.object(gate, 'verify_target', side_effect=[None, ValueError('head advanced')]) as verify:
            with self.assertRaises(ValueError):
                gate.poll_until_success(SHA, seconds=0, interval=1, verify=verify)

    def test_workflow_authority_and_recovery_contract(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/review-gate.yml').read_text()
        self.assertNotIn('\n  status:', text)
        self.assertNotIn(': write', text)
        self.assertNotIn('Pass on merge queue', text)
        self.assertIn('github.event.pull_request.base.sha', text)
        self.assertIn('persist-credentials: false', text)
        self.assertIn('timeout-minutes: 8', text)
        self.assertIn('HEAD_SHA: ${{ github.event.pull_request.head.sha || github.event.merge_group.head_sha }}', text)
        self.assertIn('EVENT_NAME: ${{ github.event_name }}', text)
        self.assertIn('PR_NUMBER: ${{ github.event.pull_request.number }}', text)

    def test_main_success_timeout_and_exception_exit(self):
        with patch.dict(os.environ, {'EVENT_NAME': 'pull_request', 'HEAD_SHA': SHA,
                                      'PR_NUMBER': '42', 'REVIEW_GATE_POLL_SECONDS': '0'}), \
                patch.object(gate, 'verify_target'), patch.object(gate, 'devin_review_succeeded', return_value=True):
            self.assertEqual(gate.main(), 0)
        with patch.dict(os.environ, {'REVIEW_GATE_POLL_SECONDS': '0'}), \
                patch.object(gate, 'verify_target'), patch.object(gate, 'devin_review_succeeded', return_value=False):
            self.assertEqual(gate.main(), 1)
        env = {**os.environ, 'EVENT_NAME': 'status', 'HEAD_SHA': SHA, 'PR_NUMBER': '42'}
        proc = subprocess.run([sys.executable, gate.__file__], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('Only original PR', proc.stdout)


if __name__ == '__main__':
    unittest.main()
