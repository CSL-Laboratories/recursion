"""Exhaustive aggregate outcomes plus actual workflow wiring."""
import itertools
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_results import validate

ROOT = Path(__file__).resolve().parents[1]


def needs(settle='skipped', linux='success', windows='success', macos='success', run='true', classification='full'):
    return {'settle': {'result': settle}, 'linux': {'result': linux, 'outputs': {
        'run_full': run, 'classification': classification}}, 'windows': {'result': windows}, 'macos': {'result': macos}}


class AggregateTests(unittest.TestCase):
    def test_queue_exhaustive_results(self):
        states = ['success', 'failure', 'pending', 'cancelled', 'skipped', '']
        for settle, linux, windows, macos in itertools.product(states, repeat=4):
            args = needs(settle, linux, windows, macos)
            self.assertEqual(validate(args, event='merge_group'),
                             (settle, linux, windows, macos) == ('skipped', 'success', 'success', 'success'), args)

    def test_event_and_docs_selection(self):
        for event, full, run in itertools.product(['pull_request', 'pull_request_review', 'push', 'workflow_dispatch', 'merge_group'],
                                                 [True, False], ['true', 'false']):
            win = run == 'true'
            mac = run == 'true' and event != 'push'
            args = needs(windows='success' if win else 'skipped', macos='success' if mac else 'skipped',
                         run=run, classification='full' if run == 'true' else 'docs-only')
            expected = event not in {'pull_request', 'pull_request_review'} or run == 'true'
            self.assertEqual(validate(args, event=event, full=full), expected, (event, full, run))
            self.assertFalse(validate(args, event=event, full=full, cancelled=True))
            for job in ['linux', 'windows', 'macos']:
                for result in ['failure', 'pending', 'cancelled', '']:
                    mutated = json.loads(json.dumps(args))
                    mutated[job]['result'] = result
                    self.assertFalse(validate(mutated, event=event, full=full))

    def test_no_delayed_linux_only_run_can_weaken_full_coverage(self):
        # A label arriving after an ordinary run must feed this same aggregate.
        # Neither absent label snapshots nor reruns may approve skipped OS jobs.
        for action, full in itertools.product(['opened', 'ready_for_review', 'labeled', 'synchronize'], [False, True]):
            settle = 'success' if action == 'synchronize' else 'skipped'
            for windows, macos in itertools.product(['success', 'failure', 'pending', 'cancelled', 'skipped', ''], repeat=2):
                args = needs(settle=settle, windows=windows, macos=macos)
                self.assertEqual(validate(args, event='pull_request', action=action, full=full),
                                 windows == macos == 'success', (action, full, windows, macos))
        self.assertFalse(validate(needs(windows='skipped', macos='skipped', run='false', classification='docs-only'),
                                 event='pull_request', action='labeled', full=True))

    def test_docs_requires_explicit_verified_classification(self):
        for run, classification in itertools.product(['false', '', 'true'], ['', 'docs-only', 'full']):
            self.assertEqual(validate(needs(windows='skipped', macos='skipped', run=run,
                                            classification=classification), event='merge_group'),
                             run == 'false' and classification == 'docs-only')

    def test_sync_requires_successful_settle(self):
        self.assertTrue(validate(needs(settle='success'), event='pull_request', action='synchronize', full=True))
        self.assertFalse(validate(needs(), event='pull_request', action='synchronize', full=True))
        self.assertTrue(validate(needs(), event='pull_request', action='synchronize', draft=True, full=True))

    def test_actual_cli_failure_exit_and_workflow_wiring(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        aggregate = text.split('  platform-gates:\n')[1]
        self.assertIn('needs: [settle, linux, windows, macos]', aggregate)
        self.assertIn('always()', aggregate)
        self.assertIn('if: cancelled()\n        run: exit 1', aggregate)
        self.assertIn('CI_NEEDS: ${{ toJSON(needs) }}', aggregate)
        required_linux = text.split('  linux:\n', 1)[1].split('  windows:\n', 1)[0]
        self.assertIn('classification: ${{ needs.linux-tests.outputs.classification }}', required_linux)
        self.assertIn('run_full: ${{ needs.linux-tests.outputs.run_full }}', required_linux)
        self.assertIn('CI_ACTION: ${{ github.event.action }}', aggregate)
        self.assertIn('CI_DRAFT: ${{ github.event.pull_request.draft || false }}', aggregate)
        self.assertIn("CI_FULL: ${{ inputs.candidate != '' }}", aggregate)
        self.assertNotIn('CI_HEAD_SHA:', text.split('jobs:')[0])
        self.assertNotIn('CI_HEAD_SHA: ${{ github.event.pull_request.head.sha', text)
        self.assertIn('github.event.merge_group.head_sha || github.sha', text)
        for result, code in [('success', 0), ('failure', 1), ('skipped', 1), ('pending', 1)]:
            env = {**os.environ, 'CI_NEEDS': json.dumps(needs(macos=result)), 'CI_EVENT': 'merge_group',
                   'CI_ACTION': '', 'CI_DRAFT': 'false', 'CI_FULL': 'false', 'CI_CANCELLED': 'false'}
            proc = subprocess.run([sys.executable, str(ROOT / 'tools/ci_results.py')], env=env,
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)


if __name__ == '__main__':
    unittest.main()
